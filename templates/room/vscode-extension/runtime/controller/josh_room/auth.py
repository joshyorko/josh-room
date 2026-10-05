import json
import os
import re
import stat
import tempfile
import time
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError

from .config import config_dir
from .encryption_domain import (
    KEYSET_CONTROL_KEY,
    ROOM_STORE_KEYSET_FORMAT_VERSION,
    EncryptionKeyset,
    EncryptionMaterial,
    physical_bucket_identity,
    validate_encryption_domain_id,
    validate_minio_transport,
    validate_operational_identity,
    validate_recipient,
)
from .keyring import (
    lookup_encryption_identity,
    store_encryption_identity,
    store_room_store_secret,
)
from .private_paths import (
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)
from .progress import report_progress
from .r2_room_store_material import (
    R2RoomStoreAuthority,
    R2RoomStoreError,
    R2RoomStoreMaterial,
)
from .tls import system_ssl_context

_RUNTIME_FILES = ("r2.json", "age.identity", "config.json", "session.json")
_ROOM_STORE_HANDOFF_FIELDS = (
    "room_store_session_id",
    "room_store_domain_id",
    "room_store_capability",
    "room_store_capability_expires_at",
)
DEFAULT_AUTH_URL = "https://josh-room-auth.joshua-yorko.workers.dev"
_AUTH_PURPOSES = {"encryption", "r2"}


def _runtime_root() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"


def _runtime_paths() -> tuple[Path, ...]:
    root = _runtime_root()
    return tuple(root / name for name in _RUNTIME_FILES)


def _runtime_root_is_private() -> bool:
    root = _runtime_root()
    if root.is_symlink() or not root.is_dir():
        return False
    try:
        verify_private_path(root, directory=True)
    except (OSError, RuntimeError):
        return False
    return True


def _r2_logout_marker() -> Path:
    return _runtime_root() / "r2-logout.json"


def _write_private_text(path: Path, value: str) -> None:
    protect_private_directory(path.parent)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        protect_private_file(temporary)
        os.replace(temporary, path)
        protect_private_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_private_json(path: Path, body: dict) -> None:
    _write_private_text(path, json.dumps(body))


def _clear_runtime_session() -> None:
    credentials, identity, config, metadata = _runtime_paths()
    if _runtime_root_is_private():
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


