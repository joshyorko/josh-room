"""Ephemeral Linux identity checks for a directly spawned loopback server."""

from __future__ import annotations

import os
import select
import stat
import subprocess
import sys
from pathlib import Path

from .cancellation import _proc_start_time

_ENVIRONMENT = ("HOME", "T3CODE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


class OwnedServerError(RuntimeError):
    pass


def _fail(reason):
    raise OwnedServerError(reason) from None


def _canonical(path, *, directory):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        _fail("owned-server-path-invalid")
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        _fail("owned-server-path-invalid")
    if not directory and not stat.S_ISREG(info.st_mode):
        _fail("owned-server-path-invalid")
    return path, (info.st_dev, info.st_ino)


def _read(path, maximum):
    with path.open("rb") as source:
        data = source.read(maximum + 1)
    if len(data) > maximum:
        _fail("owned-server-proc-invalid")
    return data


class OwnedServer:
    def __init__(self, process, *, executable, argv, cwd, environment, port):
        self.pidfd = None
        try:
            if not sys.platform.startswith("linux") or not hasattr(os, "pidfd_open"):
                _fail("owned-server-platform-unavailable")
            if not isinstance(process, subprocess.Popen) or type(port) is not int or not 1024 <= port <= 65535:
                _fail("owned-server-input-invalid")
            if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
                _fail("owned-server-input-invalid")
            self.process = process
            self.pid = process.pid
            self.start_time = _proc_start_time(self.pid)
            if self.start_time is None:
                _fail("owned-server-process-changed")
            self.executable, self.executable_identity = _canonical(executable, directory=False)
            self.cwd, self.cwd_identity = _canonical(cwd, directory=True)
            if argv[0] != str(self.executable) or process.args != argv:
                _fail("owned-server-command-mismatch")
            self.argv = tuple(argv)
            self.environment = {}
            self.paths = []
            for name in _ENVIRONMENT:
                value = environment.get(name)
                if not isinstance(value, str):
                    _fail("owned-server-environment-mismatch")
                path, identity = _canonical(value, directory=True)
                self.environment[name] = str(path)
                self.paths.append((path, identity))
            homes = [Path(self.environment[name]) for name in _ENVIRONMENT[1:]]
            if [path.name for path in homes] != ["t3", "codex", "claude"] or len({path.parent for path in homes}) != 1:
                _fail("owned-server-profile-mismatch")
            self.port = port
            self.pidfd = os.pidfd_open(self.pid, 0)
            self.check()
        except (OSError, ValueError, KeyError, TypeError):
            self.close()
            _fail("owned-server-unverifiable")
        except OwnedServerError:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    def close(self):
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None

    def check(self):
        try:
            if self.pidfd is None:
                _fail("owned-server-closed")
            poller = select.poll()
            poller.register(self.pidfd, select.POLLIN)
            if poller.poll(0) or self.process.poll() is not None or self.process.pid != self.pid or _proc_start_time(self.pid) != self.start_time:
                _fail("owned-server-process-changed")
            proc = Path("/proc") / str(self.pid)
            fields = dict(line.split(":", 1) for line in _read(proc / "status", 16384).decode().splitlines() if ":" in line)
            if int(fields["PPid"].strip()) != os.getpid() or [int(uid) for uid in fields["Uid"].split()] != [os.getuid()] * 4:
                _fail("owned-server-process-foreign")
            executable = (proc / "exe").stat()
            if (executable.st_dev, executable.st_ino) != self.executable_identity or _canonical(self.executable, directory=False)[1] != self.executable_identity:
                _fail("owned-server-executable-mismatch")
            if tuple(os.fsdecode(item) for item in _read(proc / "cmdline", 65536).rstrip(b"\0").split(b"\0")) != self.argv:
                _fail("owned-server-command-mismatch")
            cwd = (proc / "cwd").stat()
            if (cwd.st_dev, cwd.st_ino) != self.cwd_identity or os.readlink(proc / "cwd") != str(self.cwd) or _canonical(self.cwd, directory=True)[1] != self.cwd_identity:
                _fail("owned-server-workspace-mismatch")
            observed = {}
            for item in _read(proc / "environ", 262144).split(b"\0"):
                key, separator, value = item.partition(b"=")
                name = os.fsdecode(key)
                if name in _ENVIRONMENT:
                    if not separator or name in observed:
                        _fail("owned-server-environment-mismatch")
                    observed[name] = os.fsdecode(value)
            if observed != self.environment or any(_canonical(path, directory=True)[1] != identity for path, identity in self.paths):
                _fail("owned-server-environment-mismatch")
            self._listener(proc)
            if _proc_start_time(self.pid) != self.start_time or poller.poll(0):
                _fail("owned-server-process-changed")
        except (OSError, ValueError, KeyError, TypeError):
            _fail("owned-server-unverifiable")

    def _listener(self, proc):
        candidates = []
        for family in ("tcp", "tcp6"):
            for line in _read(proc / "net" / family, 1024 * 1024).decode("ascii").splitlines()[1:]:
                fields = line.split()
                address, port = fields[1].split(":")
                if fields[3] == "0A" and int(port, 16) == self.port:
                    candidates.append((family, address, fields[9]))
        if len(candidates) != 1 or candidates[0][:2] != ("tcp", "0100007F"):
            _fail("owned-server-endpoint-mismatch")
        expected = "socket:[" + candidates[0][2] + "]"
        for descriptor in (proc / "fd").iterdir():
            try:
                if os.readlink(descriptor) == expected:
                    return
            except FileNotFoundError:
                continue
        _fail("owned-server-endpoint-foreign")
