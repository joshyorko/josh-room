"""Reversible one-shot scheduler integration for PCC harvest."""
from __future__ import annotations

import hashlib
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = "josh-room.scheduler"
SCHEMA_VERSION = {"major": 1, "minor": 0}
TASK_NAME = "JoshRoomPccHarvest"


def _home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


def _executable(value: str | os.PathLike[str] | None = None) -> str:
    candidate = Path(value) if value is not None else Path(sys.argv[0])
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in str(candidate)):
        raise ValueError("scheduler executable contains controls")
    if not candidate.is_absolute():
        raise ValueError("scheduler executable must be absolute")
    status = candidate.lstat()
    if candidate.is_symlink() or not stat.S_ISREG(status.st_mode) or not (status.st_mode & 0o111):
        raise ValueError("scheduler executable is not trusted")
    if status.st_mode & 0o022 or (os.name == "posix" and status.st_uid != os.getuid()):
        raise ValueError("scheduler executable is not trusted")
    return str(candidate)
def _executable_digest(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _envelope(*, ok: bool, action: str, **body: object) -> dict[str, object]:
    result = {"schema": SCHEMA, "schema_version": dict(SCHEMA_VERSION), "ok": bool(ok), "action": action}
    result.update(body)
    return result


def _trusted_home(value: Path | None) -> Path:
    home = Path(value) if value is not None else _home()
    if not home.is_absolute() or home.is_symlink() or (home.exists() and not home.is_dir()):
        raise ValueError("scheduler home is unavailable")
    return home


def _platform(value: str | None = None) -> str:
    name = (value or sys.platform).lower()
    if name.startswith("linux"):
        return "linux"
    if name == "darwin":
        return "macos"
    if name.startswith("win"):
        return "windows"
    return "unsupported"
def _manifest_path(home: Path) -> Path:
    return home / ".config" / "josh-room" / "pcc-harvest.scheduler.json"

def _write_manifest(home: Path, platform_name: str, executable: str, interval: int) -> bool:
    path = _manifest_path(home)
    body = json.dumps({"platform": platform_name, "executable": executable, "executable_sha256": _executable_digest(executable), "interval": interval}, sort_keys=True) + "\n"
    return _write_private(path, body)

def _linux_paths(home: Path) -> tuple[Path, Path]:
    base = home / ".config" / "systemd" / "user"
    return base / "josh-room-pcc-harvest.service", base / "josh-room-pcc-harvest.timer"



def _mac_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / "dev.josh-room.pcc-harvest.plist"


def _write_private(path: Path, content: str) -> bool:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
        raise OSError("scheduler path is unavailable")
    try:
        if path.read_text(encoding="utf-8") == content and stat.S_IMODE(path.lstat().st_mode) == 0o600:
            return False
    except (OSError, UnicodeError):
        pass
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)



def _systemd_arg(value: str) -> str:
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ValueError("scheduler executable contains controls")
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace(" ", "\\x20").replace("\t", "\\x09")


def _linux_content(executable: str, interval: int) -> tuple[str, str]:
    command = f"/usr/bin/env --ignore-environment HOME=%h PATH=/usr/bin:/bin {_systemd_arg(executable)} harvest drain --limit 100"
    service = f"[Unit]\nDescription=Josh Room PCC harvest\nRefuseManualStart=yes\n\n[Service]\nType=oneshot\nExecStart={command}\n"
    timer = f"[Unit]\nDescription=Josh Room PCC harvest timer\n\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec={interval}s\nPersistent=true\nUnit=josh-room-pcc-harvest.service\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def _mac_content(executable: str, interval: int, home: Path) -> str:
    escaped = executable.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    home_value = str(home).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.josh-room.pcc-harvest</string>
  <key>ProgramArguments</key><array><string>{escaped}</string><string>harvest</string><string>drain</string><string>--limit</string><string>100</string></array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>{home_value}</string><key>PATH</key><string>/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>60</integer>
