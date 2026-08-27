import json
import os
import shutil
import stat
import subprocess
from pathlib import Path


def available() -> bool:
    return shutil.which("secret-tool") is not None


def lookup(profile: str) -> dict[str, str]:
    """Read operation-time credentials from Secret Service without logging them."""
    runtime_path = os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS")
    if runtime_path:
        path = Path(runtime_path)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
            raise RuntimeError("runtime credential source is unsafe")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError("runtime credential source permissions are not private")
        values = json.loads(path.read_text())
        if not isinstance(values, dict) or not all(
            isinstance(values.get(field), str) and values[field]
            for field in ("access-key-id", "secret-access-key")
        ):
            raise RuntimeError("runtime credential source is incomplete")
        credentials = {
            field: values[field] for field in ("access-key-id", "secret-access-key")
        }
        if isinstance(values.get("session-token"), str) and values["session-token"]:
            credentials["session-token"] = values["session-token"]
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
