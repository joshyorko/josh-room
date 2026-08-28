import json
import os
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .config import config_dir
from .progress import report_progress

_RUNTIME_FILES = ("r2.json", "age.identity", "config.json", "session.json")
DEFAULT_AUTH_URL = "https://josh-room-auth.joshua-yorko.workers.dev"


def _runtime_paths() -> tuple[Path, ...]:
    root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"
    return tuple(root / name for name in _RUNTIME_FILES)


def _clear_runtime_session() -> None:
    for path in _runtime_paths():
        path.unlink(missing_ok=True)
    for name in (
        "JOSH_ROOM_RUNTIME_CREDENTIALS",
        "JOSH_ROOM_RUNTIME_CONFIG",
        "JOSH_ROOM_IDENTITY",
        "JOSH_ROOM_RUNTIME_PROFILE",
    ):
        os.environ.pop(name, None)


def start_oauth_session() -> dict:
    started = _request("/session/start", method="POST")
    return {
        "session_id": started["sessionId"],
        "authorization_url": started["authorizationUrl"],
        "expires_in": int(started.get("expiresIn", 600)),
    }


def poll_oauth_session(session_id: str, dimension_id: str | None = None) -> dict:
    session = _request(f"/session/{session_id}")
    status = session.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status != "authorized":
        _clear_runtime_session()
        raise RuntimeError(f"Cloudflare authorization {status or 'failed'}")
    _write_runtime(session, dimension_id=dimension_id)
    return {"status": "authorized"}


def cancel_oauth_session(session_id: str) -> dict:
    try:
        result = _request(f"/session/{session_id}/cancel", method="POST")
    except HTTPError as error:
        _clear_runtime_session()
        if error.code == 404:
            return {"status": "canceled", "stale": True}
        raise
    if result.get("status") == "canceled":
        _clear_runtime_session()
        return result
    return result
    return result


def logout_runtime_session() -> dict:
    """Forget the local Cloudflare session without contacting the authority."""
    _clear_runtime_session()
    return {"status": "logged_out"}


