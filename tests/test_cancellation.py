import json
import os
import selectors
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from josh_room import cli
from josh_room.cancellation import CLICancelled, sigterm_cancellation


def test_sigterm_cancellation_restores_the_previous_handler():
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(CLICancelled), sigterm_cancellation():
        signal.raise_signal(signal.SIGTERM)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_sigterm_cancellation_does_not_mutate_handlers_from_worker_thread():
    previous = signal.getsignal(signal.SIGTERM)
    seen = []

    def worker():
        with sigterm_cancellation():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert seen == [previous]
    assert signal.getsignal(signal.SIGTERM) == previous


def test_cli_preserves_operation_details_in_cancelled_result(monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext

    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "_instance_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_uses_minio_encryption", lambda _args: False)
    monkeypatch.setattr(cli, "_identity_environment", nullcontext)
    monkeypatch.setattr(cli, "load_runtime_session", lambda: False)
    monkeypatch.setattr(cli, "_requires_oauth", lambda _args: False)
    monkeypatch.setattr(cli, "_requires_encryption", lambda _args: False)
    monkeypatch.setattr(cli, "_write_runtime_result", lambda _result: None)
    monkeypatch.setattr(
        cli,
        "dispatch",
        lambda *_args: (_ for _ in ()).throw(
            CLICancelled({
                "publication_state": "committed",
                "marker_state": "updated",
                "snapshot_id": "snapshot-synthetic",
            })
        ),
    )

    assert cli.main(["projects", "list", "--json"]) == 130
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "state": "cancelled",
        "cancelled": True,
        "publication_state": "committed",
        "marker_state": "updated",
        "snapshot_id": "snapshot-synthetic",
    }


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM subprocess contract is POSIX-specific")
def test_cli_sigterm_unwinds_and_emits_cancelled_result(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    script = """
import time
from contextlib import nullcontext
from pathlib import Path
from josh_room import cli
cli.initialize_system_trust = lambda: None
cli._instance_root = lambda: Path('/tmp')
cli._uses_minio_encryption = lambda _args: False
cli._identity_environment = lambda: nullcontext()
cli.load_runtime_session = lambda: False
cli._requires_oauth = lambda _args: False
cli._requires_encryption = lambda _args: False
cli._write_runtime_result = lambda _result: None
def blocked_dispatch(*_args):
    print('dispatch-ready', flush=True)
    time.sleep(30)
cli.dispatch = blocked_dispatch
raise SystemExit(cli.main(['projects', 'list', '--json']))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(source_root / "src"), environment.get("PYTHONPATH")])
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=source_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=3), "CLI did not enter dispatch"
        assert process.stdout.readline().strip() == "dispatch-ready"
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=3)
    assert process.returncode == 130, stderr
    assert json.loads(stdout.splitlines()[-1]) == {
        "ok": False,
        "state": "cancelled",
        "cancelled": True,
    }