def _valid_room_store_handoff(metadata: dict, *, now: float | None = None) -> bool:
    """Validate the private, short-lived broker handoff without exposing it."""
    now = time.time() if now is None else now
    session_id = metadata.get("room_store_session_id")
    domain_id = metadata.get("room_store_domain_id")
    capability = metadata.get("room_store_capability")
    expires_at = metadata.get("room_store_capability_expires_at")
    session_expires_at = metadata.get("expires_at")
    return (
        isinstance(session_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", session_id) is not None
        and isinstance(domain_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", domain_id) is not None
        and isinstance(capability, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{43}", capability) is not None
        and type(expires_at) in {int, float}
        and now + 15 < expires_at <= now + 615
        and type(session_expires_at) in {int, float}
        and expires_at <= session_expires_at
    )


def _clear_room_store_handoff(metadata: dict, path: Path) -> None:
    if not any(name in metadata for name in _ROOM_STORE_HANDOFF_FIELDS):
        return
    for name in _ROOM_STORE_HANDOFF_FIELDS:
        metadata.pop(name, None)
    _write_private_json(path, metadata)


def _recover_r2_logout() -> bool:
    if not _runtime_root_is_private():
        return False
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
    result = {
        "session_id": started["sessionId"],
        "authorization_url": started["authorizationUrl"],
        "expires_in": int(started.get("expiresIn", 600)),
    }
    if isinstance(started.get("purpose"), str) and started["purpose"] in _AUTH_PURPOSES:
        result["purpose"] = started["purpose"]
    elif "purpose" in started:
        result["invalid_metadata"] = True
    if isinstance(started.get("capabilities"), list) and all(
        isinstance(value, str) and value in _AUTH_PURPOSES for value in started["capabilities"]
    ):
        result["capabilities"] = list(dict.fromkeys(started["capabilities"]))
    elif "capabilities" in started:
        result["invalid_metadata"] = True
    return result


def _validate_source_handoff_mode(purpose: str | None, handoff: Path | None, legacy_r2_source: bool) -> None:
    if legacy_r2_source:
        if handoff is None or purpose != "r2":
            raise ValueError("legacy R2 source requires a legacy source handoff and r2 purpose")
    elif handoff is not None and purpose != "encryption":
        raise ValueError("legacy source handoff requires encryption purpose")


def poll_oauth_session(
    session_id: str,
    dimension_id: str | None = None,
    purpose: str | None = None,
    legacy_source_handoff: Path | None = None,
    legacy_r2_source: bool = False,
) -> dict:
    _validate_source_handoff_mode(purpose, legacy_source_handoff, legacy_r2_source)
    session = _request(f"/session/{session_id}")
    status = session.get("status")
    if status == "pending":
        return {"status": "pending"}
    if status != "authorized":
        if legacy_source_handoff is None:
            _clear_runtime_session()
        label = "Josh Room encryption authorization" if purpose == "encryption" else "Cloudflare authorization"
        raise RuntimeError(f"{label} {status or 'failed'}")
    if legacy_source_handoff is not None:
        _write_legacy_source_handoff(session, legacy_source_handoff, legacy_r2_source=legacy_r2_source)
        return {"status": "authorized", "purpose": "r2" if legacy_r2_source else "encryption"}
    if purpose is None:
        _write_runtime(session, dimension_id=dimension_id, session_id=session_id)
    else:
        _write_runtime(session, dimension_id=dimension_id, purpose=purpose, session_id=session_id)
    return {"status": "authorized"}


def cancel_oauth_session(session_id: str, preserve_runtime_session: bool = False) -> dict:
    try:
        result = _request(f"/session/{session_id}/cancel", method="POST")
    except HTTPError as error:
        if not preserve_runtime_session:
            _clear_runtime_session()
        if error.code == 404:
            return {"status": "canceled", "stale": True}
        raise
    if result.get("status") == "canceled":
        if not preserve_runtime_session:
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
    legacy_source_handoff: Path | None = None,
    legacy_r2_source: bool = False,
) -> dict:
    _validate_source_handoff_mode(purpose, legacy_source_handoff, legacy_r2_source)
    deadline = time.monotonic() + timeout
    started_at = time.monotonic()
    validation_label = "Josh Room encryption authorization" if purpose == "encryption" else "Cloudflare session"
    authorization_label = "Josh Room encryption authorization" if purpose == "encryption" else "Cloudflare authorization"
    try:
        while time.monotonic() < deadline:
            result = poll_oauth_session(
                session_id,
                dimension_id=dimension_id,
                purpose=purpose,
                legacy_source_handoff=legacy_source_handoff,
                legacy_r2_source=legacy_r2_source,
            )
            if result["status"] != "pending":
                report_progress("auth", f"Validating {validation_label} ({int(time.monotonic() - started_at)}s elapsed)")
                return result
            report_progress("auth", f"Waiting for browser approval ({int(time.monotonic() - started_at)}s elapsed)")
            remaining = deadline - time.monotonic()
            if remaining > 0 and poll_interval > 0:
                time.sleep(min(poll_interval, remaining))
    except KeyboardInterrupt:
        if legacy_source_handoff is None:
            _clear_runtime_session()
        raise
    if legacy_source_handoff is None:
        _clear_runtime_session()
    raise RuntimeError(f"{authorization_label} timed out")


def _valid_identity(value: str) -> bool:
    try:
        validate_operational_identity(value)
    except (TypeError, ValueError):
        return False
    return True


def _normalize_identity(value: object) -> str:
    if value is None:
        raise ValueError("identity missing")
    if not isinstance(value, str):
        raise TypeError("identity type invalid")
    if len(value) > 16 * 1024:
        raise ValueError("identity format invalid")
    candidates = [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not candidates:
        raise ValueError("identity missing")
    if len(candidates) != 1:
        raise ValueError("identity multiple")
    try:
        validate_operational_identity(candidates[0])
    except (TypeError, ValueError) as error:
        raise ValueError("identity format invalid") from error
    return candidates[0]


def _validate_recipients(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("recipient type invalid")
    if any(not item for item in value):
        raise ValueError("recipient value invalid")
    if len(value) < 2 or len(set(value)) < 2:
        raise ValueError("recipient count invalid")
    return value


def _read_runtime() -> tuple[str, tuple[str, ...]]:
    if not _runtime_root_is_private():
        return "missing", ()
    _recover_r2_logout()
    credentials, identity, config, metadata = _runtime_paths()
    if not metadata.is_file() or metadata.is_symlink():
        if any(path.exists() for path in (credentials, identity, config, metadata)):
            _clear_runtime_session()
        return "missing", ()
    try:
        verify_private_path(metadata, directory=False)
    except (OSError, RuntimeError):
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
    if any(name in metadata_body for name in _ROOM_STORE_HANDOFF_FIELDS) and not _valid_room_store_handoff(metadata_body):
        _clear_room_store_handoff(metadata_body, metadata)
    if not all(path.is_file() and not path.is_symlink() for path in (identity, config)):
        _clear_runtime_session()
        return "missing", ()
    try:
        verify_private_path(identity, directory=False)
        verify_private_path(config, directory=False)
    except (OSError, RuntimeError):
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
        try:
            verify_private_path(credentials, directory=False)
        except (OSError, RuntimeError):
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


def _validate_r2_storage_material(session: dict) -> None:
    if not all(isinstance(session.get(name), str) and session[name] for name in (
        "accessKeyId", "secretAccessKey", "sessionToken", "endpoint", "bucket",
    )):
        raise RuntimeError("Cloudflare authorization did not provide complete R2 storage material")


def _write_legacy_source_handoff(session: dict, path: Path, *, legacy_r2_source: bool = False) -> None:
    if legacy_r2_source:
        if ("purpose" in session and session["purpose"] != "r2") or (
            "capabilities" in session and session["capabilities"] not in (["encryption", "r2"], ["r2", "encryption"])
        ):
            raise RuntimeError("authorization broker legacy R2 source purpose/capabilities contract is invalid")
        _validate_r2_storage_material(session)
    elif session.get("purpose") != "encryption" or session.get("capabilities") != ["encryption"]:
        raise RuntimeError("authorization broker lacks encryption-only capability")
    identity = _normalize_identity(session.get("ageIdentity"))
    _validate_recipients(session.get("ageRecipients"))
    path = Path(path)
    parent = path.parent
    try:
        if parent.is_symlink() or not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) != 0o700:
            raise RuntimeError("legacy source handoff parent is not private")
        if path.is_symlink() or path.exists():
            raise RuntimeError("legacy source handoff already exists")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (OSError, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError("legacy source handoff is unsafe") from error
    complete = False
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(identity + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
        complete = True
    except (OSError, ValueError) as error:
        raise RuntimeError("legacy source handoff could not be written") from error
    finally:
        if not complete:
            path.unlink(missing_ok=True)


def _write_runtime(
    session: dict,
    dimension_id: str | None = None,
    purpose: str | None = None,
    *,
    session_id: str | None = None,
) -> None:
    purpose = _session_purpose(session, purpose)
    try:
        age_identity = _normalize_identity(session.get("ageIdentity"))
        age_recipients = _validate_recipients(session.get("ageRecipients"))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Cloudflare authorization encryption material {error}") from error
    include_r2 = purpose == "r2"
    if include_r2:
        _validate_r2_storage_material(session)
    room_store_handoff = None
    handoff_fields = (
        "roomStoreDomainId",
        "roomStoreCapability",
        "roomStoreCapabilityExpiresIn",
    )
    if any(name in session for name in handoff_fields):
        domain_id = session.get("roomStoreDomainId")
        capability = session.get("roomStoreCapability")
        capability_expires_in = session.get("roomStoreCapabilityExpiresIn")
        if (
            not include_r2
            or not isinstance(session_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", session_id) is None
            or not isinstance(domain_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", domain_id) is None
            or not isinstance(capability, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", capability) is None
            or type(capability_expires_in) is not int
            or not 1 <= capability_expires_in <= 600
        ):
            raise RuntimeError("Cloudflare authorization Room Store handoff is invalid")
        room_store_handoff = (session_id, domain_id, capability, capability_expires_in)

    root = _runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    protect_private_directory(root)
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
    _write_private_text(identity, age_identity.rstrip("\n") + "\n")
    _write_private_json(config, runtime_config)
    if include_r2:
        _write_private_json(credentials, credentials_body)
    now = time.time()
    expires_at = now + int(session.get("expiresIn", 600))
    metadata_body = {
        "expires_at": expires_at,
        "capabilities": ["encryption", "r2"] if include_r2 else ["encryption"],
        "purpose": purpose,
    }
    if room_store_handoff is not None:
        handoff_session_id, domain_id, capability, capability_expires_in = room_store_handoff
        metadata_body.update({
            "room_store_session_id": handoff_session_id,
            "room_store_domain_id": domain_id,
            "room_store_capability": capability,
            "room_store_capability_expires_at": min(now + capability_expires_in, expires_at),
        })
    _write_private_json(metadata, metadata_body)
    _set_runtime_environment(("encryption", "r2") if include_r2 else ("encryption",))


def _load_runtime_legacy() -> bool:
    """Compatibility alias for callers that used the private helper."""
    return _load_runtime()


def load_runtime_session(*, require_r2: bool = False) -> bool:
    """Load an existing local session without contacting the authority."""
    if type(require_r2) is not bool:
        raise TypeError("R2 runtime capability requirement must be boolean")
    return _load_runtime(require_r2=require_r2)


@dataclass(frozen=True, slots=True)
class R2RoomStoreSession:
    """Authenticated broker plus the matching local age and password material."""

    authority: R2RoomStoreAuthority = field(repr=False)
    room_store: R2RoomStoreMaterial = field(repr=False)
    encryption_material: EncryptionMaterial = field(repr=False)

    def __repr__(self) -> str:
        return "R2RoomStoreSession(authority=<redacted>, material=<redacted>)"


def create_r2_room_store_authority(
    dimension, *, allow_initialize: bool = False, endpoint_transport=None
) -> R2RoomStoreSession:
    """Read R2 material privately, creating broker state only when explicitly allowed."""
    if type(allow_initialize) is not bool:
        raise TypeError("R2 Room Store initialization setting must be boolean")
    state, capabilities = _read_runtime()
    if state != "connected" or "r2" not in capabilities:
        raise R2RoomStoreError("r2_authorization_session_unavailable")
    if getattr(dimension, "provider", None) != "r2":
        raise ValueError("Room Store authority requires an R2 Dimension")

    _credentials, identity_path, config_path, metadata_path = _runtime_paths()
    try:
        verify_private_path(identity_path, directory=False)
        verify_private_path(config_path, directory=False)
        verify_private_path(metadata_path, directory=False)
        runtime_config = json.loads(config_path.read_text())
        metadata = json.loads(metadata_path.read_text())
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        raise R2RoomStoreError("r2_authorization_session_unavailable") from None
    r2_config = runtime_config.get("r2") if isinstance(runtime_config, dict) else None
    if not isinstance(r2_config, dict):
        raise R2RoomStoreError("r2_authorization_session_unavailable")
    try:
        selected_binding = physical_bucket_identity("r2", dimension.endpoint, dimension.bucket)
        oauth_binding = physical_bucket_identity("r2", r2_config["endpoint"], r2_config["bucket"])
    except (KeyError, TypeError, ValueError):
        raise R2RoomStoreError("r2_authorization_scope_invalid") from None
    if selected_binding != oauth_binding:
        raise R2RoomStoreError("r2_authorization_scope_mismatch")
    if not _valid_room_store_handoff(metadata):
        raise R2RoomStoreError("room_store_capability_unavailable")

    domain_id = metadata["room_store_domain_id"]
    try:
        recipients = tuple(
            validate_recipient(value)
            for value in _validate_recipients(runtime_config.get("age_recipients"))
        )
        verify_private_path(identity_path, directory=False)
        from .crypto import derive_recipient

        operational_recipient = derive_recipient(identity_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise R2RoomStoreError("r2_encryption_material_unavailable") from None
    if operational_recipient not in recipients:
        raise R2RoomStoreError("r2_encryption_recipient_mismatch")

    authority = R2RoomStoreAuthority(
        session_id=metadata["room_store_session_id"],
        capability=metadata["room_store_capability"],
        physical_binding=domain_id,
        endpoint_transport=_worker_url() if endpoint_transport is None else endpoint_transport,
        identity_path=identity_path,
        recipients=recipients,
    )
    room_store = (
        authority.ensure_material_details()
        if allow_initialize
        else authority.read_material_details()
    )
    if room_store.physical_binding != domain_id:
        raise R2RoomStoreError("room_store_physical_binding_mismatch")
    try:
        validate_encryption_domain_id(room_store.encryption_domain_id)
        if type(room_store.key_generation) is not int or room_store.key_generation < 1:
            raise ValueError("invalid age key generation")
        selected_domain = getattr(dimension, "encryption_domain_id", None)
        if selected_domain is not None and selected_domain != room_store.encryption_domain_id:
            raise ValueError("selected encryption domain mismatch")
        keyset = EncryptionKeyset.create(
            provider="r2",
            endpoint=dimension.endpoint,
            bucket=dimension.bucket,
            operational_identity=_normalize_identity(identity_path.read_text()),
            operational_recipient=operational_recipient,
            recovery_recipients=[value for value in recipients if value != operational_recipient],
            encryption_domain_id=room_store.encryption_domain_id,
            key_generation=room_store.key_generation,
        )
        encryption_material = EncryptionMaterial(keyset, identity_path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise R2RoomStoreError("r2_encryption_domain_mismatch") from None
    return R2RoomStoreSession(authority, room_store, encryption_material)


class EncryptionStateError(RuntimeError):
    """A machine-readable encryption state prevents unsafe implicit migration."""

    def __init__(self, message: str, *, error_code: str, state: str, dimension_id: str, dimension_ids=None):
        self.result = {
            "error_code": error_code,
            "encryption_state": state,
            "dimension_id": dimension_id,
        }
        if dimension_ids is not None:
            self.result["dimension_ids"] = list(dimension_ids)
        super().__init__(message)


def _encryption_material_path(domain_id: str, key_generation: int) -> Path:
    path = _runtime_root() / "encryption" / domain_id / f"generation-{key_generation}.identity"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    return path


def _write_encryption_identity(path: Path, value: str) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError("encryption material path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _material_for_keyset(keyset: EncryptionKeyset, identity_path: Path | None = None) -> EncryptionMaterial:
    if identity_path is not None and Path(identity_path).exists():
        path = Path(identity_path)
        if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError("encryption identity handoff is unsafe")
        if path.stat().st_size > 16 * 1024:
            raise ValueError("encryption identity handoff is too large")
        validate_operational_identity(path.read_text())
        return EncryptionMaterial(keyset, path)

    try:
        identity_value = lookup_encryption_identity(keyset.encryption_domain_id, keyset.key_generation)
    except (RuntimeError, OSError):
        identity_value = keyset.operational_identity
    path = Path(identity_path) if identity_path is not None else _encryption_material_path(keyset.encryption_domain_id, keyset.key_generation)
    material = EncryptionMaterial(keyset, _write_encryption_identity(path, identity_value))
    try:
        store_encryption_identity(keyset.encryption_domain_id, keyset.key_generation, identity_value)
    except (RuntimeError, OSError):
        pass
    return material


def _normalize_generated_identity(value: str) -> str:
    """Keep age-keygen comments out of the keyset while validating one key."""
    identity_lines = [line for line in value.splitlines() if not line.startswith("#")]
    if len(identity_lines) != 1:
        raise ValueError("operational identity is invalid")
    return validate_operational_identity(identity_lines[0])


def _keyset_from_backend(dimension, backend):
    keyset, _etag = _read_keyset_record_from_backend(dimension, backend)
    return keyset


def _read_keyset_record_from_backend(dimension, backend):
    _validate_minio_backend_transport(dimension, backend)
    body, etag = backend.read_control(KEYSET_CONTROL_KEY, 64 * 1024)
    if body is None:
        return None, etag
    try:
        keyset = EncryptionKeyset.from_json(
            body,
            provider=dimension.provider,
            endpoint=dimension.endpoint,
            bucket=dimension.bucket,
        )
    except (TypeError, ValueError) as error:
        raise EncryptionStateError(
            "MinIO encryption keyset is invalid",
            error_code="encryption-keyset-invalid",
            state="failed",
            dimension_id=dimension.dimension_id,
        ) from error
    return keyset, etag


def _validate_minio_backend_transport(dimension, backend):
    if dimension.provider != "minio":
        return
    config = getattr(backend, "config", None)
    option = getattr(dimension, "option", None)
    endpoint = getattr(config, "endpoint", None) or dimension.endpoint
    verify_tls = getattr(config, "verify_tls", option("verify_tls", True) if option else True)
    ca_bundle = getattr(config, "ca_bundle", option("ca_bundle", None) if option else None)
    validate_minio_transport(endpoint, verify_tls=verify_tls, ca_bundle=ca_bundle)


def _assert_keyset_matches_dimension(dimension, keyset):
    if dimension.encryption_domain_id and dimension.encryption_domain_id != keyset.encryption_domain_id:
        raise EncryptionStateError(
            "Dimension encryption domain does not match remote keyset",
            error_code="encryption-domain-mismatch",
            state="failed",
            dimension_id=dimension.dimension_id,
        )
    return keyset


def _require_minio_room_store(dimension):
    if dimension.provider != "minio":
        raise EncryptionStateError(
            "Room Store keyset is unsupported for this provider",
            error_code="room-store-keyset-unsupported",
            state="unsupported",
            dimension_id=dimension.dimension_id,
        )


def _room_store_keyset_error(dimension, message, error_code, cause=None):
    error = EncryptionStateError(
        message,
        error_code=error_code,
        state="failed",
        dimension_id=dimension.dimension_id,
    )
    if cause is not None:
        raise error from cause
    raise error


def _cache_room_store_secret(keyset):
    metadata = keyset.room_store
    store_room_store_secret(keyset.encryption_domain_id, metadata.generation, metadata.secret)


def _validate_room_store_winner(dimension, keyset):
    if keyset is None:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset is unavailable",
            "room-store-keyset-unavailable",
        )
    _assert_keyset_matches_dimension(dimension, keyset)
    if keyset.format_version != ROOM_STORE_KEYSET_FORMAT_VERSION or keyset.room_store is None:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset upgrade outcome is unknown",
            "room-store-keyset-write-unknown",
        )
    return keyset


def _same_room_store_secret(first, second):
    return compare_digest(first.room_store.secret, second.room_store.secret)


def ensure_room_store_keyset(dimension, backend) -> EncryptionKeyset:
    """Explicitly upgrade a MinIO keyset and cache only its durable winner."""
    _require_minio_room_store(dimension)
    keyset, etag = _read_keyset_record_from_backend(dimension, backend)
    if keyset is None:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset is uninitialized; initialize encryption first",
            "room-store-keyset-uninitialized",
        )
    _assert_keyset_matches_dimension(dimension, keyset)
    if keyset.format_version == ROOM_STORE_KEYSET_FORMAT_VERSION:
        _cache_room_store_secret(keyset)
        return keyset
    if not etag:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset upgrade requires a backend version token",
            "room-store-keyset-version-unavailable",
        )
    replace_control = getattr(backend, "replace_control", None)
    if not callable(replace_control):
        _room_store_keyset_error(
            dimension,
            "Room Store keyset upgrade requires conditional control replacement",
            "room-store-keyset-conditional-write-unavailable",
        )

    candidate = keyset.upgrade_for_room_store()
    try:
        replace_control(KEYSET_CONTROL_KEY, candidate.to_json(), etag)
    except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError) as write_error:
        try:
            winner, _winner_etag = _read_keyset_record_from_backend(dimension, backend)
            winner = _validate_room_store_winner(dimension, winner)
        except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError):
            raise write_error
    else:
        try:
            winner, _winner_etag = _read_keyset_record_from_backend(dimension, backend)
            winner = _validate_room_store_winner(dimension, winner)
        except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError) as read_error:
            _room_store_keyset_error(
                dimension,
                "Room Store keyset write could not be verified",
                "room-store-keyset-write-unverified",
                read_error,
            )
        if not _same_room_store_secret(candidate, winner):
            _room_store_keyset_error(
                dimension,
                "Room Store keyset changed during upgrade",
                "room-store-keyset-upgrade-conflict",
            )
        if winner.room_store.generation < candidate.room_store.generation:
            _room_store_keyset_error(
                dimension,
                "Room Store keyset generation regressed during upgrade",
                "room-store-keyset-generation-conflict",
            )
    _cache_room_store_secret(winner)
    return winner


def bind_room_store_repository(
    dimension,
    backend,
    repository_id: str,
    *,
    expected_generation: int,
) -> EncryptionKeyset:
    """Conditionally bind the initialized repository to the winning Room Store keyset."""
    _require_minio_room_store(dimension)
    keyset, etag = _read_keyset_record_from_backend(dimension, backend)
    if keyset is None:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset is uninitialized",
            "room-store-keyset-uninitialized",
        )
    _assert_keyset_matches_dimension(dimension, keyset)
    if keyset.format_version != ROOM_STORE_KEYSET_FORMAT_VERSION:
        _room_store_keyset_error(
            dimension,
            "Room Store keyset upgrade is required before repository binding",
            "room-store-keyset-upgrade-required",
        )
    candidate = keyset.bind_repository(repository_id, expected_generation=expected_generation)
    if candidate is keyset:
        _cache_room_store_secret(keyset)
        return keyset
    if not etag:
        _room_store_keyset_error(
            dimension,
            "Room Store repository binding requires a backend version token",
            "room-store-keyset-version-unavailable",
        )
    replace_control = getattr(backend, "replace_control", None)
    if not callable(replace_control):
        _room_store_keyset_error(
            dimension,
            "Room Store repository binding requires conditional control replacement",
            "room-store-keyset-conditional-write-unavailable",
        )

    try:
        replace_control(KEYSET_CONTROL_KEY, candidate.to_json(), etag)
    except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError) as write_error:
        try:
            winner, _winner_etag = _read_keyset_record_from_backend(dimension, backend)
            winner = _validate_room_store_winner(dimension, winner)
        except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError):
            raise write_error
    else:
        try:
            winner, _winner_etag = _read_keyset_record_from_backend(dimension, backend)
            winner = _validate_room_store_winner(dimension, winner)
        except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError) as read_error:
            _room_store_keyset_error(
                dimension,
                "Room Store repository binding could not be verified",
                "room-store-keyset-write-unverified",
                read_error,
            )

    if (
        not _same_room_store_secret(candidate, winner)
        or winner.room_store.repository_id != repository_id
        or winner.room_store.generation != candidate.room_store.generation
    ):
        _room_store_keyset_error(
            dimension,
            "Room Store repository binding conflicted with another writer",
            "room-store-repository-binding-conflict",
        )
    _cache_room_store_secret(winner)
    return winner


