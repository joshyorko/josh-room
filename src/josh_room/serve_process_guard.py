"""Foreground Linux Serve lifetime guard; no persistent service or state."""

import os
import select
import stat
import subprocess
import sys
from pathlib import Path

# Executed by the controller's interpreter with -I, independent of JAT's cwd
# and PYTHONPATH. Only this shipped package supplies the cleanup implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from josh_room.cancellation import terminate_owned_process


def main():
    child = None
    pidfd = None
    lifetime = None
    try:
        if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
            raise RuntimeError("Linux pidfd supervision is unavailable")
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            raise ValueError("Invalid Serve lifetime invocation")
        lifetime = int(sys.argv[1])
        if lifetime < 3 or not stat.S_ISFIFO(os.fstat(lifetime).st_mode):
            raise ValueError("Invalid Serve lifetime descriptor")
        os.set_inheritable(lifetime, False)
        probe = os.pidfd_open(os.getpid())
        os.close(probe)
        if select.select([lifetime], [], [], 0)[0]:
            return 130
        child = subprocess.Popen(sys.argv[3:], start_new_session=True, close_fds=True)
        pidfd = os.pidfd_open(child.pid)
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
        if pidfd is not None:
            os.close(pidfd)
        if lifetime is not None:
            os.close(lifetime)


if __name__ == "__main__":
    raise SystemExit(main())
