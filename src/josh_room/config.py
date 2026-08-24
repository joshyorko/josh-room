import json
import os
import tempfile
from pathlib import Path

from .keyring import available


def config_dir() -> Path:
    return Path(os.environ.get("JOSH_ROOM_CONFIG_DIR", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "josh-room"))


def private_config() -> dict | None:
    runtime = os.environ.get("JOSH_ROOM_RUNTIME_CONFIG")
    if runtime:
        path = Path(runtime)
        if path.is_file():
            return json.loads(path.read_text())
    path = config_dir() / "config.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def save_private_config(body: dict) -> Path:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    fd, temp_name = tempfile.mkstemp(prefix=".config.", dir=directory)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
        return path
    finally:
        temp.unlink(missing_ok=True)


def auth_status() -> dict:
    config = private_config()
    r2 = config.get("r2") if config else None
    if not r2:
        return {"state": "unconfigured", "mode": "unconfigured", "credentials_verified": False}
    if not available():
        return {"state": "keyring-unavailable", "mode": "s3-api-credentials", "credentials_verified": False, "bucket_configured": bool(r2.get("bucket"))}
    return {"state": "configured-unverified", "mode": "s3-api-credentials", "credential_source": "os-secret-service", "credentials_verified": False, "bucket_configured": bool(r2.get("bucket")), "temporary_credentials_preferred": bool(r2.get("temporary_credentials", True))}
