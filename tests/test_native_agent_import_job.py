"""Temporary worker protocol keeps live admission in the host controller."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import native_agent_import_job as job
from josh_room import native_agents as native
from josh_room.cancellation import CLICancelled

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux temporary import job")
SESSION = "11111111-1111-4111-8111-111111111111"
RECEIPT = {"admitted": True, "authority_sha256": "a" * 64, "native_resume_verified": False}
RESULT = {
    "ok": True, "state": "history-imported", "tool": "codex", "imported_count": 1,
    "sessions": [{"provider_session_id": SESSION, "new_t3_thread_id": "import:codex:" + SESSION}],
    "cwd_matches": True, "runnable_effects": 0, "original_t3_identity_restored": False,
    "native_resume_verified": False,
}
WORKER = """import json,sys,time
config=json.loads(sys.stdin.readline())
def send(value):print(json.dumps(value),flush=True)
def read():return json.loads(sys.stdin.readline())
def admission():
 send({'kind':'admit'});reply=read()
 if not reply['ok']:
  send({'kind':'result','result':{'ok':False,'error':reply['error'],'committed':False,'cancelled':False}})
  sys.exit(1)
admission()
send({'kind':'may-import'});read()
admission()
"""


@pytest.fixture
def profile(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    root = tmp_path / "profile"
    binding = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": str(workspace)}
    native._initialize(root, binding, ["age1" + "a" * 58, "age1" + "b" * 58])
    calls = []

    def admit(*args, **kwargs):
        calls.append((os.getpid(), args, kwargs))
        return dict(RECEIPT)

    monkeypatch.setattr(native, "_admit", admit)
    return root, binding, calls


@pytest.fixture
def worker(monkeypatch):
    processes = []

    def start(script):
        def spawn(_root):
            process = subprocess.Popen(
                [sys.executable, "-s", "-B", "-c", script], env={},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, start_new_session=True,
            )
            processes.append(process)
            return process
        monkeypatch.setattr(job, "_start_worker", spawn)

    yield start
    assert all(process.poll() is not None for process in processes)


def test_import_job_revalidates_only_in_host_and_returns_metadata(profile, worker):
    root, binding, calls = profile
    worker(WORKER + "send({'kind':'result','result':" + repr(RESULT) + "})\n")
    assert job.import_history(root, executable="/synthetic/t3", **binding) == RESULT
    assert len(calls) == 4
    assert {call[0] for call in calls} == {os.getpid()}
    assert all(call[2] == {"root": root, "operation": "import-history", "tool": "codex"} for call in calls)


@pytest.mark.parametrize("after_mark", [False, True])
def test_revoked_authority_is_not_replaced_with_worker_approval(profile, worker, monkeypatch, after_mark):
    root, binding, _calls = profile
    count = 0

    def revoked(*_args, **_kwargs):
        nonlocal count
        count += 1
        if count == (3 if after_mark else 2):
            raise native.NativeAgentError("native-admission-not-authorized")
        return dict(RECEIPT)

    monkeypatch.setattr(native, "_admit", revoked)
    worker(WORKER + "send({'kind':'result','result':" + repr(RESULT) + "})\n")
    with pytest.raises(native.NativeAgentError) as failure:
        job.import_history(root, executable="/synthetic/t3", **binding)
    assert failure.value.committed is after_mark
    assert str(failure.value) == ("t3-import-outcome-uncertain" if after_mark else "native-admission-not-authorized")


def test_unavailable_initial_authority_never_spawns_worker(profile, monkeypatch):
    root, binding, _calls = profile

    def denied(*_args, **_kwargs):
        raise native.NativeAgentError("native-admission-not-authorized")

    monkeypatch.setattr(native, "_admit", denied)
    monkeypatch.setattr(job, "_start_worker", lambda _root: pytest.fail("must not spawn"))
    with pytest.raises(native.NativeAgentError, match="native-admission-not-authorized"):
        job.import_history(root, executable="/synthetic/t3", **binding)


def test_profile_policy_change_is_checked_on_every_gate(profile, worker, monkeypatch):
    root, binding, _calls = profile
    worker("import json,sys\nconfig=json.loads(sys.stdin.readline())\nfrom pathlib import Path\np=Path(config['root'])/'native-profile.json'\np.write_bytes(p.read_bytes()+b' ')\nprint(json.dumps({'kind':'admit'}),flush=True)\nr=json.loads(sys.stdin.readline())\nprint(json.dumps({'kind':'result','result':{'ok':False,'error':r['error'],'committed':False,'cancelled':False}}),flush=True)\nsys.exit(1)\n")
    with pytest.raises(native.NativeAgentError, match="native-admission-changed"):
        job.import_history(root, executable="/synthetic/t3", **binding)


@pytest.mark.parametrize("marked", [False, True])
def test_worker_exit_has_truthful_uncertainty(profile, worker, marked):
    root, binding, _calls = profile
    worker("import json,sys\njson.loads(sys.stdin.readline())\n" + ("print(json.dumps({'kind':'may-import'}),flush=True)\njson.loads(sys.stdin.readline())\n" if marked else ""))
    with pytest.raises(native.NativeAgentError) as failure:
        job.import_history(root, executable="/synthetic/t3", **binding)
    assert failure.value.committed is marked
    assert str(failure.value) == ("t3-import-outcome-uncertain" if marked else "t3-import-job-exited")


def test_cancel_after_import_mark_stops_worker_and_preserves_uncertainty(profile, worker, monkeypatch):
    root, binding, _calls = profile
    worker(WORKER + "time.sleep(30)\n")
    receive = job._receive
    calls = 0

    def cancel(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise CLICancelled
        return receive(*args)

    monkeypatch.setattr(job, "_receive", cancel)
    with pytest.raises(native.NativeAgentError, match="t3-import-outcome-uncertain") as failure:
        job.import_history(root, executable="/synthetic/t3", **binding)
    assert failure.value.committed is True


@pytest.mark.parametrize("message", [{"kind": "other"}, {"kind": "admit", "root": "/foreign"}, {"kind": "result", "result": {"ok": True, "private": "synthetic-secret"}}])
def test_unexpected_worker_protocol_is_not_forwarded(profile, worker, message):
    root, binding, _calls = profile
    worker("import json,sys\njson.loads(sys.stdin.readline())\nprint(" + repr(json.dumps(message)) + ",flush=True)\n")
    with pytest.raises(native.NativeAgentError, match="t3-import-job-protocol-invalid"):
        job.import_history(root, executable="/synthetic/t3", **binding)


def test_worker_environment_does_not_copy_host_auth_or_module_options(profile, monkeypatch):
    root, _binding, _calls = profile
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NODE_OPTIONS", "NODE_PATH", "LD_PRELOAD", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.setenv(name, "synthetic-sensitive-value")
    environment = job._environment(root)
    assert set(environment) <= {"HOME", "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH", "TMPDIR"}
    assert environment["HOME"] == str(root)
    assert environment["PYTHONPATH"] == str(Path(job.__file__).resolve().parents[1])
    assert environment["PYTHONSAFEPATH"] == "1"
    assert "synthetic-sensitive-value" not in environment.values()


def test_admission_receipt_projection_never_relays_private_fields():
    assert job._public_admission({**RECEIPT, "credential": "synthetic-sensitive-value"}) == RECEIPT
    with pytest.raises(native.NativeAgentError, match="admission-invalid"):
        job._public_admission({"admitted": True, "authority_sha256": "invalid"})


def test_unshare_does_not_use_caller_path(monkeypatch, tmp_path):
    command = tmp_path / "unshare"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    try:
        selected = job._system_unshare()
    except native.NativeAgentError as error:
        assert str(error) == "t3-import-isolation-unavailable"
        return
    assert selected != str(command)
    assert Path(selected).stat().st_uid == 0


def test_result_requires_original_provider_and_new_t3_identity():
    broken = {**RESULT, "sessions": [{"provider_session_id": SESSION, "new_t3_thread_id": SESSION}]}
    with pytest.raises(native.NativeAgentError, match="protocol-invalid"):
        job._result({"kind": "result", "result": broken}, "codex", True)


def test_worker_main_refuses_noninternal_invocation(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["native_agent_import_job"])
    assert job.main() == 2


@pytest.mark.parametrize("change_at", [2, 4])
def test_changed_authority_digest_from_preflight_or_final_gate_refuses(profile, worker, monkeypatch, change_at):
    root, binding, _calls = profile
    count = 0

    def changed(*_args, **_kwargs):
        nonlocal count
        count += 1
        return {**RECEIPT, "authority_sha256": ("b" if count >= change_at else "a") * 64}

    monkeypatch.setattr(native, "_admit", changed)
    worker(WORKER + "send({'kind':'result','result':" + repr(RESULT) + "})\n")
    with pytest.raises(native.NativeAgentError) as failure:
        job.import_history(root, executable="/synthetic/t3", **binding)
    assert failure.value.committed is (change_at == 4)
    assert str(failure.value) == ("t3-import-outcome-uncertain" if change_at == 4 else "native-admission-changed")


def test_worker_cannot_import_a_shadow_package_from_callers_workspace(profile, tmp_path, monkeypatch):
    root, _binding, _calls = profile
    workspace = tmp_path / "hostile-cwd"
    package = workspace / "josh_room"
    package.mkdir(parents=True)
    marker = tmp_path / "shadow-executed"
    (package / "__init__.py").write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('unsafe')\n")
    (package / "native_agent_import_job.py").write_text("raise SystemExit(0)\n")
    shim = tmp_path / "synthetic-unshare"
    shim.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    shim.chmod(0o700)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(job, "_system_unshare", lambda: str(shim))
    process = job._start_worker(root)
    try:
        deadline = time.monotonic() + 10
        job._send(process.stdin, {"invalid": True}, deadline)
        result = job._receive(process.stdout, bytearray(), deadline)
        assert result["kind"] == "result" and result["result"]["ok"] is False
        assert process.wait(timeout=5) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
    assert not marker.exists()
