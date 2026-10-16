"""Opt-in real native runtime checks, with disposable authorities only."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import psutil
import pytest

from josh_room.adapter_contract import CancellationToken
from josh_room.private_paths import protect_private_file, verify_private_path
from josh_room.restic_store import RepositoryInfo, ResticStoreError
from josh_room.rustic_store import RusticStore
from scripts import install_rustic


def authority(tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-native-password")
    protect_private_file(password)
    return {"repository": tmp_path / "repository", "cache_dir": tmp_path / "cache", "password_file": password}


def test_real_installer_cache_and_native_permissions():
    value = os.environ.get("JOSH_ROOM_RUSTIC_QUALIFICATION_RUNTIME")
    if not value:
        pytest.skip("explicit disposable installed runtime not supplied")
    runtime = Path(value)
    native = "win32-x64" if os.name == "nt" else "linux-x64"
    manifest = Path(__file__).resolve().parents[1] / "vscode-extension/runtime/rustic-manifest.json"
    result = install_rustic.install_rustic(manifest, runtime, native)
    assert result["cached"] is True
    binary = Path(result["executable"])
    for directory in (runtime, runtime / "rustic", runtime / "rustic/0.11.4", binary.parent):
        verify_private_path(directory, directory=True)
    assert install_rustic._regular_file(binary, executable=True)
    verify_private_path(binary.with_name(binary.name + ".sha256"), directory=False)


@pytest.mark.skipif(os.name != "nt", reason="native Windows scoped custom-CA rejection")
def test_native_windows_custom_ca_rejected_before_spawn(tmp_path):
    ca = tmp_path / "synthetic-ca.pem"
    ca.write_text("synthetic-ca-bounded-file")
    with (
        pytest.raises(ResticStoreError) as error,
        RusticStore(**authority(tmp_path), ca_bundle=ca,
                    process_factory=lambda *_a, **_k: pytest.fail("spawned with unsupported custom CA")),
    ):
        pytest.fail("admitted unsupported Windows custom CA")
    assert error.value.code.value == "invalid-configuration"


def test_native_running_process_tree_cancellation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child_pid = tmp_path / "child.pid"
    token = CancellationToken()
    processes = []
    script = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )

    def spawn(_argv, **kwargs):
        process = subprocess.Popen([sys.executable, "-c", script, str(child_pid)], **kwargs)
        processes.append(process)
        return process

    def cancel_when_running():
        import time
        deadline = time.monotonic() + 8
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        token.cancel()

    thread = threading.Thread(target=cancel_when_running, daemon=True)
    with RusticStore(**authority(tmp_path), process_factory=spawn, command_timeout=10) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        thread.start()
        try:
            with pytest.raises(ResticStoreError) as error:
                store.backup(workspace, cancellation=token)
            assert error.value.code.value == "cancelled"
            assert store._active_process is None
            assert processes and all(p.poll() is not None for p in processes)
            assert child_pid.exists()
            pid = int(child_pid.read_text())
            assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        finally:
            thread.join(timeout=1)