</dict></plist>
'''


def _windows_command(executable: str, interval: int) -> list[str]:
    minutes = max(1, (interval + 59) // 60)
    task_command = subprocess.list2cmdline([executable, "harvest", "drain", "--limit", "100"])
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(minutes), "/TR", task_command, "/F"]


def install(*, interval: int = 900, executable: str | os.PathLike[str] | None = None, platform_name: str | None = None, home: Path | None = None) -> dict[str, object]:
    if type(interval) is not int or not 60 <= interval <= 86400:
        return _envelope(ok=False, action="install", error="invalid-interval")
    try:
        selected, home, exe = _platform(platform_name), _trusted_home(home), _executable(executable)
    except (OSError, ValueError):
        return _envelope(ok=False, action="install", error="scheduler-path-invalid")
    if selected == "linux":
        service, timer = _linux_paths(home)
        service_body, timer_body = _linux_content(exe, interval)
        service_changed = _write_private(service, service_body)
        timer_changed = _write_private(timer, timer_body)
        changed = service_changed or timer_changed or _write_manifest(home, selected, exe, interval)
        return _envelope(ok=True, action="install", platform=selected, installed=True, changed=changed, files=[str(service), str(timer)], executable=exe, executable_sha256=_executable_digest(exe), overlap="systemd-oneshot")
    if selected == "macos":
        path = _mac_path(home)
        changed = _write_private(path, _mac_content(exe, interval, home))
        changed = changed or _write_manifest(home, selected, exe, interval)
        return _envelope(ok=True, action="install", platform=selected, installed=True, changed=changed, files=[str(path)], executable=exe, executable_sha256=_executable_digest(exe), overlap="throttle-interval")
    if selected == "windows":
        try:
            process = subprocess.run(_windows_command(exe, interval), capture_output=True, text=True, check=False)
        except OSError:
            return _envelope(ok=False, action="install", platform=selected, error="scheduler-unavailable")
        return _envelope(ok=process.returncode == 0, action="install", platform=selected, installed=process.returncode == 0, changed=process.returncode == 0, task=TASK_NAME, executable=exe, executable_sha256=_executable_digest(exe), overlap="task-single-instance", **({} if process.returncode == 0 else {"error": "scheduler-install-failed"}))
    return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")

def _manifest_state(home: Path) -> tuple[bool, bool]:
    path = _manifest_path(home)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        executable = body["executable"]
        digest = body["executable_sha256"]
        return True, _executable_digest(executable) == digest
    except (OSError, KeyError, TypeError, ValueError):
        return False, False

def status(*, platform_name: str | None = None, home: Path | None = None) -> dict[str, object]:
    try:
        selected, home = _platform(platform_name), _trusted_home(home)
    except (OSError, ValueError):
        return _envelope(ok=False, action="status", error="scheduler-path-invalid")
    if selected == "linux":
        paths = _linux_paths(home)
        manifest, fresh = _manifest_state(home)
        installed = all(path.is_file() and not path.is_symlink() for path in paths) and manifest
        return _envelope(ok=installed and fresh, action="status", platform=selected, installed=installed, stale=installed and not fresh, files=[str(path) for path in paths])
    if selected == "macos":
        path = _mac_path(home)
        manifest, fresh = _manifest_state(home)
        installed = path.is_file() and not path.is_symlink() and manifest
        return _envelope(ok=installed and fresh, action="status", platform=selected, installed=installed, stale=installed and not fresh, files=[str(path)])
    if selected == "windows":
        try:
            process = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, check=False)
        except OSError:
            return _envelope(ok=False, action="status", platform=selected, error="scheduler-unavailable")
        return _envelope(ok=process.returncode == 0, action="status", platform=selected, installed=process.returncode == 0, task=TASK_NAME, **({} if process.returncode == 0 else {"error": "scheduler-not-installed"}))
    return _envelope(ok=False, action="status", platform=selected, error="scheduler-unsupported-platform")


def remove(*, platform_name: str | None = None, home: Path | None = None) -> dict[str, object]:
    try:
        selected, home = _platform(platform_name), _trusted_home(home)
    except (OSError, ValueError):
        return _envelope(ok=False, action="remove", error="scheduler-path-invalid")
    if selected == "linux":
        paths = _linux_paths(home)
        for path in paths:
            if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
                return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        existed = any(path.exists() for path in paths)
        for path in paths:
            path.unlink(missing_ok=True)
        return _envelope(ok=True, action="remove", platform=selected, removed=True, changed=existed, files=[str(path) for path in paths])
    if selected == "macos":
        path = _mac_path(home)
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        existed = path.exists()
        path.unlink(missing_ok=True)
        return _envelope(ok=True, action="remove", platform=selected, removed=True, changed=existed, files=[str(path)])
    if selected == "windows":
        try:
            process = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, check=False)
        except OSError:
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unavailable")
        return _envelope(ok=process.returncode == 0, action="remove", platform=selected, removed=process.returncode == 0, changed=process.returncode == 0, task=TASK_NAME, **({} if process.returncode == 0 else {"error": "scheduler-remove-failed"}))
    return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")


__all__ = ["SCHEMA", "SCHEMA_VERSION", "install", "remove", "status"]
