import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


def available() -> bool:
    if shutil.which("secret-tool") is not None:
        return True
    runtime_path = os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS")
    return (
        os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1"
        and runtime_path is not None
        and _private_file(Path(runtime_path))
    )


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
        raise RuntimeError("runtime credential source is incomplete")
    if isinstance(values.get("profiles"), dict):
        values = values["profiles"].get(profile)
        if not isinstance(values, dict):
            raise RuntimeError(f"runtime credential profile is unavailable: {profile}")
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
    if allow_runtime is None:
        allow_runtime = profile == runtime_profile
    credentials = _runtime_credentials(profile, runtime_profile, runtime_path, allow_runtime)
    if credentials is not None:
        return credentials
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
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
    if not available():
        raise RuntimeError("OS Secret Service is unavailable")
    process = subprocess.run(["secret-tool", "store", "--label", label, "service", "josh-room", "profile", profile, "field", field], input=value + "\n", text=True, capture_output=True, check=False)
    if process.returncode:
        raise RuntimeError("OS Secret Service import failed")
