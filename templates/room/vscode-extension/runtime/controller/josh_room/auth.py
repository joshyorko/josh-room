import json
import os
import re
import stat
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .config import config_dir
from .progress import report_progress
from .tls import system_ssl_context

_RUNTIME_FILES = ("r2.json", "age.identity", "config.json", "session.json")
DEFAULT_AUTH_URL = "https://josh-room-auth.joshua-yorko.workers.dev"
_AUTH_PURPOSES = {"encryption", "r2"}


def _runtime_root() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"


def _runtime_paths() -> tuple[Path, ...]:
    root = _runtime_root()
    return tuple(root / name for name in _RUNTIME_FILES)


def _r2_logout_marker() -> Path:
    return _runtime_root() / "r2-logout.json"


def _write_private_json(path: Path, body: dict) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _clear_runtime_session() -> None:
    credentials, identity, config, metadata = _runtime_paths()
    for path in (credentials, identity, config, metadata):
        path.unlink(missing_ok=True)
    _r2_logout_marker().unlink(missing_ok=True)
    if os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS") == str(credentials):
        os.environ.pop("JOSH_ROOM_RUNTIME_CREDENTIALS", None)
    if os.environ.get("JOSH_ROOM_RUNTIME_CONFIG") == str(config):
        os.environ.pop("JOSH_ROOM_RUNTIME_CONFIG", None)
    if os.environ.get("JOSH_ROOM_RUNTIME_PROFILE") == "oauth-runtime":
        os.environ.pop("JOSH_ROOM_RUNTIME_PROFILE", None)
    if os.environ.get("JOSH_ROOM_IDENTITY") == str(identity):
        os.environ.pop("JOSH_ROOM_IDENTITY", None)


def _recover_r2_logout() -> bool:
    marker = _r2_logout_marker()
    if not marker.exists():
        return False
    if marker.is_symlink() or not marker.is_file() or stat.S_IMODE(marker.stat().st_mode) & 0o077:
        _clear_runtime_session()
        return False
    try:
        body = json.loads(marker.read_text())
        config_body = body["config"]
        metadata_body = body["metadata"]
        if not isinstance(config_body, dict) or not isinstance(metadata_body, dict) \
                or metadata_body.get("capabilities") != ["encryption"] \
                or metadata_body.get("purpose") != "encryption" \
                or not isinstance(metadata_body.get("expires_at"), (int, float)):
            raise ValueError("invalid R2 logout recovery marker")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        _clear_runtime_session()
        return False
    credentials, _identity, config, metadata = _runtime_paths()
    _write_private_json(config, config_body)
    _write_private_json(metadata, metadata_body)
    credentials.unlink(missing_ok=True)
    marker.unlink()
    return True


def _validate_purpose(purpose: str) -> str:
    if purpose not in _AUTH_PURPOSES:
        raise ValueError("authorization purpose must be encryption or r2")
    return purpose


def start_oauth_session(purpose: str = "r2") -> dict:
    purpose = _validate_purpose(purpose)
    started = _request("/session/start", method="POST", body={"purpose": purpose})
    return {
        "session_id": started["sessionId"],
        "authorization_url": started["authorizationUrl"],
        "expires_in": int(started.get("expiresIn", 600)),
    }


def poll_oauth_session(session_id: str, dimension_id: str | None = None, purpose: str | None = None) -> dict:
    session = _request(f"/session/{session_id}")
    status = session.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status != "authorized":
        _clear_runtime_session()
        raise RuntimeError(f"Cloudflare authorization {status or 'failed'}")
    if purpose is None:
        _write_runtime(session, dimension_id=dimension_id)
    else:
        _write_runtime(session, dimension_id=dimension_id, purpose=purpose)
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


def logout_runtime_session(purpose: str = "all") -> dict:
    """Forget R2 authority or the complete local session without contacting the authority."""
    if purpose not in {"all", "r2"}:
        raise ValueError("logout purpose must be all or r2")
    if purpose == "all":
        _clear_runtime_session()
        return {"status": "logged_out"}

    state, capabilities = _read_runtime()
    if state != "connected" or "encryption" not in capabilities:
        return {"status": "logged_out", "encryption_preserved": False}
    if "r2" not in capabilities:
        _set_runtime_environment(capabilities)
        return {"status": "logged_out", "encryption_preserved": True}

    _credentials, _identity, config, metadata = _runtime_paths()
    runtime_config = json.loads(config.read_text())
    metadata_body = json.loads(metadata.read_text())
    age_recipients = runtime_config["age_recipients"]
    persisted_path = config_dir() / "config.json"
    try:
        persisted = json.loads(persisted_path.read_text()) if persisted_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        persisted = {}
    downgraded = json.loads(json.dumps(persisted)) if isinstance(persisted, dict) else {}
    downgraded["age_recipients"] = list(age_recipients)
    _write_private_json(_r2_logout_marker(), {
        "config": downgraded,
        "metadata": {
            "expires_at": metadata_body["expires_at"],
            "capabilities": ["encryption"],
            "purpose": "encryption",
        },
    })
    _recover_r2_logout()
    _set_runtime_environment(("encryption",))
    return {"status": "logged_out", "encryption_preserved": True}


