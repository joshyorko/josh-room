import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import serve_process_guard

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"),
    reason="requires native Linux pidfds",
)

_LISTENER = """
import json, os, socket, sys
from pathlib import Path
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    server.listen()
    ready = Path(sys.argv[1])
    pending = ready.with_suffix('.tmp')
    pending.write_text(json.dumps([os.getpid(), server.getsockname()[1]]))
    pending.replace(ready)
    while True:
        conn, _ = server.accept()
        with conn:
            conn.sendall(b'alive')
"""
_RCC = """
import os, subprocess, sys, time
from pathlib import Path
Path(sys.argv[2] + '.rcc').write_text(str(os.getpid()))
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True)
time.sleep(60)
"""
_RCC_EXITS = _RCC.replace("time.sleep(60)", "while not Path(sys.argv[2] + '.stop').exists(): time.sleep(.01)\nsys.exit(23)")
_OWNER = """
import sys
from josh_room.jat import _run_cli
status, _, _ = _run_cli([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], None, guard_owner=True)
raise SystemExit(status)
"""


@contextlib.contextmanager
def _session(ready, rcc_script=_RCC):
    env = {**os.environ, "PYTHONPATH": str(Path(serve_process_guard.__file__).resolve().parents[1])}
    owner = subprocess.Popen([sys.executable, "-c", _OWNER, rcc_script, _LISTENER, str(ready)], env=env)
    child = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert owner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        child, port = json.loads(ready.read_text())
        yield owner, child, port
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if child is not None and _alive(child):
            # Test failure must not leave its synthetic listener behind.
            os.kill(child, signal.SIGKILL)


def _alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _assert_serving(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        assert connection.recv(5) == b"alive"


def test_abrupt_owner_death_closes_detached_listener_without_stopping_parallel_serve(tmp_path):
    with _session(tmp_path / "subject") as (owner, child, port), _session(tmp_path / "other") as other:
        _assert_serving(port)
        _assert_serving(other[2])
        owner.kill()
        owner.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not _alive(child)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        _assert_serving(other[2])


def test_owner_already_gone_prevents_guard_launch(tmp_path):
    launched = tmp_path / "unexpected"
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    os.close(write_fd)
    try:
        process = subprocess.run(
            [sys.executable, "-I", str(Path(serve_process_guard.__file__)), str(read_fd), "--",
             sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(launched)],
            pass_fds=(read_fd,), timeout=5, check=False,
        )
        assert process.returncode == 130
        assert not launched.exists()
    finally:
        os.close(read_fd)


def test_lifetime_eof_during_spawn_cleans_started_process(monkeypatch):
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    try:
        def spawn(*args, **kwargs):
            os.close(write_fd)
            return child

        monkeypatch.setattr(serve_process_guard.subprocess, "Popen", spawn)
        monkeypatch.setattr(serve_process_guard, "_become_subreaper", lambda: None)
        monkeypatch.setattr(serve_process_guard, "_reap_owned_children", lambda: None)
        monkeypatch.setattr(sys, "argv", ["guard", str(read_fd), "--", "synthetic-rcc"])
        assert serve_process_guard.main() == 130
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_guard_preserves_child_exit_status(tmp_path):
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        process = subprocess.run(
            [sys.executable, "-I", str(Path(serve_process_guard.__file__)), str(read_fd), "--",
             sys.executable, "-c", "raise SystemExit(19)"],
            pass_fds=(read_fd,), timeout=5, check=False,
        )
        assert process.returncode == 19
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_packaged_python_without_os_binding_uses_available_libc(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    descriptor = serve_process_guard._open_pidfd(os.getpid())
    try:
        assert not os.get_inheritable(descriptor)
        assert os.fstat(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("abrupt", [False, True], ids=["rcc-exits", "rcc-killed"])
def test_rcc_exit_reaps_existing_detached_listener_and_releases_output_pipes(tmp_path, abrupt):
    ready = tmp_path / "rcc-exit"
    with _session(ready, _RCC_EXITS) as (owner, child, port):
        _assert_serving(port)
        if abrupt:
            os.kill(int(Path(str(ready) + ".rcc").read_text()), signal.SIGKILL)
        else:
            Path(str(ready) + ".stop").touch()
        status = owner.wait(timeout=5)
        assert status == (247 if abrupt else 23)
        assert not _alive(child)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_existing_listener_reparented_during_cleanup_cannot_escape_subreaper(tmp_path):
    ready = tmp_path / "reparented"
    middle = """
import os, subprocess, sys, time
from pathlib import Path
Path(sys.argv[2] + '.middle').write_text(str(os.getpid()))
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], start_new_session=True)
while not Path(sys.argv[2] + '.middle-stop').exists(): time.sleep(.001)
"""
    rcc = """
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]], start_new_session=True)
while not Path(sys.argv[3] + '.rcc-stop').exists(): time.sleep(.001)
sys.exit(23)
"""
    wrapper = f"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, {str(Path(serve_process_guard.__file__).resolve().parents[1])!r})
from josh_room import cancellation, serve_process_guard
original = cancellation._task_children
ready = Path({str(ready)!r})
def children(pid):
    result = original(pid)
    if pid == os.getpid() and not Path(str(ready) + '.seam').exists():
        listener, _ = json.loads(ready.read_text())
        Path(str(ready) + '.middle-stop').touch()
        deadline = time.monotonic() + 3
        while True:
            raw = Path(f'/proc/{{listener}}/stat').read_text()
            if int(raw[raw.rfind(')') + 2:].split()[1]) == os.getpid(): break
            assert time.monotonic() < deadline
            time.sleep(.001)
        Path(str(ready) + '.seam').touch()
    return result
cancellation._task_children = children
raise SystemExit(serve_process_guard.main())
"""
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", wrapper, str(read_fd), "--", sys.executable,
         "-c", rcc, middle, _LISTENER, str(ready)],
        pass_fds=(read_fd,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    os.close(read_fd)
    listener = None
    intermediary = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        listener, port = json.loads(ready.read_text())
        intermediary = int(Path(str(ready) + ".middle").read_text())
        _assert_serving(port)
        Path(str(ready) + ".rcc-stop").touch()
        assert process.wait(timeout=5) == 23
        assert Path(str(ready) + ".seam").exists()
        process.communicate(timeout=1)
        assert not _alive(listener)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
    finally:
        os.close(write_fd)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pid in (listener, intermediary):
            if pid is not None and _alive(pid):
                os.kill(pid, signal.SIGKILL)
        process.communicate(timeout=2)