def _resolve_recovery_recipients(recovery_recipients, recovery_handoff):
    if recovery_handoff is None:
        value = os.environ.get("JOSH_ROOM_RECOVERY_HANDOFF")
        recovery_handoff = Path(value) if value else None
    if recovery_handoff is not None and recovery_recipients:
        raise ValueError("recovery recipients and recovery handoff are ambiguous")
    if recovery_handoff is None:
        return list(recovery_recipients or [])
    path = Path(recovery_handoff)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 or mode & 0o077:
            raise ValueError
    except (OSError, ValueError) as error:
        raise ValueError("recovery handoff is unsafe") from error
    from .crypto import derive_recipient

    try:
        return [derive_recipient(path)]
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("recovery handoff is invalid") from error


def resolve_encryption_material(
    dimension,
    backend,
    *,
    recovery_recipients=None,
    recovery_handoff: Path | None = None,
    identity_path: Path | None = None,
    preserve_identity_path: bool = False,
    allow_initialize: bool = True,
    occupied=(),
) -> EncryptionMaterial | None:
    """Resolve one selected Dimension's operational age material.

    R2 remains on its existing Cloudflare-issued identity path. MinIO reads the
    fixed remote keyset and only enrolls an empty physical bucket when callers
    explicitly supply public recovery recipients.
    """
    if dimension.provider == "r2":
        return None
    if dimension.provider != "minio":
        raise ValueError("unsupported encryption provider")
    keyset = _keyset_from_backend(dimension, backend)
    if keyset is not None:
        _assert_keyset_matches_dimension(dimension, keyset)
        return _material_for_keyset(keyset, identity_path)
    handoff = recovery_handoff or os.environ.get("JOSH_ROOM_RECOVERY_HANDOFF")
    if not allow_initialize or not recovery_recipients and not handoff:
        catalog, _catalog_etag = backend.read_catalog()
        if catalog is not None:
            raise EncryptionStateError(
                "MinIO catalog requires explicit encryption migration",
                error_code="legacy-encryption-migration-required",
                state="legacy",
                dimension_id=dimension.dimension_id,
            )
        raise EncryptionStateError(
            "MinIO encryption is uninitialized; provide public recovery recipients",
            error_code="encryption-initialization-required",
            state="uninitialized",
            dimension_id=dimension.dimension_id,
        )
    return ensure_minio_domain(
        dimension,
        backend,
        recovery_recipients=recovery_recipients,
        recovery_handoff=handoff,
        identity_path=identity_path,
        preserve_identity_path=preserve_identity_path,
        occupied=occupied,
    )