def wait_oauth_session(
    session_id: str,
    timeout: int = 600,
    poll_interval: int = 2,
    dimension_id: str | None = None,
    purpose: str | None = None,
) -> dict:
    deadline = time.monotonic() + timeout
    started_at = time.monotonic()
    try:
        while time.monotonic() < deadline:
            result = poll_oauth_session(session_id, dimension_id=dimension_id, purpose=purpose)
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


def _valid_identity(value: str) -> bool:
    return any(re.fullmatch(r"AGE-SECRET-KEY-[A-Za-z0-9-]+", line.strip()) for line in value.splitlines())


def _read_runtime() -> tuple[str, tuple[str, ...]]:
    _recover_r2_logout()
    credentials, identity, config, metadata = _runtime_paths()
    if not metadata.is_file() or metadata.is_symlink():
        if any(path.exists() for path in (credentials, identity, config, metadata)):
            _clear_runtime_session()
        return "missing", ()
    try:
        metadata_body = json.loads(metadata.read_text())
        expires_at = float(metadata_body["expires_at"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _clear_runtime_session()
        return "missing", ()
    if expires_at <= time.time() + 60:
        _clear_runtime_session()
        return "expired", ()
    if not all(path.is_file() and not path.is_symlink() for path in (identity, config)):
        _clear_runtime_session()
        return "missing", ()
    try:
        identity_value = identity.read_text().strip()
        config_body = json.loads(config.read_text())
        recipients = config_body.get("age_recipients") if isinstance(config_body, dict) else None
    except (OSError, TypeError, json.JSONDecodeError):
        _clear_runtime_session()
        return "missing", ()
    if not _valid_identity(identity_value) or not isinstance(config_body, dict) \
            or not isinstance(recipients, list) \
            or len(recipients) < 2 \
            or len({value for value in recipients if isinstance(value, str) and value}) < 2 \
            or any(not isinstance(value, str) or not value for value in recipients) \
            or stat.S_IMODE(identity.stat().st_mode) & 0o077:
        _clear_runtime_session()
        return "missing", ()
    capabilities = metadata_body.get("capabilities")
    if capabilities is None:
        capabilities = ["encryption", "r2"] if credentials.is_file() else ["encryption"]
    if not isinstance(capabilities, list) or "encryption" not in capabilities \
            or any(value not in {"encryption", "r2"} for value in capabilities):
        _clear_runtime_session()
        return "missing", ()
    capabilities = tuple(sorted(set(capabilities)))
    if "r2" in capabilities:
        try:
            credential_body = json.loads(credentials.read_text())
            required = {"access-key-id", "secret-access-key", "session-token"}
            credentials_valid = required.issubset(credential_body) and all(
                isinstance(credential_body[name], str) and credential_body[name]
                for name in required
            )
        except (OSError, TypeError, json.JSONDecodeError):
            credentials_valid = False
        if not credentials_valid or not credentials.is_file() or credentials.is_symlink() \
                or stat.S_IMODE(credentials.stat().st_mode) & 0o077:
            _clear_runtime_session()
            return "missing", ()
    elif credentials.exists():
        # Encryption-only sessions must not retain authority-issued R2 material.
        _clear_runtime_session()
        return "missing", ()
    return "connected", capabilities


def runtime_session_state() -> str:
    return _read_runtime()[0]


def runtime_capabilities() -> tuple[str, ...]:
    state, capabilities = _read_runtime()
    return capabilities if state == "connected" else ()


def encryption_session_state() -> str:
    return runtime_session_state()


def r2_session_state() -> str:
    state, capabilities = _read_runtime()
    if state != "connected":
        return state
    return "connected" if "r2" in capabilities else "missing"


def _set_runtime_environment(capabilities: tuple[str, ...]) -> None:
    credentials, identity, config, _metadata = _runtime_paths()
    os.environ["JOSH_ROOM_RUNTIME_CONFIG"] = str(config)
    os.environ["JOSH_ROOM_IDENTITY"] = str(identity)
    if "r2" in capabilities:
        os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] = str(credentials)
        os.environ["JOSH_ROOM_RUNTIME_PROFILE"] = "oauth-runtime"
    else:
        if os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS") == str(credentials):
            os.environ.pop("JOSH_ROOM_RUNTIME_CREDENTIALS", None)
        if os.environ.get("JOSH_ROOM_RUNTIME_PROFILE") == "oauth-runtime":
            os.environ.pop("JOSH_ROOM_RUNTIME_PROFILE", None)


def _load_runtime(require_r2: bool = False) -> bool:
    state, capabilities = _read_runtime()
    if state != "connected" or (require_r2 and "r2" not in capabilities):
        return False
    _set_runtime_environment(capabilities)
    return True


def ensure_runtime_session(timeout: int = 600, dimension_id: str | None = None) -> None:
    if _load_runtime(require_r2=True):
        report_progress("auth", "Cloudflare session is ready")
        return
    _clear_runtime_session()
    report_progress("auth", "Opening Cloudflare sign-in")
    started = start_oauth_session("r2")
    webbrowser.open(started["authorization_url"])
    report_progress("auth", "Waiting for Cloudflare approval in your browser")
    wait_oauth_session(started["session_id"], timeout=timeout, dimension_id=dimension_id, purpose="r2")
    report_progress("auth", "Cloudflare session authorized")


def _worker_url() -> str:
    value = os.environ.get("JOSH_ROOM_AUTH_URL", DEFAULT_AUTH_URL).strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("Cloudflare auth authority is not configured; set JOSH_ROOM_AUTH_URL to an http(s) URL")
    return value


def _request(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None
    headers = {"User-Agent": "Josh-Room/0.1 (+https://github.com/joshyorko/josh-room)"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        _worker_url() + path,
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30, context=system_ssl_context()) as response:
        return json.load(response)


def _session_purpose(session: dict, purpose: str | None) -> str:
    if purpose is not None:
        return _validate_purpose(purpose)
    declared = session.get("purpose")
    if declared in _AUTH_PURPOSES:
        return declared
    capabilities = session.get("capabilities")
    if isinstance(capabilities, list) and "r2" not in capabilities:
        return "encryption"
    # Responses from the pre-purpose authority always carried R2 material.
    return "r2"


def _write_runtime(session: dict, dimension_id: str | None = None, purpose: str | None = None) -> None:
    purpose = _session_purpose(session, purpose)
    age_identity = session.get("ageIdentity")
    age_recipients = session.get("ageRecipients")
    if not isinstance(age_identity, str) or not _valid_identity(age_identity) \
            or not isinstance(age_recipients, list) \
            or len(age_recipients) < 2 \
            or len({value for value in age_recipients if isinstance(value, str) and value}) < 2 \
            or any(not isinstance(value, str) or not value for value in age_recipients):
        raise RuntimeError("Cloudflare authorization did not provide complete encryption material")
    include_r2 = purpose == "r2"
    if include_r2 and not all(isinstance(session.get(name), str) and session[name] for name in (
        "accessKeyId", "secretAccessKey", "sessionToken", "endpoint", "bucket",
    )):
        raise RuntimeError("Cloudflare authorization did not provide complete R2 storage material")

    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    credentials = root / "r2.json"
    identity = root / "age.identity"
    config = root / "config.json"
    metadata = root / "session.json"
    persisted_path = config_dir() / "config.json"
    try:
        persisted = json.loads(persisted_path.read_text()) if persisted_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        persisted = {}
    runtime_config = json.loads(json.dumps(persisted)) if isinstance(persisted, dict) else {}
    runtime_config["age_recipients"] = list(age_recipients)

    if include_r2:
        runtime_config.setdefault("default_backend", "r2")
        runtime_config.setdefault("default_ide", "vscode-insiders")
        runtime_config["r2"] = {
            **runtime_config.get("r2", {}),
            "endpoint": session["endpoint"],
            "bucket": session["bucket"],
            "region": "auto",
            "credential_profile": "oauth-runtime",
            "catalog_key": "catalog.jroom.age",
            "temporary_credentials": True,
        }
        dimensions = runtime_config.setdefault("dimensions", {})
        connections = runtime_config.setdefault("connections", {})
        target = dimension_id or "r2"
        target_record = dimensions.get(target)
        connection_id = target_record.get("connection_id") if isinstance(target_record, dict) else None
        connection = connections.get(connection_id) if connection_id else None
        target_provider = connection.get("provider") if isinstance(connection, dict) else (
            target_record.get("provider") if isinstance(target_record, dict) else ("r2" if target == "r2" else None)
        )
        if target_provider == "r2" and connection_id:
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
                **runtime_config["r2"],
            }
        elif target_provider == "r2":
            dimensions[target] = {**dimensions[target], **runtime_config["r2"]}
    else:
        credentials.unlink(missing_ok=True)

    credentials_body = {
        "access-key-id": session.get("accessKeyId"),
        "secret-access-key": session.get("secretAccessKey"),
        "session-token": session.get("sessionToken"),
    }
    identity.write_text(age_identity.rstrip("\n") + "\n")
    config.write_text(json.dumps(runtime_config))
    if include_r2:
        credentials.write_text(json.dumps(credentials_body))
    metadata.write_text(json.dumps({
        "expires_at": time.time() + int(session.get("expiresIn", 600)),
        "capabilities": ["encryption", "r2"] if include_r2 else ["encryption"],
        "purpose": purpose,
    }))
    for path in (identity, config, metadata):
        path.chmod(0o600)
    if include_r2:
        credentials.chmod(0o600)
    _set_runtime_environment(("encryption", "r2") if include_r2 else ("encryption",))


def _load_runtime_legacy() -> bool:
    """Compatibility alias for callers that used the private helper."""
    return _load_runtime()


def load_runtime_session() -> bool:
    """Load an existing local encryption or Cloudflare session without contacting the authority."""
    return _load_runtime()
