"""Exercise real unmanaged writer processes against the Linux capture barrier."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room.native_agent_quiescence import QuiescenceError, read_barrier

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux file leases only")


def source(tmp_path):
    path = tmp_path / "native.jsonl"
    path.write_bytes(b"synthetic native state")
    info = path.stat()
    return path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def test_unmanaged_existing_writer_blocks_barrier(tmp_path):
    path, info = source(tmp_path)
    script = "import os,sys; f=open(sys.argv[1],'ab'); print('ready',flush=True); sys.stdin.read()"
    child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(QuiescenceError, match="writer-active"), read_barrier([(path, info)]):
            pytest.fail("an unmanaged open writer must prevent capture")
    finally:
        child.communicate("stop", timeout=5)


def test_new_unmanaged_write_open_is_refused_and_invalidates_capture(tmp_path):
    path, info = source(tmp_path)
    old_handler = signal.getsignal(signal.SIGIO)
    with pytest.raises(QuiescenceError, match="writer-active"), read_barrier([(path, info)]) as barrier:
        script = "import os,sys; os.open(sys.argv[1],os.O_WRONLY|os.O_NONBLOCK)"
        child = subprocess.run([sys.executable, "-c", script, str(path)], check=False, capture_output=True, timeout=5)
        assert child.returncode != 0
        assert path.read_bytes() == b"synthetic native state"
        barrier.check()
    assert signal.getsignal(signal.SIGIO) == old_handler
    with path.open("ab") as output:
        output.write(b" after barrier")


def test_unchanged_sources_are_readable_and_lease_is_released(tmp_path):
    path, info = source(tmp_path)
    with read_barrier([(path, info)]) as barrier:
        assert path.read_bytes() == b"synthetic native state"
        barrier.check()
    with path.open("ab") as output:
        output.write(b" after capture")


def test_unknown_filesystem_or_kernel_fails_closed(tmp_path, monkeypatch):
    import fcntl

    path, info = source(tmp_path)
    actual = fcntl.fcntl

    def unavailable(fd, command, *args):
        if command == fcntl.F_SETLEASE:
            raise OSError("unsupported synthetic filesystem")
        return actual(fd, command, *args)

    monkeypatch.setattr(fcntl, "fcntl", unavailable)
    with pytest.raises(QuiescenceError, match="unavailable"), read_barrier([(path, info)]):
        pytest.fail("unsupported leases must refuse capture")


def test_cancelled_operation_releases_kernel_leases(tmp_path):
    path, info = source(tmp_path)
    before = signal.getsignal(signal.SIGIO)
    with pytest.raises(KeyboardInterrupt), read_barrier([(path, info)]):
        raise KeyboardInterrupt
    assert signal.getsignal(signal.SIGIO) == before
    with path.open("ab") as output:
        output.write(b" released after cancellation")


def test_sigkill_releases_kernel_barrier_without_plaintext_staging(tmp_path):
    import select

    path, _info = source(tmp_path)
    before = set(tmp_path.iterdir())
    script = """import os,signal,sys
from pathlib import Path
from josh_room.native_agent_quiescence import read_barrier
path=Path(sys.argv[1]); info=path.stat()
expected=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
with read_barrier([(path,expected)]):
    print('ready',flush=True)
    signal.pause()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == b"ready"
        child.kill()
        child.wait(timeout=5)
        with path.open("ab") as output:
            output.write(b" after killed capture")
        assert set(tmp_path.iterdir()) == before
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()