def ensure_minio_domain(
    dimension,
    backend,
    *,
    recovery_recipients=None,
    recovery_handoff: Path | None = None,
    identity_path: Path | None = None,
    preserve_identity_path: bool = False,
    occupied=(),
    allow_legacy_migration: bool = False,
) -> EncryptionMaterial:
    """Enroll or reconcile the keyset for one empty MinIO bucket."""
    if dimension.provider != "minio":
        raise ValueError("MinIO encryption initialization requires a MinIO Dimension")
    existing = _keyset_from_backend(dimension, backend)
    if existing is not None:
        _assert_keyset_matches_dimension(dimension, existing)
        return _material_for_keyset(existing, identity_path)
    catalog, _catalog_etag = backend.read_catalog()
    if catalog is not None and not allow_legacy_migration:
        raise EncryptionStateError(
            "MinIO catalog requires explicit encryption migration",
            error_code="legacy-encryption-migration-required",
            state="legacy",
            dimension_id=dimension.dimension_id,
        )
    recovery_recipients = _resolve_recovery_recipients(recovery_recipients, recovery_handoff)
    if not recovery_recipients:
        raise EncryptionStateError(
            "MinIO encryption initialization requires public recovery recipients",
            error_code="encryption-initialization-required",
            state="uninitialized",
            dimension_id=dimension.dimension_id,
        )
    from .crypto import derive_recipient, generate_identity

    candidate_path = Path(identity_path) if identity_path else _runtime_root() / f".enrollment-{uuid.uuid4().hex}.identity"
    candidate_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate_existed = candidate_path.exists()
    keep_candidate = False
    try:
        generate_identity(candidate_path)
    except BaseException:
        if not candidate_existed:
            candidate_path.unlink(missing_ok=True)
        raise
    try:
        operational_identity = _normalize_generated_identity(candidate_path.read_text())
        _write_encryption_identity(candidate_path, operational_identity)
        candidate = EncryptionKeyset.create(
            provider=dimension.provider,
            endpoint=dimension.endpoint,
            bucket=dimension.bucket,
            operational_identity=operational_identity,
            operational_recipient=derive_recipient(candidate_path),
            recovery_recipients=list(recovery_recipients),
            encryption_domain_id=dimension.encryption_domain_id,
            occupied=tuple(occupied),
        )
        try:
            backend.create_control(KEYSET_CONTROL_KEY, candidate.to_json())
            winner = candidate
        except Exception as error:
            if error.__class__.__name__ not in {"R2Conflict", "MinioConflict"} and not (
                getattr(error, "published", None) is False and "conditional" in str(error).lower()
            ):
                raise
            winner = _keyset_from_backend(dimension, backend)
            if winner is None:
                raise RuntimeError("MinIO keyset race outcome is unknown") from error
            _assert_keyset_matches_dimension(dimension, winner)
            if not candidate_existed:
                candidate_path.unlink(missing_ok=True)
        if winner is candidate:
            material = _material_for_keyset(
                winner,
                identity_path or _encryption_material_path(winner.encryption_domain_id, winner.key_generation),
            )
            keep_candidate = identity_path is not None
            return material
        if identity_path is not None and preserve_identity_path and not candidate_existed:
            material = _material_for_keyset(winner, identity_path)
            keep_candidate = True
            return material
        return _material_for_keyset(winner, None)
    finally:
        if not candidate_existed and not keep_candidate:
            candidate_path.unlink(missing_ok=True)


