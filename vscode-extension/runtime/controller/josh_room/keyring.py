"""OS-backed secret authority.

The legacy runtime-file reader remains below for v0.1 extension compatibility,
but it is deliberately never considered a secure authority. Device enrollment
and all durable credentials use only the allowlisted native stores.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

SECURE_BACKENDS = frozenset(
    {
        "linux-secret-service",
        "macos-keychain",
        "windows-credential-locker",
    }
)
_SECRET_SERVICE_COMMAND = "secret-tool"
_KEYCHAIN_COMMAND = "security"
_WINDOWS_COMMAND = "cmdkey"
_FIELD_NAMES = frozenset({"access-key-id", "secret-access-key", "session-token", "age-identity", "receipt"})
_IDENTIFIER_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


@dataclass(frozen=True)
class BackendStatus:
    """Stable, secret-free native secret-store diagnostics."""

    backend: str | None
    platform: str
    available: bool
    locked: bool
    reason: str
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "platform": self.platform,
            "available": self.available,
            "locked": self.locked,
            "reason": self.reason,
            "diagnostics": list(self.diagnostics),
        }


class SecureBackendError(RuntimeError):
    """Raised when no approved native secret authority can be used."""


def _platform_name(platform_name: str | None = None) -> str:
    value = platform_name or sys.platform
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "macos"
    if value in {"win32", "cygwin", "msys"}:
        return "windows"
    return value


def _container_kind(environ: Mapping[str, str]) -> str | None:
    if Path("/proc/version").is_file():
        try:
            version = Path("/proc/version").read_text(errors="replace").lower()
        except OSError:
            version = ""
        if "microsoft" in version or "wsl" in version:
            return "wsl"
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return "container"
    if environ.get("container") or environ.get("CONTAINER"):
        return "container"
    return None


def _headless_diagnostics(environ: Mapping[str, str]) -> tuple[str, ...]:
    diagnostics: list[str] = []
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        diagnostics.append("headless-ssh")
    if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        diagnostics.append("headless")
    return tuple(diagnostics)


def _run(
    command: list[str],
    *,
    runner: Callable[..., object] = subprocess.run,
    input: str | None = None,
) -> object:
    """Run a store helper with secrets only on stdin, never in argv."""

    return runner(
        command,
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )


def _trusted_helper(command: str) -> bool:
    """Accept only a system helper with a non-writable resolved path."""
    path = shutil.which(command)
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
        if not any(str(resolved).startswith(prefix + "/") for prefix in ("/bin", "/usr/bin", "/usr/local/bin")):
            return False
        info = resolved.stat()
        if stat.S_IMODE(info.st_mode) & 0o022:
            return False
    except OSError:
        return str(path).startswith(("/bin/", "/usr/bin/", "/usr/local/bin/"))
    return True

def _probe_linux(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., object],
) -> BackendStatus:
    diagnostics = list(_headless_diagnostics(environ))
    container = _container_kind(environ)
    if container:
        diagnostics.append(container)
    if not _trusted_helper(_SECRET_SERVICE_COMMAND):
        diagnostics.append("secret-tool-missing")
        return BackendStatus("linux-secret-service", "linux", False, False, "helper-missing", tuple(diagnostics))
    if not environ.get("DBUS_SESSION_BUS_ADDRESS"):
        if container == "wsl":
            reason = "wsl-session-bus-missing"
        elif container == "container":
            reason = "container-session-bus-missing"
        elif "headless-ssh" in diagnostics:
            reason = "headless-ssh-session-bus-missing"
        else:
            reason = "session-bus-missing"
        diagnostics.append("dbus-session-missing")
        return BackendStatus("linux-secret-service", "linux", False, False, reason, tuple(diagnostics))
    try:
        result = _run(
            [_SECRET_SERVICE_COMMAND, "lookup", "service", "josh-room-device-probe", "probe", "unavailable"],
            runner=runner,
        )
    except OSError:
        diagnostics.append("secret-tool-exec-failed")
        return BackendStatus("linux-secret-service", "linux", False, False, "helper-failed", tuple(diagnostics))
    stderr = str(getattr(result, "stderr", "") or "").lower()
    returncode = int(getattr(result, "returncode", 1))
    if any(token in stderr for token in ("autolaunch", "cannot connect", "no such file", "dbus")):
        diagnostics.append("dbus-unavailable")
        return BackendStatus("linux-secret-service", "linux", False, False, "session-bus-unavailable", tuple(diagnostics))
    if any(token in stderr for token in ("locked", "access denied", "permission denied")):
        diagnostics.append("secret-service-locked")
        return BackendStatus("linux-secret-service", "linux", False, True, "locked", tuple(diagnostics))
    if returncode == 0 or "no such secret" in stderr or "not found" in stderr:
        diagnostics.append("secret-service-reachable")
        return BackendStatus("linux-secret-service", "linux", True, False, "available", tuple(diagnostics))
    diagnostics.append("secret-service-error")
    return BackendStatus("linux-secret-service", "linux", False, False, "probe-failed", tuple(diagnostics))


def _probe_macos(
    environ: Mapping[str, str],
    *,
    runner: Callable[..., object],
) -> BackendStatus:
    diagnostics = list(_headless_diagnostics(environ))
    if not _trusted_helper(_KEYCHAIN_COMMAND):
        diagnostics.append("security-helper-missing")
        return BackendStatus("macos-keychain", "macos", False, False, "helper-missing", tuple(diagnostics))
    try:
        result = _run(
            [_KEYCHAIN_COMMAND, "find-generic-password", "-s", "josh-room-device-probe", "-a", "josh-room"],
            runner=runner,
        )
    except OSError:
        diagnostics.append("security-helper-exec-failed")
        return BackendStatus("macos-keychain", "macos", False, False, "helper-failed", tuple(diagnostics))
    stderr = str(getattr(result, "stderr", "") or "").lower()
    if any(token in stderr for token in ("locked", "errsecinteractionnotallowed", "user interaction")):
        diagnostics.append("keychain-locked")
        return BackendStatus("macos-keychain", "macos", False, True, "locked", tuple(diagnostics))
    if "could not be found" in stderr or "item not found" in stderr:
        diagnostics.append("keychain-reachable")
        return BackendStatus("macos-keychain", "macos", True, False, "available", tuple(diagnostics))
    if int(getattr(result, "returncode", 1)) in {0, 44}:
        diagnostics.append("keychain-reachable")
        return BackendStatus("macos-keychain", "macos", True, False, "available", tuple(diagnostics))
    diagnostics.append("keychain-probe-failed")
    return BackendStatus("macos-keychain", "macos", False, False, "probe-failed", tuple(diagnostics))


def _probe_windows(environ: Mapping[str, str]) -> BackendStatus:
    del environ
    diagnostics: list[str] = []
    try:
        api = getattr(getattr(ctypes, "windll", None), "advapi32", None)
    except (AttributeError, OSError, TypeError):
        api = None
    required = ("CredReadW", "CredWriteW", "CredDeleteW", "CredFree")
    if api is not None and all(callable(getattr(api, name, None)) for name in required):
        diagnostics.append("credential-manager-api")
        return BackendStatus("windows-credential-locker", "windows", True, False, "available", tuple(diagnostics))
    diagnostics.append("credential-manager-api-required")
    return BackendStatus("windows-credential-locker", "windows", False, False, "api-unavailable", tuple(diagnostics))


def backend_status(
    *,
    platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    backend: str | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> BackendStatus:
    """Return truthful native-store availability without inspecting secrets."""

    environ = os.environ if environ is None else environ
    platform_value = _platform_name(platform_name)
    if backend is not None and backend not in SECURE_BACKENDS:
        return BackendStatus(None, platform_value, False, False, "unknown-backend", ("allowlist-rejected",))
    expected = {
        "linux": "linux-secret-service",
        "macos": "macos-keychain",
        "windows": "windows-credential-locker",
    }.get(platform_value)
    if expected is None:
        return BackendStatus(None, platform_value, False, False, "unsupported-platform", ("allowlist-rejected",))
    if backend is not None and backend != expected:
        return BackendStatus(expected, platform_value, False, False, "backend-platform-mismatch", ("allowlist-rejected",))
    if platform_value == "linux":
        return _probe_linux(environ, runner=runner)
    if platform_value == "macos":
        return _probe_macos(environ, runner=runner)
    return _probe_windows(environ)


def secure_available() -> bool:
    return backend_status().available


def available() -> bool:
    """Compatibility name for the secure native-store availability check.

    Runtime credential files are intentionally not accepted as availability.
    Legacy lookup still reads an explicitly selected ephemeral runtime source
    before reaching this check, preserving v0.1 operation semantics.
    """

    return secure_available()


def _require_secure_backend() -> BackendStatus:
    status = backend_status()
    if not status.available:
        detail = status.reason
        if status.diagnostics:
            detail += " (" + ",".join(status.diagnostics) + ")"
        raise SecureBackendError(f"secure secret backend unavailable: {detail}")
def _secret_attributes(profile: str, field: str) -> list[str]:
    service = "josh-room-device" if field == "receipt" else "josh-room"
    return ["service", service, "profile", _validate_identifier(profile, "profile"), "field", _validate_field(field)]

def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(character not in _IDENTIFIER_CHARS for character in value):
        raise ValueError(f"{label} is invalid")
    return value


def _validate_field(field: str) -> str:
    if field not in _FIELD_NAMES:
        raise ValueError("secret field is invalid")
    return field




def _linux_lookup(profile: str, field: str) -> str:
    result = _run([_SECRET_SERVICE_COMMAND, "lookup", *_secret_attributes(profile, field)])
    value = str(getattr(result, "stdout", "") or "").rstrip("\n")
    if int(getattr(result, "returncode", 1)) or not value:
        raise SecureBackendError("secure secret is unavailable")
    return value


def _linux_store(profile: str, field: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("secret value is invalid")
    result = _run(
        [_SECRET_SERVICE_COMMAND, "store", "--label", "Josh Room device secret", *_secret_attributes(profile, field)],
        input=value + "\n",
    )
    if int(getattr(result, "returncode", 1)):
        raise SecureBackendError("secure secret import failed")


def _linux_delete(profile: str, field: str) -> None:
    result = _run([_SECRET_SERVICE_COMMAND, "clear", *_secret_attributes(profile, field)])
    if int(getattr(result, "returncode", 1)) not in {0, 1}:
        raise SecureBackendError("secure secret removal failed")


def _windows_target(profile: str, field: str) -> str:
    return f"josh-room/device/{_validate_identifier(profile, 'profile')}/{_validate_field(field)}"


def _windows_api():
    if _platform_name() != "windows":
        raise SecureBackendError("windows credential locker is unavailable")
    loader = getattr(ctypes, "WinDLL", None)
    if loader is not None:
        try:
            return loader("advapi32", use_last_error=True)
        except OSError:
            pass
    api = getattr(getattr(ctypes, "windll", None), "advapi32", None)
    if api is None:
        raise SecureBackendError("windows credential locker API is unavailable")
    return api


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

def _windows_lookup(profile: str, field: str) -> str:
    api = _windows_api()
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p), ("LastWritten", _FILETIME), ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
        ]
    pointer = ctypes.POINTER(CREDENTIALW)()
    target = _windows_target(profile, field)
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        raise SecureBackendError("secure secret is unavailable")
    try:
        item = pointer.contents
        return ctypes.string_at(item.CredentialBlob, item.CredentialBlobSize).decode("utf-8")
    finally:
        api.CredFree(pointer)


def _windows_store(profile: str, field: str, value: str) -> None:
    api = _windows_api()
    if not isinstance(value, str) or not value:
        raise ValueError("secret value is invalid")
    blob = value.encode("utf-8")
    buffer = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p), ("LastWritten", _FILETIME), ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)), ("Persist", ctypes.c_uint32), ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
        ]
    item = CREDENTIALW()
    item.Type = 1
    item.TargetName = _windows_target(profile, field)
    item.CredentialBlobSize = len(blob)
    item.CredentialBlob = buffer
    item.Persist = 2
    item.UserName = "josh-room"
    if not api.CredWriteW(ctypes.byref(item), 0):
        raise SecureBackendError("secure secret import failed")


def _windows_delete(profile: str, field: str) -> None:
    api = _windows_api()
    if not api.CredDeleteW(_windows_target(profile, field), 1, 0):
        error = ctypes.get_last_error()
        if error != 1168:  # ERROR_NOT_FOUND is the only benign failure.
            raise SecureBackendError("secure secret removal failed")


def secure_lookup(profile: str, field: str) -> str:
    status = _require_secure_backend()
    if status.backend == "linux-secret-service":
        return _linux_lookup(profile, field)
    if status.backend == "macos-keychain":
        result = _run([_KEYCHAIN_COMMAND, "find-generic-password", "-a", _validate_identifier(profile, "profile"), "-s", _validate_field(field), "-w"])
        value = str(getattr(result, "stdout", "") or "").rstrip("\n")
        if int(getattr(result, "returncode", 1)) or not value:
            raise SecureBackendError("secure secret is unavailable")
        return value
    return _windows_lookup(profile, field)


def secure_store(profile: str, field: str, value: str) -> None:
    status = _require_secure_backend()
    if status.backend == "linux-secret-service":
        _linux_store(profile, field, value)
    elif status.backend == "macos-keychain":
        result = _run(
            [_KEYCHAIN_COMMAND, "add-generic-password", "-U", "-a", _validate_identifier(profile, "profile"), "-s", _validate_field(field), "-w"],
            input=value + "\n",
        )
        if int(getattr(result, "returncode", 1)):
            raise SecureBackendError("secure secret import failed")
    else:
        _windows_store(profile, field, value)


def secure_delete(profile: str, field: str) -> None:
    status = _require_secure_backend()
    if status.backend == "linux-secret-service":
        _linux_delete(profile, field)
    elif status.backend == "macos-keychain":
        result = _run([_KEYCHAIN_COMMAND, "delete-generic-password", "-a", _validate_identifier(profile, "profile"), "-s", _validate_field(field)])
        if int(getattr(result, "returncode", 1)) not in {0, 44}:
            raise SecureBackendError("secure secret removal failed")
    else:
        _windows_delete(profile, field)


def _private_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


def _runtime_credentials(
    profile: str,
    runtime_profile: str,
    runtime_path: str | None,
    allow_runtime: bool,
) -> dict[str, str] | None:
    extension_mode = os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1"
    if not runtime_path or not ((allow_runtime and profile == runtime_profile) or extension_mode):
        return None
    path = Path(runtime_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise RuntimeError("runtime credential source is unsafe")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("runtime credential source permissions are not private")
    values = json.loads(path.read_text())
    if not isinstance(values, dict):
        raise TypeError("runtime credential source is incomplete")
    if isinstance(values.get("profiles"), dict):
        values = values["profiles"].get(profile)
        if not isinstance(values, dict):
            raise TypeError(f"runtime credential profile is unavailable: {profile}")
    elif profile != runtime_profile:
        raise RuntimeError(f"runtime credential profile is unavailable: {profile}")
    if not all(
        isinstance(values.get(field), str) and values[field]
        for field in ("access-key-id", "secret-access-key")
    ):
        raise RuntimeError("runtime credential source is incomplete")
    return {
        field: values[field]
        for field in ("access-key-id", "secret-access-key", "session-token")
        if isinstance(values.get(field), str) and values[field]
    }


def lookup(profile: str, *, allow_runtime: bool | None = None) -> dict[str, str]:
    """Read operation-time credentials from Secret Service without logging them."""
    runtime_profile = os.environ.get("JOSH_ROOM_RUNTIME_PROFILE", "oauth-runtime")
    runtime_path = os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS")
    provider_path = os.environ.get("JOSH_ROOM_PROVIDER_CREDENTIALS")
    if allow_runtime is None:
        allow_runtime = profile == runtime_profile
    if provider_path and profile != runtime_profile:
        credentials = _runtime_credentials(profile, runtime_profile, provider_path, False)
        if credentials is not None:
            return credentials
    credentials = _runtime_credentials(profile, runtime_profile, runtime_path, allow_runtime)
    if credentials is not None:
        return credentials
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    if _platform_name() != "linux":
        values = {}
        for field in ("access-key-id", "secret-access-key", "session-token"):
            try:
                values[field] = secure_lookup(profile, field)
            except SecureBackendError:
                pass
        if "access-key-id" not in values or "secret-access-key" not in values:
            raise RuntimeError("native credential profile is incomplete")
        return values
    values = {}
    for field in (
        "access-key-id",
        "secret-access-key",
        "session-token",
    ):
        process = subprocess.run(["secret-tool", "lookup", "service", "josh-room", "profile", profile, "field", field], capture_output=True, text=True, check=False)
        if process.returncode == 0:
            values[field] = process.stdout.rstrip("\n")
    if "access-key-id" not in values or "secret-access-key" not in values:
        raise RuntimeError("OS Secret Service profile is incomplete")
    return values


def lookup_value(profile: str, field: str) -> str:
    runtime_profile = os.environ.get("JOSH_ROOM_RUNTIME_PROFILE", "oauth-runtime")
    credentials = _runtime_credentials(
        profile,
        runtime_profile,
        os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS"),
        profile == runtime_profile,
    )
    if credentials is not None:
        value = credentials.get(field)
        if value:
            return value
        raise RuntimeError(f"runtime credential field is unavailable: {field}")
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    if _platform_name() != "linux":
        try:
            return secure_lookup(profile, field)
        except SecureBackendError as error:
            raise RuntimeError(f"native secret field is unavailable: {field}") from error
    process = subprocess.run(["secret-tool", "lookup", "service", "josh-room", "profile", profile, "field", field], capture_output=True, text=True, check=False)
    value = process.stdout.rstrip("\n")
    if process.returncode or not value:
        raise RuntimeError(f"OS Secret Service field is unavailable: {field}")
    return value


def store(profile: str, credentials: dict[str, str]) -> None:
    """Import one-time credentials into Secret Service; values never enter argv."""
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    for field in (
        "access-key-id",
        "secret-access-key",
        "session-token",
    ):
        value = credentials.get(field)
        if value is None:
            continue
        store_value(profile, field, value, label="Josh Room R2 credential")


def store_value(profile: str, field: str, value: str, label: str = "Josh Room secret") -> None:
    del label
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    if _platform_name() != "linux":
        secure_store(profile, field, value)
        return
    process = subprocess.run(["secret-tool", "store", "--label", "Josh Room secret", "service", "josh-room", "profile", profile, "field", field], input=value + "\n", text=True, capture_output=True, check=False)
    if process.returncode:
        raise RuntimeError("OS Secret Service import failed")


def _scoped_attributes(domain_id: str, key_generation: int) -> list[str]:
    if not isinstance(domain_id, str) or not domain_id or type(key_generation) is not int or key_generation < 1:
        raise ValueError("encryption key scope is invalid")
    return ["service", "josh-room", "scope", "encryption", "domain", domain_id, "generation", str(key_generation)]
def _scoped_profile(domain_id: str, key_generation: int) -> str:
    _scoped_attributes(domain_id, key_generation)
    return f"encryption-{domain_id}-{key_generation}"


def lookup_encryption_identity(domain_id: str, key_generation: int) -> str:
    """Read an operational identity from its isolated Secret Service scope."""
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    if _platform_name() != "linux":
        try:
            return secure_lookup(_scoped_profile(domain_id, key_generation), "age-identity")
        except SecureBackendError as error:
            raise RuntimeError("native encryption identity is unavailable") from error
    process = subprocess.run(
        ["secret-tool", "lookup", *_scoped_attributes(domain_id, key_generation), "field", "identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = process.stdout.rstrip("\n")
    if process.returncode or not value:
        raise RuntimeError("OS Secret Service encryption identity is unavailable")
    return value


def store_encryption_identity(domain_id: str, key_generation: int, value: str) -> None:
    """Store an operational identity without placing it in argv or project files."""
    if not isinstance(value, str) or not value:
        raise ValueError("encryption identity is invalid")
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    if _platform_name() != "linux":
        try:
            secure_store(_scoped_profile(domain_id, key_generation), "age-identity", value)
        except SecureBackendError as error:
            raise RuntimeError("native encryption identity import failed") from error
        return
    process = subprocess.run(
        [
            "secret-tool",
            "store",
            "--label",
            "Josh Room encryption identity",
            *_scoped_attributes(domain_id, key_generation),
            "field",
            "identity",
        ],
        input=value + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        raise RuntimeError("OS Secret Service encryption identity import failed")


def encryption_identity_scope(domain_id: str, key_generation: int) -> tuple[str, int]:
    _scoped_attributes(domain_id, key_generation)
    return domain_id, key_generation
