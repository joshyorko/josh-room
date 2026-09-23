"""Reversible one-shot scheduler integration for PCC harvest."""
from __future__ import annotations

import hashlib
import json
import os

try:
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None

import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .policy import RepositoryIdentity


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
        repository_value = text(repository, "repository")
        if repository_value is not None:
            RepositoryIdentity.from_remote(repository_value)
        return cls(
            profile=text(profile, "profile", required=True),
            codex_active_root=absolute(codex_active_root, "Codex active root", required=True),
            codex_archived_root=absolute(codex_archived_root, "Codex archived root", required=True),
            policy_config=absolute(policy_config, "policy config"),
            config_home=absolute(config_home, "config home"),
            workspace_id=text(workspace_id, "workspace id"),
            workspace_path=absolute(workspace_path, "workspace path"),
            repository=repository_value,
            path_kind=path_kind,
            age_executable=age,
        )

    def argv(self, executable: str, context_id: str | None = None) -> list[str]:
        """Return the public scheduler command for this context."""
        identifier = context_id or _context_identifier(self, executable)
        return [
            "josh-room",
            "harvest",
            "run",
            "--scheduler-context-id",
            identifier,
            "--drain",
            "--limit",
            "100",
        ]

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
_STATE_RELATIVE = Path(".local") / "state" / "josh-room" / "scheduler"
_CONTEXT_ID_PREFIX = "ctx-"
_CONTEXT_ID_HEX_LENGTH = 32

