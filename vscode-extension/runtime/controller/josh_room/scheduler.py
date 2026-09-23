"""Reversible one-shot scheduler integration for PCC harvest."""
from __future__ import annotations

import hashlib
import json
import os

try:
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

def _home() -> Path:
    if pwd is None:
        return Path.home()
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


_PATH_KINDS = frozenset({"directory", "worktree", "remote", "wsl", "symlink", "unknown"})


@dataclass(frozen=True, slots=True)
class SchedulerContext:
    profile: str
    codex_active_root: str
    codex_archived_root: str
    policy_config: str | None = None
    config_home: str | None = None
    workspace_id: str | None = None
    workspace_path: str | None = None
    repository: str | None = None
    path_kind: str = "unknown"
    age_executable: str | None = None

    @classmethod
    def from_values(
        cls,
        *,
        profile: str | None,
        codex_active_root: Path | str | None,
        codex_archived_root: Path | str | None,
        policy_config: Path | str | None = None,
        config_home: Path | str | None = None,
        workspace_id: str | None = None,
        workspace_path: Path | str | None = None,
        repository: str | None = None,
        path_kind: str = "unknown",
        age_executable: Path | str | None = None,
    ) -> SchedulerContext:
        def text(value: object, name: str, *, required: bool = False) -> str | None:
            if value is None:
                if required:
                    raise ValueError(f"scheduler {name} is required")
                return None
            result = str(value)
            if not result or any(ord(char) < 0x20 or ord(char) == 0x7f for char in result):
                raise ValueError(f"scheduler {name} is invalid")
            return result

        def absolute(value: Path | str | None, name: str, *, required: bool = False) -> str | None:
            result = text(value, name, required=required)
            if result is None:
                return None
            if not Path(result).is_absolute():
                raise ValueError(f"scheduler {name} must be absolute")
            return result

        if path_kind not in _PATH_KINDS:
            raise ValueError("scheduler path kind is invalid")
        age = absolute(age_executable, "age executable")
        if age is not None:
            age = _executable(age)
        return cls(
            profile=text(profile, "profile", required=True),
            codex_active_root=absolute(codex_active_root, "Codex active root", required=True),
            codex_archived_root=absolute(codex_archived_root, "Codex archived root", required=True),
            policy_config=absolute(policy_config, "policy config"),
            config_home=absolute(config_home, "config home"),
            workspace_id=text(workspace_id, "workspace id"),
            workspace_path=absolute(workspace_path, "workspace path"),
            repository=text(repository, "repository"),
            path_kind=path_kind,
            age_executable=age,
        )

    def argv(self, executable: str) -> list[str]:
        result = [
            executable,
            "harvest",
            "run",
            "--drain",
            "--limit",
            "100",
            "--profile",
            self.profile,
            "--codex-active-root",
            self.codex_active_root,
            "--codex-archived-root",
            self.codex_archived_root,
        ]
        for option, value in (
            ("--policy-config", self.policy_config),
            ("--config-home", self.config_home),
            ("--workspace-id", self.workspace_id),
            ("--workspace-path", self.workspace_path),
            ("--repository", self.repository),
        ):
            if value is not None:
                result.extend((option, value))
        result.extend(("--path-kind", self.path_kind))
        if self.age_executable is not None:
            result.extend(("--age-executable", self.age_executable))
        return result

    def to_dict(self) -> dict[str, str | None]:
        return {
            "profile": self.profile,
            "codex_active_root": self.codex_active_root,
            "codex_archived_root": self.codex_archived_root,
            "policy_config": self.policy_config,
            "config_home": self.config_home,
            "workspace_id": self.workspace_id,
            "workspace_path": self.workspace_path,
            "repository": self.repository,
            "path_kind": self.path_kind,
            "age_executable": self.age_executable,
        }

SCHEMA = "josh-room.scheduler"
SCHEMA_VERSION = {"major": 1, "minor": 0}
TASK_NAME = "JoshRoomPccHarvest"
def _manifest_path(home: Path) -> Path:
    return home / ".config" / "josh-room" / "pcc-harvest.scheduler.json"


def _write_manifest(home: Path, platform_name: str, executable: str, interval: int, context: SchedulerContext) -> bool:
    path = _manifest_path(home)
    body = json.dumps(
        {
            "platform": platform_name,
            "executable": executable,
            "executable_sha256": _executable_digest(executable),
            "interval": interval,
            "context": context.to_dict(),
            "argv": context.argv(executable),
        },
        sort_keys=True,
    ) + "\n"
    return _write_private(path, body)



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