def encryption_status(dimension, backend) -> dict:
    """Read non-secret keyset state without enrollment or Cloudflare access."""
    if dimension.provider == "r2":
        return {
            "state": "legacy-cloudflare",
            "provider": "r2",
            "dimension_id": dimension.dimension_id,
        }
    keyset = _keyset_from_backend(dimension, backend)
    if keyset is not None:
        _assert_keyset_matches_dimension(dimension, keyset)
        identity_path = None
        try:
            _runtime_root().mkdir(parents=True, exist_ok=True, mode=0o700)
            _runtime_root().chmod(0o700)
            with tempfile.NamedTemporaryFile(mode="w", prefix=".remote-identity.", dir=_runtime_root(), delete=False) as handle:
                identity_path = Path(handle.name)
                handle.write(keyset.operational_identity)
            identity_path.chmod(0o600)
            from .crypto import derive_recipient

            if derive_recipient(identity_path) != keyset.operational_recipient:
                raise ValueError("remote operational recipient does not match identity")
        except (OSError, RuntimeError, ValueError) as error:
            raise EncryptionStateError(
                "MinIO encryption keyset identity is invalid",
                error_code="encryption-keyset-invalid",
                state="failed",
                dimension_id=dimension.dimension_id,
            ) from error
        finally:
            if identity_path is not None:
                identity_path.unlink(missing_ok=True)
        return {
            "state": "ready",
            "provider": dimension.provider,
            "dimension_id": dimension.dimension_id,
            "encryption_domain_id": keyset.encryption_domain_id,
            "key_generation": keyset.key_generation,
        }
    catalog, _catalog_etag = backend.read_catalog()
    result = {
        "state": "legacy" if catalog is not None else "uninitialized",
        "provider": dimension.provider,
        "dimension_id": dimension.dimension_id,
    }
    if catalog is not None:
        result["error_code"] = "legacy-encryption-migration-required"
    return result
