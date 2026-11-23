import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from josh_room import serve_job_windows

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows Job Objects")

_LISTENER = """
import json, os, socket, sys
from pathlib import Path
with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen()
    ready = Path(sys.argv[1])
    pending = ready.with_suffix(".tmp")
    pending.write_text(json.dumps([os.getpid(), server.getsockname()[1]]))
    pending.replace(ready)
    while True:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b"alive")
"""

_OWNER = """
import subprocess, sys
from josh_room.serve_job_windows import ensure_serve_job
ensure_serve_job()
ensure_serve_job()
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]],
                 creationflags=subprocess.DETACHED_PROCESS,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.stdin.buffer.read(1)
sys.exit(19)
"""


@contextlib.contextmanager
def _session(ready):
    import _winapi

    # Target the actual interpreter, not the venv's extra launcher process.
    env = {**os.environ, "PYTHONPATH": str(Path(serve_job_windows.__file__).resolve().parents[1])}
    owner = subprocess.Popen(
        [sys._base_executable, "-c", _OWNER, _LISTENER, str(ready)],
        env=env, stdin=subprocess.PIPE,
    )
    child = None
    try:
        deadline = time.monotonic() + 15
        while not ready.is_file():
            assert owner.poll() is None, "Serve job owner exited before the listener was ready"
            assert time.monotonic() < deadline, "Serve job listener failed to start"
            time.sleep(0.02)
        pid, port = json.loads(ready.read_text())
        child = _winapi.OpenProcess(0x00100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
        yield owner, child, port
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        owner.stdin.close()
        if child is not None:
            try:
                if _winapi.WaitForSingleObject(child, 1000) != _winapi.WAIT_OBJECT_0:
                    _winapi.TerminateProcess(child, 1)
            finally:
                _winapi.CloseHandle(child)


def _assert_serving(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as conn:
        assert conn.makefile("rb").read() == b"alive"


@pytest.mark.parametrize("abrupt", [False, True], ids=["normal-exit", "owner-killed"])
def test_owner_exit_closes_only_its_detached_listener(tmp_path, abrupt):
    import _winapi

    with _session(tmp_path / "subject") as (owner, child, port), _session(tmp_path / "other") as other:
        _assert_serving(port)
        _assert_serving(other[2])
        if abrupt:
            owner.kill()
        else:
            owner.stdin.write(b"x")
            owner.stdin.flush()
        status = owner.wait(timeout=10)
        if not abrupt:
            assert status == 19
        assert _winapi.WaitForSingleObject(child, 5000) == _winapi.WAIT_OBJECT_0
        # Windows runner networking can silently drop a closed-port SYN instead
        # of returning RST. The process handle above must already be signalled.
        with pytest.raises((ConnectionRefusedError, TimeoutError)):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        _assert_serving(other[2])


def test_job_assignment_failure_closes_unassigned_handle(monkeypatch):
    kernel = Mock()
    kernel.CreateJobObjectW.return_value = 123
    kernel.SetInformationJobObject.return_value = True
    kernel.AssignProcessToJobObject.return_value = False
    monkeypatch.setattr(serve_job_windows.ctypes, "WinDLL", lambda *args, **kw: kernel)
    with pytest.raises(RuntimeError, match="could not join"):
        serve_job_windows.ensure_serve_job()
    kernel.CloseHandle.assert_called_once_with(123)
    assert serve_job_windows._serve_job is None