def wait_oauth_session(
    session_id: str,
    timeout: int = 600,
    poll_interval: int = 2,
    dimension_id: str | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    started_at = time.monotonic()
    try:
        while time.monotonic() < deadline:
            result = poll_oauth_session(session_id, dimension_id=dimension_id)
            if result["status"] != "pending":
                report_progress("auth", f"Validating Cloudflare session ({int(time.monotonic() - started_at)}s elapsed)")
                return result
            report_progress("auth", f"Waiting for browser approval ({int(time.monotonic() - started_at)}s elapsed)")
            remaining = deadline - time.monotonic()
            if remaining > 0 and poll_interval > 0:
                time.sleep(min(poll_interval, remaining))
    except KeyboardInterrupt:
        _clear_runtime_session()
        raise
    _clear_runtime_session()
    raise RuntimeError("Cloudflare authorization timed out")


def runtime_session_state() -> str:
    runtime_files = _runtime_paths()
    metadata = runtime_files[-1]
    if not metadata.is_file():
        if any(path.exists() for path in runtime_files):
            _clear_runtime_session()
        return "missing"
    try:
        expires_at = float(json.loads(metadata.read_text())["expires_at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _clear_runtime_session()
        return "missing"
    if expires_at <= time.time() + 60:
        _clear_runtime_session()
        return "expired"
    if not all(path.is_file() for path in runtime_files[:-1]):
        _clear_runtime_session()
        return "missing"
    return "connected"


def ensure_runtime_session(timeout: int = 600, dimension_id: str | None = None) -> None:
    runtime_files_ready = all(os.environ.get(name) and Path(os.environ[name]).is_file() for name in (
        "JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"
    ))
    if runtime_session_state() == "connected" and runtime_files_ready:
        report_progress("auth", "Cloudflare session is ready")
        return
    if _load_runtime():
        report_progress("auth", "Reusing this Room's Cloudflare session")
        return
    _clear_runtime_session()
    report_progress("auth", "Opening Cloudflare sign-in")
    started = start_oauth_session()
    webbrowser.open(started["authorization_url"])
    report_progress("auth", "Waiting for Cloudflare approval in your browser")
    wait_oauth_session(started["session_id"], timeout=timeout, dimension_id=dimension_id)
    report_progress("auth", "Cloudflare session authorized")


def _worker_url() -> str:
    value = os.environ.get("JOSH_ROOM_AUTH_URL", DEFAULT_AUTH_URL).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Cloudflare auth authority is not configured; set JOSH_ROOM_AUTH_URL to an http(s) URL")
    return value


def _request(path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(
        _worker_url() + path,
        method=method,
        headers={"User-Agent": "Josh-Room/0.1 (+https://github.com/joshyorko/josh-room)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _write_runtime(session: dict, dimension_id: str | None = None) -> None:
    root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    credentials = root / "r2.json"
    identity = root / "age.identity"
    config = root / "config.json"
    metadata = root / "session.json"
    credentials.write_text(json.dumps({
        "access-key-id": session["accessKeyId"],
        "secret-access-key": session["secretAccessKey"],
        "session-token": session["sessionToken"],
    }))
    identity.write_text(session["ageIdentity"].rstrip("\n") + "\n")
    persisted_path = config_dir() / "config.json"
    try:
        persisted = json.loads(persisted_path.read_text()) if persisted_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        persisted = {}
    runtime_config = json.loads(json.dumps(persisted))
    runtime_config.setdefault("default_backend", "r2")
    runtime_config.setdefault("default_ide", "vscode-insiders")
    runtime_config["age_recipients"] = session["ageRecipients"]
    runtime_r2 = {
        "endpoint": session["endpoint"],
        "bucket": session["bucket"],
        "region": "auto",
        "credential_profile": "oauth-runtime",
        "catalog_key": "catalog.jroom.age",
        "temporary_credentials": True,
    }
    runtime_config["r2"] = {**runtime_config.get("r2", {}), **runtime_r2}
    dimensions = runtime_config.setdefault("dimensions", {})
    connections = runtime_config.setdefault("connections", {})
    target = dimension_id or "r2"
    target_record = dimensions.get(target)
    if isinstance(target_record, dict) and target_record.get("connection_id"):
        connection_id = target_record["connection_id"]
        connections[connection_id] = {
            **connections.get(connection_id, {}),
            "provider": "r2",
            "endpoint": session["endpoint"],
            "credential_profile": "oauth-runtime",
            "region": "auto",
            "temporary_credentials": True,
            "auth_state": "configured",
        }
    elif target == "r2" or target not in dimensions:
        dimensions["r2"] = {
            "display_name": "Cloudflare R2",
            "provider": "r2",
            **runtime_r2,
        }
    elif dimensions[target].get("provider") == "r2":
        dimensions[target] = {**dimensions[target], **runtime_r2}
    config.write_text(json.dumps(runtime_config))
    metadata.write_text(json.dumps({"expires_at": time.time() + int(session["expiresIn"])}))
    for path in (credentials, identity, config, metadata):
        path.chmod(0o600)
    os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] = str(credentials)
    os.environ["JOSH_ROOM_IDENTITY"] = str(identity)
    os.environ["JOSH_ROOM_RUNTIME_CONFIG"] = str(config)
    os.environ["JOSH_ROOM_RUNTIME_PROFILE"] = "oauth-runtime"


def _load_runtime() -> bool:
    credentials, identity, config, metadata = _runtime_paths()
    if not all(path.is_file() for path in (credentials, identity, config, metadata)):
        if any(path.exists() for path in (credentials, identity, config, metadata)):
            _clear_runtime_session()
        return False
    try:
        expires_at = float(json.loads(metadata.read_text())["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        _clear_runtime_session()
        return False
    if expires_at <= time.time() + 60:
        _clear_runtime_session()
        return False
    os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] = str(credentials)
    os.environ["JOSH_ROOM_IDENTITY"] = str(identity)
    os.environ["JOSH_ROOM_RUNTIME_CONFIG"] = str(config)
    os.environ["JOSH_ROOM_RUNTIME_PROFILE"] = "oauth-runtime"
    return True