def _activate(platform_name: str, home: Path, files: tuple[Path, ...]) -> str:
    if home != _home():
        return "not-attempted"
    try:
        if platform_name == "linux":
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "josh-room-pcc-harvest.timer"], check=True, capture_output=True)
            return "active"
        if platform_name == "macos":
            uid = str(os.getuid())
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(files[0])], check=True, capture_output=True)
            return "active"
    except (OSError, subprocess.CalledProcessError):
        return "activation-failed"
def _deactivate(platform_name: str, home: Path) -> str:
    if home != _home():
        return "not-attempted"
    try:
        if platform_name == "linux":
            subprocess.run(["systemctl", "--user", "disable", "--now", "josh-room-pcc-harvest.timer"], check=True, capture_output=True)
            return "inactive"
        if platform_name == "macos":
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/dev.josh-room.pcc-harvest"], check=True, capture_output=True)
            return "inactive"
    except subprocess.CalledProcessError:
        return "inactive"
    except OSError:
        return "deactivation-failed"
    return "unsupported"

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
        raise ValueError("scheduler argument contains controls")
    return value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace(" ", "\\x20").replace("\t", "\\x09")


def _linux_content(executable: str, interval: int, context: SchedulerContext) -> tuple[str, str]:
    argv = context.argv(executable)
    command = " ".join(["/usr/bin/env", "--ignore-environment", "HOME=%h", "PATH=/usr/bin:/bin", *(_systemd_arg(item) for item in argv)])
    service = f"[Unit]\nDescription=Josh Room PCC harvest\nRefuseManualStart=yes\n\n[Service]\nType=oneshot\nExecStart={command}\n"
    timer = f"[Unit]\nDescription=Josh Room PCC harvest timer\n\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec={interval}s\nPersistent=true\nUnit=josh-room-pcc-harvest.service\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer


