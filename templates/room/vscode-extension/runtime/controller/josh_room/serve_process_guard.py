"""Foreground Linux Serve lifetime guard; no persistent service or state."""

import ctypes
import errno
import os
import select
import stat
import subprocess
import sys
from pathlib import Path

# Executed by the controller's interpreter with -I, independent of JAT's cwd
# and PYTHONPATH. Only this shipped package supplies the cleanup implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from josh_room.cancellation import terminate_owned_descendants, terminate_owned_process


def _open_pidfd(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some packaged CPython builds omit the os binding despite kernel/libc
    # support. Use the same checked libc ABI, never guessed syscall numbers.
    function = getattr(ctypes.CDLL(None, use_errno=True), "pidfd_open", None)
    if function is None:
        raise OSError(errno.ENOSYS, "pidfd supervision unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "pidfd supervision unavailable")
    os.set_inheritable(descriptor, False)
    return descriptor


def _become_subreaper():
    function = getattr(ctypes.CDLL(None, use_errno=True), "prctl", None)
    if function is None:
        raise OSError(errno.ENOSYS, "Serve child adoption unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    function.restype = ctypes.c_int
    if function(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "Serve child adoption unavailable")


def _reap_owned_children():
    # A single /proc traversal is not stable while an intermediary exits.
    # As subreaper, ECHILD is the kernel proof that no owned descendants remain.
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            terminate_owned_descendants(os.getpid())


def main():
    child = None
    pidfd = None
    lifetime = None
    try:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Linux pidfd supervision is unavailable")
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            raise ValueError("Invalid Serve lifetime invocation")
        lifetime = int(sys.argv[1])
        if lifetime < 3 or not stat.S_ISFIFO(os.fstat(lifetime).st_mode):
            raise ValueError("Invalid Serve lifetime descriptor")
        os.set_inheritable(lifetime, False)
        probe = _open_pidfd(os.getpid())
        os.close(probe)
        _become_subreaper()
        if select.select([lifetime], [], [], 0)[0]:
            return 130
        child = subprocess.Popen(sys.argv[3:], start_new_session=True, close_fds=True)
        pidfd = _open_pidfd(child.pid)
        while True:
            ready, _, _ = select.select([lifetime, pidfd], [], [])
            if lifetime in ready:
                terminate_owned_process(child)
                return 130
            if pidfd in ready:
                return child.wait()
    except (OSError, RuntimeError, TypeError, ValueError):
        print("Foreground Serve supervision is unavailable", file=sys.stderr)
        return 125
    finally:
        if child is not None and child.poll() is None:
            terminate_owned_process(child)
        if child is not None:
            _reap_owned_children()
        if pidfd is not None:
            os.close(pidfd)
        if lifetime is not None:
            os.close(lifetime)


if __name__ == "__main__":
    raise SystemExit(main())
