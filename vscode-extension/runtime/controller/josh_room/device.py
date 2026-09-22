"""Device enrollment and recipient/credential authority.

Only native OS secret stores are accepted for durable secrets.  The JSON state
keeps public metadata and a receipt digest; it never contains credentials,
age identities, or receipt contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from collections.abc import Mapping
from pathlib import Path

from .config import config_dir
from .encryption_domain import validate_recipient
from .keyring import (
    BackendStatus,
    SecureBackendError,
    backend_status,
    secure_delete,
    secure_lookup,
    secure_store,
)

SCHEMA_VERSION = 1
STATE_NAME = "device.json"
_CLOCK_SKEW_SECONDS = 300


class DeviceError(RuntimeError):
    """A device authority operation failed without disclosing secrets."""


class DeviceUnavailable(DeviceError):
    """The native secret authority cannot be used for prepare/upload."""

    def __init__(self, status: BackendStatus):
        self.status = status
        super().__init__(f"device authority unavailable: {status.reason}")


def state_path() -> Path:
    return config_dir() / STATE_NAME


def _private_path(path: Path) -> bool:
    if os.name == "nt":
        return path.is_file() and not path.is_symlink()
    try:
        return path.is_file() and not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    except OSError:
        return False


def _write_state(state: Mapping[str, object]) -> Path:
    directory = state_path().parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    target = state_path()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _read_state() -> dict[str, object] | None:
    path = state_path()
    if not path.exists():
        return None
    if not _private_path(path):
        raise DeviceError("device state is not private")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DeviceError("device state is unreadable") from error
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise DeviceError("device state schema is unsupported")
    if not isinstance(state.get("device_id"), str) or not state["device_id"]:
        raise DeviceError("device state identity is invalid")
    if not isinstance(state.get("profiles"), dict):
        raise DeviceError("device state profiles are invalid")
    return state


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{label} is invalid")
    if any(ord(char) < 0x21 or ord(char) > 0x7E or char in "\\/:\"'`$;|&<>" for char in value):
        raise ValueError(f"{label} is invalid")
    return value


def _recipients(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("recipient set is required")
    result = [validate_recipient(value, "recipient") for value in values]
    if len(result) != len(set(result)):
        raise ValueError("recipient set contains duplicates")
    return result


def recipient_fingerprint(recipients: list[str] | tuple[str, ...]) -> str:
    body = "\n".join(recipients).encode()
    return hashlib.sha256(body).hexdigest()


def _new_device_id(existing: set[str]) -> str:
    for _ in range(32):
        value = "device-" + secrets.token_hex(16)
        if value not in existing:
            return value
    raise DeviceError("device id generation collided repeatedly")


def _now() -> int:
    return int(time.time())


def _profile_state(
    *,
    profile: str,
    credential_profile: str,
    recipients: list[str],
    created_at: int,
    age_profile: str | None = None,
) -> dict[str, object]:
    return {
        "credential_profile": credential_profile,
        "credential_profiles": [credential_profile],
        "age_profile": age_profile,
        "active_recipient_set_version": 1,
        "recipient_sets": {
            "1": {
                "version": 1,
                "fingerprint": recipient_fingerprint(recipients),
                "recipients": recipients,
                "created_at": created_at,
            }
        },
        "profile": profile,
    }
def _receipt_profile(device_id: str, profile: str) -> str:
    material = f"{len(device_id)}:{device_id}{len(profile)}:{_identifier(profile, 'profile')}".encode()
    return "receipt-" + hashlib.sha256(material).hexdigest()


def _secure_status() -> BackendStatus:
    status = backend_status()
    if not status.available:
        raise DeviceUnavailable(status)
    return status
def _receipt_present(device_id: str, profile: str) -> bool:
    try:
        value = secure_lookup(_receipt_profile(device_id, profile), "receipt")
    except (SecureBackendError, RuntimeError):
        return False
    return bool(value)



def enroll(
    *,
    profile: str,
    credential_profile: str,
    recipients: object,
    credentials: Mapping[str, object] | None = None,
    age_identity: str | None = None,
    age_profile: str | None = None,
    recovery_identity: str | None = None,
    recovery_profile: str | None = None,
    device_id: str | None = None,
    issued_at: int | None = None,
) -> dict[str, object]:
    """Enroll or converge one profile using secrets supplied by stdin only."""

    profile = _identifier(profile, "profile")
    credential_profile = _identifier(credential_profile, "credential profile")
    recipient_values = _recipients(recipients)
    if age_identity is not None and not isinstance(age_identity, str):
        raise ValueError("age identity is invalid")
    if age_profile is not None:
        age_profile = _identifier(age_profile, "age profile")
    if recovery_profile is not None:
        recovery_profile = _identifier(recovery_profile, "recovery profile")
    if recovery_identity is not None and not isinstance(recovery_identity, str):
        raise ValueError("recovery identity is invalid")
    status = _secure_status()
    del status
    existing = _read_state()
    if existing is None:
        selected_id = _new_device_id(set()) if device_id is None else _identifier(device_id, "device id")
        if device_id is not None and _receipt_present(selected_id, profile):
            selected_id = _new_device_id({selected_id})
        state = {
            "schema_version": SCHEMA_VERSION,
            "device_id": selected_id,
            "created_at": _now() if issued_at is None else int(issued_at),
            "profiles": {},
            "recovery_profile": recovery_profile,
        }
    else:
        selected_id = str(existing["device_id"])
        state = existing
        if device_id is not None and device_id != selected_id:
            raise DeviceError("device id does not match local authority")
    profiles = state.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        raise DeviceError("device state profiles are invalid")
    previous = profiles.get(profile)
    if not isinstance(previous, dict):
        previous = None
    if previous is not None:
        sets = previous.get("recipient_sets")
        active = previous.get("active_recipient_set_version")
        current = sets.get(str(active)) if isinstance(sets, dict) else None
        if (
            isinstance(current, dict)
            and current.get("fingerprint") == recipient_fingerprint(recipient_values)
            and previous.get("credential_profile") == credential_profile
            and _receipt_present(selected_id, profile)
        ):
            return inspect(profile=profile) | {"ok": True, "converged": True, "changed": False}
        raise DeviceError("profile is already enrolled; use rotate-new-writes")

    # Import secrets before publishing local state. No secret reaches argv,
    # environment, state files, receipts, exceptions, or JSON output.
    stored_secrets: list[tuple[str, str]] = []

    def store_secret(secret_profile: str, secret_field: str, secret_value: str) -> None:
        try:
            secure_store(secret_profile, secret_field, secret_value)
        except BaseException:
            for old_profile, old_field in reversed(stored_secrets):
                try:
                    secure_delete(old_profile, old_field)
                except SecureBackendError:
                    pass
            raise
        stored_secrets.append((secret_profile, secret_field))
    if credentials is not None:
        if not isinstance(credentials, Mapping):
            raise ValueError("credentials are invalid")
        for field in ("access-key-id", "secret-access-key", "session-token"):
            value = credentials.get(field)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise ValueError("credential value is invalid")
                store_secret(credential_profile, field, value)
    if age_identity is not None:
        if not age_profile:
            raise ValueError("age profile is required with age identity")
        store_secret(age_profile, "age-identity", age_identity)
    if recovery_identity is not None:
        if not recovery_profile:
            raise ValueError("recovery profile is required with recovery identity")
        store_secret(recovery_profile, "age-identity", recovery_identity)
    receipt = secrets.token_urlsafe(32)
    store_secret(_receipt_profile(selected_id, profile), "receipt", receipt)
    profile_body = _profile_state(profile=profile, credential_profile=credential_profile, recipients=recipient_values, created_at=_now(), age_profile=age_profile)
    profiles[profile] = profile_body
    state["recovery_profile"] = recovery_profile or state.get("recovery_profile")
    state["updated_at"] = _now()
    try:
        state_path_value = _write_state(state)
    except BaseException:
        for old_profile, old_field in reversed(stored_secrets):
            try:
                secure_delete(old_profile, old_field)
            except SecureBackendError:
                pass
        raise
    return {
        "ok": True,
        "changed": True,
        "converged": False,
        "device_id": selected_id,
        "profile": profile,
        "credential_profile": credential_profile,
        "recipient_set_version": 1,
        "recipient_set_fingerprint": profile_body["recipient_sets"]["1"]["fingerprint"],
        "state": str(state_path_value),
    }


def inspect(*, profile: str | None = None) -> dict[str, object]:
    status = backend_status()
    state = _read_state()
    result: dict[str, object] = {
        "ok": True,
        "backend": status.to_dict(),
        "device": None,
        "profiles": [],
    }
    if state is None:
        result["device"] = {"enrolled": False}
        return result
    result["device"] = {
        "enrolled": True,
        "device_id": state["device_id"],
        "schema_version": state["schema_version"],
        "created_at": state.get("created_at"),
    }
    profiles = state.get("profiles", {})
    assert isinstance(profiles, dict)
    names = [profile] if profile else sorted(profiles)
    for name in names:
        item = profiles.get(name)
        if not isinstance(item, dict):
            continue
        sets = item.get("recipient_sets", {})
        active = item.get("active_recipient_set_version")
        current = sets.get(str(active)) if isinstance(sets, dict) else None
        result["profiles"].append({
            "profile": name,
            "credential_profile": item.get("credential_profile"),
            "active_recipient_set_version": active,
            "recipient_set_fingerprint": current.get("fingerprint") if isinstance(current, dict) else None,
            "recipient_set_versions": sorted(int(value) for value in sets if str(value).isdigit()) if isinstance(sets, dict) else [],
        })
    return result


def rotate_new_writes(*, profile: str, recipients: object, credential_profile: str | None = None) -> dict[str, object]:
    profile = _identifier(profile, "profile")
    recipient_values = _recipients(recipients)
    _secure_status()
    state = _read_state()
    if state is None:
        raise DeviceError("device is not enrolled")
    device_id = str(state["device_id"])
    profiles = state.get("profiles")
    if not isinstance(profiles, dict) or not isinstance(profiles.get(profile), dict):
        raise DeviceError("profile is not enrolled")
    if not _receipt_present(device_id, profile):
        raise DeviceError("device enrollment receipt is unavailable")
    item = profiles[profile]
    sets = item.get("recipient_sets")
    if not isinstance(sets, dict):
        raise DeviceError("recipient set history is malformed")
    current_version = int(item.get("active_recipient_set_version", 0))
    fingerprint = recipient_fingerprint(recipient_values)
    for value in sets.values():
        if isinstance(value, dict) and value.get("fingerprint") == fingerprint:
            raise DeviceError("recipient set already exists")
    next_version = max([int(value) for value in sets if str(value).isdigit()] + [current_version]) + 1
    selected_credential_profile = item.get("credential_profile") if credential_profile is None else _identifier(credential_profile, "credential profile")
    sets[str(next_version)] = {
        "version": next_version,
        "fingerprint": fingerprint,
        "recipients": recipient_values,
        "created_at": _now(),
    }
    if not isinstance(selected_credential_profile, str):
        raise DeviceError("credential profile binding is malformed")
    credential_profiles = item.setdefault("credential_profiles", [selected_credential_profile])
    if not isinstance(credential_profiles, list):
        raise DeviceError("credential profile history is malformed")
    if selected_credential_profile not in credential_profiles:
        credential_profiles.append(selected_credential_profile)
    item["active_recipient_set_version"] = next_version
    item["credential_profile"] = selected_credential_profile
    state["updated_at"] = _now()
    _write_state(state)
    return {
        "ok": True,
        "changed": True,
        "device_id": device_id,
        "profile": profile,
        "recipient_set_version": next_version,
        "recipient_set_fingerprint": fingerprint,
        "writes_use_version": next_version,
        "historical_versions_readable": sorted(int(value) for value in sets if str(value).isdigit()),
        "old_ciphertexts_preserved": True,
    }


def active_recipients(*, profile: str | None = None) -> list[str] | None:
    profile = profile or os.environ.get("JOSH_ROOM_DEVICE_PROFILE")
    state = _read_state()
    if state is None:
        return None
    profiles = state.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return None
    if profile is None and len(profiles) != 1:
        raise DeviceError("multiple device profiles require explicit selection")
    selected = profiles.get(profile) if profile is not None else next(iter(profiles.values()))
    if not isinstance(selected, dict):
        raise DeviceError("selected device profile is unavailable")
    sets = selected.get("recipient_sets")
    version = selected.get("active_recipient_set_version")
    current = sets.get(str(version)) if isinstance(sets, dict) else None
    recipients = current.get("recipients") if isinstance(current, dict) else None
    if not isinstance(recipients, list) or not recipients:
        raise DeviceError("active recipient set is unavailable")
    return list(recipients)
def active_age_profile(*, profile: str | None = None) -> str | None:
    profile = profile or os.environ.get("JOSH_ROOM_DEVICE_PROFILE")
    state = _read_state()
    if state is None:
        return None
    profiles = state.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return None
    if profile is None and len(profiles) != 1:
        raise DeviceError("multiple device profiles require explicit selection")
    selected = profiles.get(profile) if profile is not None else next(iter(profiles.values()))
    value = selected.get("age_profile") if isinstance(selected, dict) else None
    return value if isinstance(value, str) else None
def active_credential_profile(*, profile: str | None = None) -> str | None:
    profile = profile or os.environ.get("JOSH_ROOM_DEVICE_PROFILE")
    state = _read_state()
    if state is None:
        return None
    profiles = state.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return None
    if profile is None and len(profiles) != 1:
        raise DeviceError("multiple device profiles require explicit selection")
    selected = profiles.get(profile) if profile is not None else next(iter(profiles.values()))
    value = selected.get("credential_profile") if isinstance(selected, dict) else None
    return value if isinstance(value, str) else None



def read_recipient_sets(*, profile: str) -> list[dict[str, object]]:
    state = _read_state()
    if state is None:
        raise DeviceError("device is not enrolled")
    profiles = state.get("profiles", {})
    item = profiles.get(profile) if isinstance(profiles, dict) else None
    sets = item.get("recipient_sets") if isinstance(item, dict) else None
    if not isinstance(sets, dict):
        raise DeviceError("profile is not enrolled")
    return [sets[key] for key in sorted(sets, key=lambda value: int(value)) if isinstance(sets[key], dict)]

def remove_local(*, profile: str | None = None) -> dict[str, object]:
    _secure_status()
    state = _read_state()
    if state is None:
        return {"ok": True, "changed": False, "removed": False}
    device_id = str(state["device_id"])
    profiles = state.get("profiles", {})
    if not isinstance(profiles, dict):
        raise DeviceError("device state profiles are invalid")
    names = [profile] if profile else list(profiles)
    remaining_names = set(profiles) - set(names)
    keep_credentials: set[str] = set()
    keep_age_profiles: set[str] = set()
    for remaining_name in remaining_names:
        remaining = profiles.get(remaining_name)
        if not isinstance(remaining, dict):
            continue
        history = remaining.get("credential_profiles", [remaining.get("credential_profile")])
        keep_credentials.update(value for value in history if isinstance(value, str))
        age_value = remaining.get("age_profile")
        if isinstance(age_value, str):
            keep_age_profiles.add(age_value)
    cleanup_errors: list[str] = []
    for name in names:
        item = profiles.get(name)
        if not isinstance(item, dict):
            continue
        credential_profiles = item.get("credential_profiles", [item.get("credential_profile")])
        if not isinstance(credential_profiles, list):
            raise DeviceError("credential profile history is malformed")
        for credential_profile in credential_profiles:
            if not isinstance(credential_profile, str) or credential_profile in keep_credentials:
                continue
            if not isinstance(credential_profile, str):
                continue
            for field in ("access-key-id", "secret-access-key", "session-token"):
                try:
                    secure_delete(credential_profile, field)
                except SecureBackendError:
                    cleanup_errors.append(f"{credential_profile}:{field}")
        age_profile = item.get("age_profile")
        if isinstance(age_profile, str) and age_profile not in keep_age_profiles:
            try:
                secure_delete(age_profile, "age-identity")
            except SecureBackendError:
                cleanup_errors.append(f"{age_profile}:age-identity")
        try:
            secure_delete(_receipt_profile(device_id, name), "receipt")
        except SecureBackendError:
            cleanup_errors.append(f"{name}:receipt")
        profiles.pop(name, None)
    if cleanup_errors:
        raise DeviceError("local secret cleanup incomplete")
    if profile is not None and profiles:
        state["updated_at"] = _now()
        _write_state(state)
        return {"ok": True, "changed": True, "removed": True, "device_id": device_id, "profile": profile}
    recovery_profile = state.get("recovery_profile")
    if isinstance(recovery_profile, str):
        try:
            secure_delete(recovery_profile, "age-identity")
        except SecureBackendError:
            raise DeviceError("local secret cleanup incomplete")
    path = state_path()
    path.unlink(missing_ok=True)
    return {"ok": True, "changed": True, "removed": True, "device_id": device_id}
def doctor() -> dict[str, object]:
    status = backend_status()
    state = None
    checks: list[dict[str, object]] = []
    try:
        state = _read_state()
        checks.append({"name": "state", "ok": True, "detail": "private device state is readable"})
    except DeviceError as error:
        checks.append({"name": "state", "ok": False, "detail": str(error)})
    if state is None:
        checks.append({"name": "enrollment", "ok": False, "detail": "device is not enrolled"})
    else:
        now = _now()
        created = state.get("created_at")
        updated = state.get("updated_at", created)
        skewed = any(
            isinstance(value, (int, float)) and value > now + _CLOCK_SKEW_SECONDS
            for value in (created, updated)
        )
        checks.append({"name": "clock", "ok": not skewed, "detail": "clock appears usable" if not skewed else "device state timestamp is ahead of local clock"})
        profiles = state.get("profiles", {})
        for name in sorted(profiles) if isinstance(profiles, dict) else []:
            receipt_ok = _receipt_present(str(state["device_id"]), name)
            checks.append({"name": f"receipt:{name}", "ok": receipt_ok, "detail": "private enrollment receipt available" if receipt_ok else "private enrollment receipt missing; config may have been copied"})
    checks.append({"name": "backend", "ok": status.available, "detail": status.reason, "diagnostics": list(status.diagnostics)})
    for check in checks:
        check.setdefault("remediation", "No action required." if check["ok"] else "Run device doctor after restoring the native secret authority.")
    ok = all(bool(check["ok"]) for check in checks)
    return {
        "ok": ok,
        "backend": status.to_dict(),
        "checks": checks,
        "prepare_allowed": ok,
        "upload_allowed": ok,
        "hook_enqueue_allowed": True,
        "historical_revocation": "not_supported; previously published ciphertexts remain decryptable to holders of old recipients",
    }

def require_prepare_upload() -> None:
    report = doctor()
    if not report["prepare_allowed"]:
        status = report.get("backend", {})
        reason = status.get("reason", "device-not-ready") if isinstance(status, dict) else "device-not-ready"
        raise DeviceError(f"device authority is not ready: {reason}")


__all__ = [
    "SCHEMA_VERSION",
    "DeviceError",
    "DeviceUnavailable",
    "active_age_profile",
    "active_credential_profile",
    "active_recipients",
    "backend_status",
    "doctor",
    "enroll",
    "inspect",
    "read_recipient_sets",
    "recipient_fingerprint",
    "remove_local",
    "require_prepare_upload",
    "rotate_new_writes",
    "state_path",
]