def _xml_arg(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _mac_content(executable: str, interval: int, home: Path, context: SchedulerContext) -> str:
    arguments = "".join(f"<string>{_xml_arg(value)}</string>" for value in context.argv(executable))
    home_value = _xml_arg(str(home))
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.josh-room.pcc-harvest</string>
  <key>ProgramArguments</key><array>{arguments}</array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>{home_value}</string><key>PATH</key><string>/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>60</integer>
</dict></plist>
'''


def _windows_command(executable: str, interval: int, context: SchedulerContext) -> list[str]:
    minutes = max(1, (interval + 59) // 60)
    task_command = subprocess.list2cmdline(context.argv(executable))
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(minutes), "/TR", task_command, "/F"]



def install(
    *,
    interval: int = 900,
    executable: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    home: Path | None = None,
    profile: str | None = None,
    codex_active_root: Path | None = None,
    codex_archived_root: Path | None = None,
    policy_config: Path | None = None,
    config_home: Path | None = None,
    workspace_id: str | None = None,
    workspace_path: str | None = None,
    repository: str | None = None,
    path_kind: str = "unknown",
    age_executable: Path | None = None,
) -> dict[str, object]:
    if type(interval) is not int or not 60 <= interval <= 86400:
        return _envelope(ok=False, action="install", error="invalid-interval")
    try:
        selected, home = _platform(platform_name), _trusted_home(home)
    except (OSError, ValueError):
        return _envelope(ok=False, action="install", error="scheduler-path-invalid")
    if selected == "windows":
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")
    if selected not in {"linux", "macos", "windows"}:
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")
    try:
        exe = _executable(executable)
    except (OSError, ValueError):
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-path-invalid")
    try:
        context = SchedulerContext.from_values(
            profile=profile,
            codex_active_root=codex_active_root,
            codex_archived_root=codex_archived_root,
            policy_config=policy_config,
            config_home=config_home,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            repository=repository,
            path_kind=path_kind,
            age_executable=age_executable,
        )
    except (OSError, ValueError):
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-context-invalid")
    argv = context.argv(exe)
    details = {"context": context.to_dict(), "argv": argv}
    try:
        if selected == "linux":
            service, timer = _linux_paths(home)
            service_body, timer_body = _linux_content(exe, interval, context)
            service_changed = _write_private(service, service_body)
            timer_changed = _write_private(timer, timer_body)
            manifest_changed = _write_manifest(home, selected, exe, interval, context)
            changed = service_changed or timer_changed or manifest_changed
            activation = _activate(selected, home, (service, timer))
            return _envelope(ok=activation != "activation-failed", action="install", platform=selected, installed=activation != "activation-failed", changed=changed, activation=activation, files=[str(service), str(timer)], executable=exe, executable_sha256=_executable_digest(exe), overlap="systemd-oneshot", **details)
        if selected == "macos":
            path = _mac_path(home)
            changed = _write_private(path, _mac_content(exe, interval, home, context))
            changed = _write_manifest(home, selected, exe, interval, context) or changed
            activation = _activate(selected, home, (path,))
            return _envelope(ok=activation != "activation-failed", action="install", platform=selected, installed=activation != "activation-failed", changed=changed, activation=activation, files=[str(path)], executable=exe, executable_sha256=_executable_digest(exe), overlap="throttle-interval", **details)
        if selected == "windows":
            process = subprocess.run(_windows_command(exe, interval, context), capture_output=True, text=True, check=False)
            if process.returncode != 0:
                return _envelope(ok=False, action="install", platform=selected, installed=False, changed=False, task=TASK_NAME, error="scheduler-install-failed", **details)
            changed = _write_manifest(home, selected, exe, interval, context)
            return _envelope(ok=True, action="install", platform=selected, installed=True, changed=changed, task=TASK_NAME, executable=exe, executable_sha256=_executable_digest(exe), overlap="task-single-instance", **details)
    except OSError:
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-unavailable")
    return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")

def _manifest_state(home: Path) -> tuple[bool, bool]:
    path = _manifest_path(home)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        executable = body["executable"]
        digest = body["executable_sha256"]
        context = body["context"]
        argv = body["argv"]
        if not isinstance(context, dict) or not isinstance(argv, list) or argv != SchedulerContext.from_values(**context).argv(executable):
            return False, False
        return True, _executable_digest(executable) == digest
    except (OSError, KeyError, TypeError, ValueError):
        return False, False
def status(*, platform_name: str | None = None, home: Path | None = None) -> dict[str, object]:
    try:

        selected, home = _platform(platform_name), _trusted_home(home)
    except (OSError, ValueError):
        return _envelope(ok=False, action="status", error="scheduler-path-invalid")

    if selected == "windows":
        return _envelope(ok=False, action="status", platform=selected, error="scheduler-unsupported-platform")
    if selected == "linux":
        paths = _linux_paths(home)
        manifest, fresh = _manifest_state(home)
        installed = all(path.is_file() and not path.is_symlink() for path in paths) and manifest
        active = False
        if home == _home() and installed:
            try:
                active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "josh-room-pcc-harvest.timer"], check=False).returncode == 0
            except OSError:
                active = False
        return _envelope(ok=installed and fresh and active, action="status", platform=selected, installed=installed, active=active, stale=installed and not fresh, files=[str(path) for path in paths])
    if selected == "macos":
        path = _mac_path(home)
        manifest, fresh = _manifest_state(home)
        installed = path.is_file() and not path.is_symlink() and manifest
        active = False
        if home == _home() and installed:
            try:
                active = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/dev.josh-room.pcc-harvest"], check=False, capture_output=True).returncode == 0
            except OSError:
                active = False
        return _envelope(ok=installed and fresh and active, action="status", platform=selected, installed=installed, active=active, stale=installed and not fresh, files=[str(path)])
    if selected == "windows":
        if os.name != "nt":
            return _envelope(ok=False, action="status", platform=selected, error="scheduler-unsupported-platform")
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
    if selected == "windows":
        return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")

    if selected == "linux":
        paths = _linux_paths(home)
        for path in paths:
            if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
                return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        manifest = _manifest_path(home)
        existed = any(path.exists() for path in paths) or manifest.exists()
        activation = _deactivate(selected, home)
        if activation == "deactivation-failed":
            return _envelope(ok=False, action="remove", platform=selected, removed=False, changed=False, activation=activation, error="scheduler-deactivation-failed")
        for path in (*paths, manifest):
            path.unlink(missing_ok=True)
        return _envelope(ok=activation != "deactivation-failed", action="remove", platform=selected, removed=True, changed=existed, activation=activation, files=[str(path) for path in (*paths, manifest)])
    if selected == "macos":
        path = _mac_path(home)
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        manifest = _manifest_path(home)
        existed = path.exists() or manifest.exists()
        activation = _deactivate(selected, home)
        if activation == "deactivation-failed":
            return _envelope(ok=False, action="remove", platform=selected, removed=False, changed=False, activation=activation, error="scheduler-deactivation-failed")
        path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        return _envelope(ok=True, action="remove", platform=selected, removed=True, changed=existed, activation=activation, files=[str(path), str(manifest)])
    if selected == "windows":
        if os.name != "nt":
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")
        try:
            process = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, check=False)
        except OSError:
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unavailable")
        return _envelope(ok=process.returncode == 0, action="remove", platform=selected, removed=process.returncode == 0, changed=process.returncode == 0, task=TASK_NAME, **({} if process.returncode == 0 else {"error": "scheduler-remove-failed"}))
    return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")


__all__ = ["SCHEMA", "SCHEMA_VERSION", "SchedulerContext", "install", "remove", "status"]
