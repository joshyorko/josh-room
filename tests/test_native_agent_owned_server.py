"""Real child/socket ownership checks; no provider, authentication or external traffic."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room import native_agent_owned_server as owned

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd/proc ownership")

SERVER = """import os,socket,sys
s=socket.socket()
s.bind((sys.argv[1],0));s.listen()
print(s.getsockname()[1],flush=True)
for command in sys.stdin:
 if command.strip()=='move':os.chdir(sys.argv[2]);print('moved',flush=True)
"""


@pytest.fixture
def spawned(tmp_path):
    executable = Path(sys.executable).resolve()
    workspace = tmp_path / "workspace"
    moved = tmp_path / "moved-workspace"
    home = tmp_path / "home"
    profile = tmp_path / "profile"
    for directory in (workspace, moved, home, profile, *(profile / name for name in ("t3", "codex", "claude"))):
        directory.mkdir(mode=0o700)
    environment = {
        "HOME": str(home), "T3CODE_HOME": str(profile / "t3"),
        "CODEX_HOME": str(profile / "codex"), "CLAUDE_CONFIG_DIR": str(profile / "claude"),
    }
    children = []

    def spawn(address="127.0.0.1"):
        argv = [str(executable), "-c", SERVER, address, str(moved)]
        process = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        children.append(process)
        port = int(process.stdout.readline())
        return process, {"executable": executable, "argv": argv, "cwd": workspace, "environment": environment, "port": port}

    yield spawn
    for process in children:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_exact_owned_server_is_checked_and_descriptor_is_closed(spawned):
    process, options = spawned()
    with owned.OwnedServer(process, **options) as proof:
        descriptor = proof.pidfd
        proof.check()
        assert process.poll() is None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with pytest.raises(owned.OwnedServerError, match="closed"):
        proof.check()
    proof.close()
    assert process.poll() is None


def test_foreign_listener_cannot_be_used_by_owned_process(spawned):
    process, options = spawned()
    foreign, foreign_options = spawned()
    assert process.pid != foreign.pid
    with pytest.raises(owned.OwnedServerError, match="endpoint-foreign"):
        owned.OwnedServer(process, **{**options, "port": foreign_options["port"]})


def test_wildcard_endpoint_is_refused(spawned):
    process, options = spawned("0.0.0.0")
    with pytest.raises(owned.OwnedServerError, match="endpoint-mismatch"):
        owned.OwnedServer(process, **options)


def test_wrong_port_is_refused(spawned):
    process, options = spawned()
    with pytest.raises(owned.OwnedServerError, match="endpoint-mismatch"):
        owned.OwnedServer(process, **{**options, "port": 65535 if options["port"] != 65535 else 65534})


def test_dead_process_cannot_reuse_endpoint_receipt(spawned):
    process, options = spawned()
    with owned.OwnedServer(process, **options) as proof:
        process.terminate()
        process.wait(timeout=5)
        with pytest.raises(owned.OwnedServerError, match="process-changed"):
            proof.check()


def test_reused_pid_start_time_is_refused(spawned, monkeypatch):
    process, options = spawned()
    with owned.OwnedServer(process, **options) as proof:
        monkeypatch.setattr(owned, "_proc_start_time", lambda _pid: proof.start_time + 1)
        with pytest.raises(owned.OwnedServerError, match="process-changed"):
            proof.check()


def test_working_directory_change_is_refused(spawned):
    process, options = spawned()
    with owned.OwnedServer(process, **options) as proof:
        process.stdin.write("move\n")
        process.stdin.flush()
        assert process.stdout.readline() == "moved\n"
        with pytest.raises(owned.OwnedServerError, match="workspace-mismatch"):
            proof.check()


@pytest.mark.parametrize("name", ["HOME", "T3CODE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR"])
def test_foreign_profile_environment_is_refused(spawned, tmp_path, name):
    process, options = spawned()
    foreign = tmp_path / "foreign" / Path(options["environment"][name]).name
    foreign.mkdir(mode=0o700, parents=True)
    environment = {**options["environment"], name: str(foreign)}
    with pytest.raises(owned.OwnedServerError, match="environment-mismatch|profile-mismatch"):
        owned.OwnedServer(process, **{**options, "environment": environment})


def test_profile_directory_replacement_is_refused(spawned):
    process, options = spawned()
    with owned.OwnedServer(process, **options) as proof:
        path = Path(options["environment"]["CLAUDE_CONFIG_DIR"])
        path.rename(path.with_name("old-claude"))
        path.mkdir(mode=0o700)
        with pytest.raises(owned.OwnedServerError, match="environment-mismatch"):
            proof.check()


def test_symlink_profile_is_refused(spawned, tmp_path):
    process, options = spawned()
    linked = tmp_path / "linked-home"
    linked.symlink_to(options["environment"]["HOME"], target_is_directory=True)
    with pytest.raises(owned.OwnedServerError, match="path-invalid"):
        owned.OwnedServer(process, **{**options, "environment": {**options["environment"], "HOME": str(linked)}})


def test_wrong_command_is_refused(spawned):
    process, options = spawned()
    with pytest.raises(owned.OwnedServerError, match="command-mismatch"):
        owned.OwnedServer(process, **{**options, "argv": [*options["argv"], "foreign"]})


def test_nonchild_process_identity_is_refused(spawned, monkeypatch):
    process, options = spawned()
    actual = owned._read

    def foreign_status(path, maximum):
        value = actual(path, maximum)
        if path.name == "status":
            value = value.replace(f"PPid:\t{os.getpid()}\n".encode(), b"PPid:\t1\n")
        return value

    monkeypatch.setattr(owned, "_read", foreign_status)
    with pytest.raises(owned.OwnedServerError, match="process-foreign"):
        owned.OwnedServer(process, **options)


def test_missing_pidfd_fails_closed(spawned, monkeypatch):
    process, options = spawned()
    monkeypatch.delattr(os, "pidfd_open")
    with pytest.raises(owned.OwnedServerError, match="platform-unavailable"):
        owned.OwnedServer(process, **options)


def test_proc_read_failure_is_sanitized_and_closes_pidfd(spawned, monkeypatch):
    process, options = spawned()
    descriptors = []
    actual = os.pidfd_open

    def opened(*args):
        descriptor = actual(*args)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "pidfd_open", opened)
    monkeypatch.setattr(owned, "_read", lambda *_args: (_ for _ in ()).throw(PermissionError("synthetic private detail")))
    with pytest.raises(owned.OwnedServerError, match="^owned-server-unverifiable$"):
        owned.OwnedServer(process, **options)
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