def _context_identifier(context: SchedulerContext, executable: str) -> str:
    payload = json.dumps(
        {"context": context.to_dict(), "executable": str(Path(executable))},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _CONTEXT_ID_PREFIX + hashlib.sha256(payload).hexdigest()[:_CONTEXT_ID_HEX_LENGTH]

def _context_id(value: object) -> str:
    result = str(value)
    if (
        not result.startswith(_CONTEXT_ID_PREFIX)
        or len(result) != len(_CONTEXT_ID_PREFIX) + _CONTEXT_ID_HEX_LENGTH
        or any(char not in "0123456789abcdef" for char in result[len(_CONTEXT_ID_PREFIX) :])
    ):
        raise ValueError("scheduler context is invalid")
    return result

def _state_path(home: Path, context_id: str) -> Path:
    return home / _STATE_RELATIVE / f"{_context_id(context_id)}.json"

def _manifest_path(home: Path) -> Path:
    return home / ".config" / "josh-room" / "pcc-harvest.scheduler.json"

def _write_context(
    home: Path,
    context_id: str,
    platform_name: str,
    executable: str,
    interval: int,
    context: SchedulerContext,
) -> bool:
    path = _state_path(home, context_id)
    body = json.dumps(
        {
            "schema": SCHEMA,
            "schema_version": dict(SCHEMA_VERSION),
            "context_id": context_id,
            "platform": platform_name,
            "executable": executable,
            "executable_sha256": _executable_digest(executable),
            "interval": interval,
            "context": context.to_dict(),
        },
        sort_keys=True,
    ) + "\n"
    return _write_private(path, body)

def _load_context_state(home: Path, context_id: str) -> tuple[SchedulerContext, str]:
    path = _state_path(home, context_id)
    try:
        status = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(status.st_mode):
            raise ValueError("scheduler context is invalid")
        if os.name == "posix" and (status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o600):
            raise ValueError("scheduler context is invalid")
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema") != SCHEMA or body.get("schema_version") != SCHEMA_VERSION or body.get("context_id") != context_id:
            raise ValueError("scheduler context is invalid")
        executable = _executable(body["executable"])
        if body["executable_sha256"] != _executable_digest(executable):
            raise ValueError("scheduler context is invalid")
        context_body = body["context"]
        if not isinstance(context_body, dict):
            raise TypeError("scheduler context is invalid")
        return SchedulerContext.from_values(**context_body), executable
    except (OSError, KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("scheduler context is invalid") from error

def _runtime_executable(value: str | os.PathLike[str]) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        located = shutil.which(str(candidate))
        if located is None:
            raise ValueError("scheduler executable is not trusted")
        candidate = Path(located)
    return _executable(candidate)


def load_context(context_id: str, *, home: Path | None = None, executable: str | os.PathLike[str] | None = None) -> SchedulerContext:
    """Load and validate an installed scheduler context without exposing it."""
    selected_home = _trusted_home(home)
    context, stored_executable = _load_context_state(selected_home, _context_id(context_id))
    if executable is not None and _runtime_executable(executable) != stored_executable:
        raise ValueError("scheduler executable is not trusted")
    return context

def launch_context(
    context_id: str,
    *,
    home: Path | None = None,
    current_executable: str | os.PathLike[str] | None = None,
    argv: list[str] | None = None,
) -> SchedulerContext:
    selected_home = _trusted_home(home)
    _launcher(selected_home)
    context, stored_executable = _load_context_state(selected_home, _context_id(context_id))
    runtime_executable = _runtime_executable(current_executable or sys.argv[0])
    if runtime_executable != stored_executable:
        os.execv(stored_executable, [stored_executable, *(sys.argv[1:] if argv is None else argv)])
    return context



def _write_manifest(home: Path, platform_name: str, executable: str, interval: int, context_id: str, context: SchedulerContext) -> bool:
    path = _manifest_path(home)
    body = json.dumps(
        {
            "platform": platform_name,
            "context_id": context_id,
            "interval": interval,
            "executable_name": Path(executable).name,
            "executable_sha256": _executable_digest(executable),
            "argv": context.argv(executable, context_id),
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
def _launcher(home: Path) -> str:
    return _executable(home / ".local" / "bin" / "josh-room")
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


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("scheduler path is unavailable")
    if os.name == "posix":
        os.chmod(path, 0o700)

def _write_private(path: Path, content: str) -> bool:
    _private_directory(path.parent)
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
    return value.replace("%", "%%").replace("$", "$$").replace("\\", "\\\\").replace('"', '\\"').replace(" ", "\\x20").replace("\t", "\\x09")


def _linux_content(executable: str, interval: int, context: SchedulerContext, context_id: str | None = None) -> tuple[str, str]:
    argv = context.argv(executable, context_id)
    command_argv = ["%h/.local/bin/josh-room", *argv[1:]]
    command = " ".join(["/usr/bin/env", "--ignore-environment", "HOME=%h", "PATH=%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin", *(_systemd_arg(item) if index else item for index, item in enumerate(command_argv))])
    service = f"[Unit]\nDescription=Josh Room PCC harvest\nRefuseManualStart=yes\n\n[Service]\nType=oneshot\nExecStart={command}\n"
    timer = f"[Unit]\nDescription=Josh Room PCC harvest timer\n\n[Timer]\nOnBootSec=5min\nOnUnitActiveSec={interval}s\nPersistent=true\nUnit=josh-room-pcc-harvest.service\n\n[Install]\nWantedBy=timers.target\n"
    return service, timer

def _xml_arg(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
def _mac_content(executable: str, interval: int, home: Path, context: SchedulerContext, context_id: str | None = None) -> str:
    del home
    argv = context.argv(executable, context_id)
    launcher = ["/bin/sh", "-c", 'exec "$HOME/.local/bin/josh-room" "$@"', "josh-room", *argv[1:]]
    arguments = "".join(f"<string>{_xml_arg(value)}</string>" for value in launcher)
    path_value = "$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin"
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.josh-room.pcc-harvest</string>
  <key>ProgramArguments</key><array>{arguments}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_value}</string></dict>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>60</integer>
</dict></plist>
'''

def _windows_command(executable: str, interval: int, context: SchedulerContext, context_id: str | None = None) -> list[str]:
    minutes = max(1, (interval + 59) // 60)
    task_command = subprocess.list2cmdline(context.argv(executable, context_id))
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
    if selected in {"windows", "unsupported"}:
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")
    try:
        _launcher(home)
    except (OSError, ValueError):
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-launcher-unavailable")
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
    try:
        context_id = _context_identifier(context, exe)
        context_changed = _write_context(home, context_id, selected, exe, interval, context)
        if selected == "linux":
            service, timer = _linux_paths(home)
            service_body, timer_body = _linux_content(exe, interval, context, context_id)
            service_changed = _write_private(service, service_body)
            timer_changed = _write_private(timer, timer_body)
            manifest_changed = _write_manifest(home, selected, exe, interval, context_id, context)
            changed = context_changed or service_changed or timer_changed or manifest_changed
            activation = _activate(selected, home, (service, timer))
            return _envelope(ok=activation != "activation-failed", action="install", platform=selected, installed=activation != "activation-failed", changed=changed, activation=activation, overlap="systemd-oneshot")
        if selected == "macos":
            path = _mac_path(home)
            changed = context_changed or _write_private(path, _mac_content(exe, interval, home, context, context_id))
            changed = _write_manifest(home, selected, exe, interval, context_id, context) or changed
            activation = _activate(selected, home, (path,))
            return _envelope(ok=activation != "activation-failed", action="install", platform=selected, installed=activation != "activation-failed", changed=changed, activation=activation, overlap="throttle-interval")
        if selected == "windows":
            process = subprocess.run(_windows_command(exe, interval, context, context_id), capture_output=True, text=True, check=False)
            if process.returncode != 0:
                return _envelope(ok=False, action="install", platform=selected, installed=False, changed=False, task=TASK_NAME, error="scheduler-install-failed")
            changed = context_changed or _write_manifest(home, selected, exe, interval, context_id, context)
            return _envelope(ok=True, action="install", platform=selected, installed=True, changed=changed, task=TASK_NAME, overlap="task-single-instance")
    except OSError:
        return _envelope(ok=False, action="install", platform=selected, error="scheduler-unavailable")
    return _envelope(ok=False, action="install", platform=selected, error="scheduler-unsupported-platform")

def _manifest_state(home: Path) -> tuple[bool, bool]:
    path = _manifest_path(home)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        context_id = _context_id(body["context_id"])
        context, executable = _load_context_state(home, context_id)
        argv = body["argv"]
        if not isinstance(argv, list) or argv != context.argv(executable, context_id):
            return False, False
        digest = body["executable_sha256"]
        return True, digest == _executable_digest(executable)
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return False, False
def _manifest_context_id(home: Path) -> str | None:
    try:
        body = json.loads(_manifest_path(home).read_text(encoding="utf-8"))
        return _context_id(body["context_id"])
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
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
        return _envelope(ok=installed and fresh and active, action="status", platform=selected, installed=installed, active=active, stale=installed and not fresh)
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
        return _envelope(ok=installed and fresh and active, action="status", platform=selected, installed=installed, active=active, stale=installed and not fresh)
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
    manifest = _manifest_path(home)
    context_id = _manifest_context_id(home)
    state = _state_path(home, context_id) if context_id is not None else None
    if selected == "linux":
        paths = _linux_paths(home)
        for path in paths:
            if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
                return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        existed = any(path.exists() for path in paths) or manifest.exists() or (state is not None and state.exists())
        activation = _deactivate(selected, home)
        if activation == "deactivation-failed":
            return _envelope(ok=False, action="remove", platform=selected, removed=False, changed=False, activation=activation, error="scheduler-deactivation-failed")
        targets = (*paths, manifest) + ((state,) if state is not None else ())
        for path in targets:
            path.unlink(missing_ok=True)
        return _envelope(ok=True, action="remove", platform=selected, removed=True, changed=existed, activation=activation)
    if selected == "macos":
        path = _mac_path(home)
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-path-unavailable")
        existed = path.exists() or manifest.exists() or (state is not None and state.exists())
        activation = _deactivate(selected, home)
        if activation == "deactivation-failed":
            return _envelope(ok=False, action="remove", platform=selected, removed=False, changed=False, activation=activation, error="scheduler-deactivation-failed")
        targets = (path, manifest) + ((state,) if state is not None else ())
        for target in targets:
            target.unlink(missing_ok=True)
        return _envelope(ok=True, action="remove", platform=selected, removed=True, changed=existed, activation=activation)
    if selected == "windows":
        if os.name != "nt":
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")
        try:
            process = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True, check=False)
        except OSError:
            return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unavailable")
        if process.returncode == 0:
            targets = (manifest,) + ((state,) if state is not None else ())
            for target in targets:
                target.unlink(missing_ok=True)
        return _envelope(ok=process.returncode == 0, action="remove", platform=selected, removed=process.returncode == 0, changed=process.returncode == 0, task=TASK_NAME, **({} if process.returncode == 0 else {"error": "scheduler-remove-failed"}))
    return _envelope(ok=False, action="remove", platform=selected, error="scheduler-unsupported-platform")

__all__ = ["SCHEMA", "SCHEMA_VERSION", "SchedulerContext", "install", "launch_context", "load_context", "remove", "status"]
