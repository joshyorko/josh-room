"""Policy-bound contracts for encrypted PCC session-evidence objects.

The streaming age implementation is layered on these small, transport-neutral
contracts.  This module never logs or includes recipient values in errors.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .crypto import _managed_executable
from .pcc_outbox import OutboxError, PccOutbox, QueueState
from .policy import CaptureProfile
from .session_evidence import (
    CONTENT_TYPES,
    CURRENT_MAJOR,
    canonical_json,
    validate_document,
)
from .session_normalizer import NormalizationEvent

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NATIVE_RECIPIENT = re.compile(r"^age1[0-9a-z]{58}$")
_PLUGIN_RECIPIENT = re.compile(r"^AGE-PLUGIN-[A-Za-z0-9][A-Za-z0-9._-]{0,4094}$")
_SSH_RECIPIENT_TYPES = frozenset(
    {
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "ssh-ed25519",
        "ssh-rsa",
    }
)
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_FORMAT = "josh-room.pcc-evidence"
_FORMAT_VERSION = 1


class CryptoErrorCode(StrEnum):
    RECIPIENT_INVALID = "recipient-invalid"
    RECIPIENT_UNSUPPORTED = "recipient-unsupported"
    RECIPIENT_DUPLICATE = "recipient-duplicate"
    RECIPIENT_DAILY_MISSING = "recipient-daily-missing"
    RECIPIENT_RECOVERY_MISSING = "recipient-recovery-missing"
    RECIPIENT_REFERENCE_MISMATCH = "recipient-reference-mismatch"
    RECIPIENT_SET_UNAVAILABLE = "recipient-set-unavailable"
    ASSET_PAYLOAD_MISSING = "asset-payload-missing"
    PROFILE_MISMATCH = "profile-mismatch"
    DOCUMENT_INVALID = "document-invalid"
    UNKNOWN_SCHEMA = "unknown-schema"
    MANIFEST_INVALID = "manifest-invalid"
    MANIFEST_MISMATCH = "manifest-mismatch"
    ENVELOPE_INVALID = "envelope-invalid"
    PAYLOAD_DIGEST_MISMATCH = "payload-digest-mismatch"
    PAYLOAD_SIZE_MISMATCH = "payload-size-mismatch"
    AGE_UNAVAILABLE = "age-unavailable"
    AGE_FAILED = "age-failed"
    CANCELLED = "cancelled"
    OUTPUT_FAILED = "output-failed"
    PUBLICATION_FAILED = "publication-failed"
    CAPTURE_GAP = "capture-gap"
    OUTBOX_PRECONDITION = "outbox-precondition"


class CryptoError(RuntimeError):
    """Public-safe failure with no untrusted values in its message."""

    def __init__(self, code: CryptoErrorCode):
        self.code = CryptoErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class RecipientSet:
    """Host-owned public recipients grouped by their operational role."""

    reference: str
    version: int
    daily_use: Sequence[str] = ()
    recovery: Sequence[str] = ()
    additional: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class ResolvedRecipients:
    reference: str
    version: int
    ordered: tuple[str, ...]
    fingerprint: str
    daily_use: tuple[str, ...]
    recovery: tuple[str, ...]
    additional: tuple[str, ...]


RecipientResolver = Callable[[str], RecipientSet | Mapping[str, object]]
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
_IO_CHUNK = 64 * 1024


def _fail(code: CryptoErrorCode) -> None:
    raise CryptoError(code)


def _valid_recipient(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    if value != value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    return not value.upper().startswith("AGE-SECRET-KEY-")


def _canonicalize_recipient(value: object) -> str:
    """Validate and canonicalize recipient forms supported by age itself."""

    if not _valid_recipient(value):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    assert isinstance(value, str)
    if value.startswith("age1"):
        if not _NATIVE_RECIPIENT.fullmatch(value):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        try:
            from .encryption_domain import validate_recipient

            validate_recipient(value)
        except (ImportError, TypeError, ValueError):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        return value

    parts = value.split()
    if parts and parts[0] in _SSH_RECIPIENT_TYPES:
        if len(parts) < 2:
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        try:
            decoded = base64.b64decode(parts[1], validate=True)
        except (ValueError, TypeError):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        key_type = parts[0].encode("ascii")
        if len(decoded) < 5 or int.from_bytes(decoded[:4], "big") != len(key_type):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        if decoded[4 : 4 + len(key_type)] != key_type or len(decoded) <= 4 + len(key_type):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        # SSH comments are not part of the recipient identity.  Removing them
        # makes fingerprints stable without exposing host-config annotations.
        return f"{parts[0]} {parts[1]}"

    if value.startswith("AGE-PLUGIN-"):
        if not _PLUGIN_RECIPIENT.fullmatch(value):
            _fail(CryptoErrorCode.RECIPIENT_INVALID)
        # Plugin-specific parsing belongs to the installed age plugin.  The
        # bounded shape check above keeps this opaque value safe for argv and
        # lets age report an unavailable/unsupported plugin at use time.
        return value

    _fail(CryptoErrorCode.RECIPIENT_UNSUPPORTED)


def _recipient_reference(value: object) -> str:
    if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    return value


def _as_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    return tuple(value)


def _coerce_recipient_set(value: RecipientSet | Mapping[str, object]) -> RecipientSet:
    if isinstance(value, RecipientSet):
        return value
    if not isinstance(value, Mapping):
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)
    try:
        return RecipientSet(
            reference=value["reference"],
            version=value["version"],
            daily_use=_as_sequence(value.get("daily_use", ())),
            recovery=_as_sequence(value.get("recovery", ())),
            additional=_as_sequence(value.get("additional", ())),
        )
    except (KeyError, TypeError):
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)


def resolve_recipients(profile: CaptureProfile, resolver: RecipientResolver) -> ResolvedRecipients:
    """Resolve recipients only through the host-owned profile reference."""

    reference = _recipient_reference(profile.recipient_set_ref)
    if not callable(resolver):
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)
    try:
        recipient_set = _coerce_recipient_set(resolver(reference))
    except CryptoError:
        raise
    except Exception:  # noqa: BLE001 - resolver failures fail closed without details
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)

    if recipient_set.reference != reference:
        _fail(CryptoErrorCode.RECIPIENT_REFERENCE_MISMATCH)
    if type(recipient_set.version) is not int or recipient_set.version < 1:
        _fail(CryptoErrorCode.RECIPIENT_INVALID)

    roles = {
        "daily": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipient_set.daily_use)),
        "recovery": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipient_set.recovery)),
        "additional": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipient_set.additional)),
    }
    if profile.destination.kind == "private-r2" and not roles["daily"]:
        _fail(CryptoErrorCode.RECIPIENT_DAILY_MISSING)
    if profile.destination.kind == "private-r2" and not roles["recovery"]:
        _fail(CryptoErrorCode.RECIPIENT_RECOVERY_MISSING)
    if not any(roles.values()):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)

    flattened = [recipient for values in roles.values() for recipient in values]
    if len(flattened) != len(set(flattened)):
        _fail(CryptoErrorCode.RECIPIENT_DUPLICATE)
    ordered = tuple(sorted(flattened))
    fingerprint_payload = {
        "reference": reference,
        "version": recipient_set.version,
        "recipients": ordered,
    }
    fingerprint = hashlib.sha256(canonical_json(fingerprint_payload)).hexdigest()
    return ResolvedRecipients(
        reference,
        recipient_set.version,
        ordered,
        fingerprint,
        roles["daily"],
        roles["recovery"],
        roles["additional"],
    )


def _document_or_fail(document: object) -> dict[str, Any]:
    result = validate_document(document)
    if result.disposition.value == "quarantined":
        _fail(CryptoErrorCode.UNKNOWN_SCHEMA)
    if result.document is None:
        _fail(CryptoErrorCode.DOCUMENT_INVALID)
    return result.document


def _profile_binding(document: Mapping[str, Any], profile: CaptureProfile) -> dict[str, str]:
    document_profile = document.get("profile_id")
    document_workspace = document.get("workspace_id")
    if document_profile is not None and document_profile != profile.profile_id:
        _fail(CryptoErrorCode.PROFILE_MISMATCH)
    if document_workspace is not None and document_workspace != profile.workspace_id:
        _fail(CryptoErrorCode.PROFILE_MISMATCH)
    return {"id": profile.profile_id, "workspace_id": profile.workspace_id}


def _validate_queue_binding(
    event: NormalizationEvent,
    document: Mapping[str, Any],
    queued: Any,
    profile: CaptureProfile,
) -> None:
    """Bind the #8 queue record to the exact normalized #9 event."""

    document_session = document.get("session_id")
    if (
        queued.event_id != document.get("event_id")
        or queued.event_id not in queued.event_ids
        or not isinstance(document_session, str)
        or queued.session_id != document_session
        or (
            "checkpoint" in document
            and queued.checkpoint != document["checkpoint"]
        )
        or event.kind != document.get("kind")
    ):
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)

    metadata = queued.metadata
    expected_workspace = document.get("workspace_id")
    if not isinstance(expected_workspace, str) or metadata.get("workspace_id") != expected_workspace:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    if metadata.get("object_kind") != document.get("kind"):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if metadata.get("destination_class") != profile.destination.kind:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    if profile.destination.binding_id is not None and metadata.get("destination_binding_id") != profile.destination.binding_id:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)

    source = document.get("source")
    if not isinstance(source, Mapping):
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    for metadata_key, document_key in (
        ("source_surface", "surface"),
        ("source_adapter", "adapter"),
        ("source_adapter_version", "adapter_version"),
    ):
        if metadata.get(metadata_key) != source.get(document_key):
            _fail(CryptoErrorCode.MANIFEST_MISMATCH)

    capture = document.get("capture")
    if not isinstance(capture, Mapping):
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    for metadata_key, document_key in (
        ("policy_decision", "policy_decision"),
        ("capture_status", "status"),
    ):
        if metadata.get(metadata_key) != capture.get(document_key):
            _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    if "sensitivity" in capture and metadata.get("sensitivity") != capture["sensitivity"]:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)



def _references(document: Mapping[str, Any]) -> dict[str, Any]:
    kind = document["kind"]
    if kind == "session-segment":
        return {
            "previous_segment_sha256": document.get("previous_segment_sha256"),
            "asset_refs": document.get("asset_refs", []),
        }
    if kind == "session-final":
        return {"last_segment_sha256": document.get("last_segment_sha256")}
    if kind == "session-asset":
        return {
            "asset_id": document["asset_id"],
            "source_event_id": document["source_event_id"],
        }
    return {
        "evidence_kind": document["evidence_kind"],
        "evidence_event_id": document["evidence_event_id"],
    }


def _payload_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    kind = document["kind"]
    if kind == "session-asset":
        return {"asset_id": document["asset_id"], "sha256": document["sha256"], "size": document["size"]}
    if kind in {"session-segment", "session-final"}:
        return {"content_sha256": document["content_sha256"], "content_size": document["content_size"]}
    return {
        "evidence_kind": document["evidence_kind"],
        "evidence_event_id": document["evidence_event_id"],
        "content_sha256": document["content_sha256"],
        "ciphertext_sha256": document["ciphertext_sha256"],
        "ciphertext_size": document["ciphertext_size"],
    }


def _source_provenance(document: Mapping[str, Any]) -> dict[str, Any]:
    source = document.get("source")
    return {"source": dict(source) if isinstance(source, Mapping) else None}


def _policy_result(document: Mapping[str, Any], profile: CaptureProfile) -> dict[str, Any]:
    capture = document.get("capture")
    if not isinstance(capture, Mapping):
        if document.get("kind") == "index-event":
            return {
                "decision": "allow",
                "sensitivity": "unknown",
                "status": "complete",
                "policy_version": profile.policy_version,
                "provenance": profile.provenance,
            }
        _fail(CryptoErrorCode.DOCUMENT_INVALID)
    return {
        "decision": capture.get("policy_decision"),
        "sensitivity": capture.get("sensitivity"),
        "status": capture.get("status"),
        "policy_version": profile.policy_version,
        "provenance": profile.provenance,
    }


def _validate_resolved_recipients(recipients: ResolvedRecipients, profile: CaptureProfile) -> None:
    """Reject forged or internally inconsistent resolved-recipient values."""

    if not isinstance(recipients, ResolvedRecipients):
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)
    reference = _recipient_reference(recipients.reference)
    if type(recipients.version) is not int or recipients.version < 1:
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    roles = {
        "daily": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipients.daily_use)),
        "recovery": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipients.recovery)),
        "additional": tuple(_canonicalize_recipient(value) for value in _as_sequence(recipients.additional)),
    }
    if profile.destination.kind == "private-r2" and (not roles["daily"] or not roles["recovery"]):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    flattened = [recipient for values in roles.values() for recipient in values]
    if not flattened or len(flattened) != len(set(flattened)):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)
    ordered = tuple(sorted(flattened))
    expected_fingerprint = hashlib.sha256(
        canonical_json({"reference": reference, "version": recipients.version, "recipients": ordered})
    ).hexdigest()
    if (
        tuple(recipients.daily_use) != roles["daily"]
        or tuple(recipients.recovery) != roles["recovery"]
        or tuple(recipients.additional) != roles["additional"]
        or recipients.ordered != ordered
        or recipients.fingerprint != expected_fingerprint
    ):
        _fail(CryptoErrorCode.RECIPIENT_INVALID)


def build_manifest(
    document: Mapping[str, Any],
    profile: CaptureProfile,
    recipients: ResolvedRecipients,
    payload_sha256: str,
    payload_size: int,
) -> dict[str, Any]:
    """Build the encrypted manifest for exactly one validated typed payload."""

    validated = _document_or_fail(dict(document))
    if not isinstance(payload_sha256, str) or not _DIGEST.fullmatch(payload_sha256):
        _fail(CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH)
    if type(payload_size) is not int or payload_size < 0:
        _fail(CryptoErrorCode.PAYLOAD_SIZE_MISMATCH)
    profile_binding = _profile_binding(validated, profile)
    _validate_resolved_recipients(recipients, profile)
    if recipients.reference != profile.recipient_set_ref:
        _fail(CryptoErrorCode.RECIPIENT_REFERENCE_MISMATCH)
    schema_version = validated["schema_version"]
    manifest = {
        "format": _FORMAT,
        "format_version": _FORMAT_VERSION,
        "schema": {
            "name": validated["schema_name"],
            "major": schema_version["major"],
            "minor": schema_version["minor"],
        },
        "kind": validated["kind"],
        "event_id": validated["event_id"],
        "payload": {"sha256": payload_sha256, "size": payload_size},
        "payload_name": "payload.bin" if validated["kind"] == "session-asset" else "payload.json",
        "profile": profile_binding,
        "provenance": _source_provenance(validated),
        "policy": _policy_result(validated, profile),
        "recipient_set": {
            "reference": recipients.reference,
            "version": recipients.version,
            "fingerprint": recipients.fingerprint,
        },
        "references": _references(validated),
        "payload_identity": _payload_identity(validated),
    }
    if validated["kind"] == "session-asset":
        manifest["asset_document"] = validated
    return manifest


def validate_manifest(
    manifest: Mapping[str, Any],
    document: Mapping[str, Any],
    payload_sha256: str,
    payload_size: int,
) -> None:
    """Validate every binding before a decrypted object is accepted."""

    if not isinstance(manifest, Mapping) or manifest.get("format") != _FORMAT or manifest.get("format_version") != _FORMAT_VERSION:
        _fail(CryptoErrorCode.MANIFEST_INVALID)
    validated = _document_or_fail(dict(document))
    schema = manifest.get("schema")
    if not isinstance(schema, Mapping) or schema.get("name") != validated.get("schema_name"):
        _fail(CryptoErrorCode.UNKNOWN_SCHEMA)
    if schema.get("major") != CURRENT_MAJOR:
        _fail(CryptoErrorCode.UNKNOWN_SCHEMA)
    if schema.get("minor") != validated.get("schema_version", {}).get("minor"):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if manifest.get("kind") != validated.get("kind") or manifest.get("event_id") != validated.get("event_id"):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    payload = manifest.get("payload")
    if not isinstance(payload, Mapping):
        _fail(CryptoErrorCode.MANIFEST_INVALID)
    if payload.get("sha256") != payload_sha256:
        _fail(CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH)
    if payload.get("size") != payload_size:
        _fail(CryptoErrorCode.PAYLOAD_SIZE_MISMATCH)
    expected_payload_name = "payload.bin" if validated["kind"] == "session-asset" else "payload.json"
    if manifest.get("payload_name") != expected_payload_name:
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    profile = manifest.get("profile")
    if not isinstance(profile, Mapping):
        _fail(CryptoErrorCode.MANIFEST_INVALID)
    if profile.get("id") != validated.get("profile_id", profile.get("id")) or profile.get("workspace_id") != validated.get("workspace_id", profile.get("workspace_id")):
        _fail(CryptoErrorCode.PROFILE_MISMATCH)
    recipient_set = manifest.get("recipient_set")
    if (
        not isinstance(recipient_set, Mapping)
        or not _REFERENCE.fullmatch(recipient_set.get("reference", ""))
        or type(recipient_set.get("version")) is not int
        or recipient_set["version"] < 1
        or not isinstance(recipient_set.get("fingerprint"), str)
        or not _DIGEST.fullmatch(recipient_set["fingerprint"])
    ):
        _fail(CryptoErrorCode.MANIFEST_INVALID)
    if manifest.get("provenance") != _source_provenance(validated):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if manifest.get("references") != _references(validated):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if manifest.get("payload_identity") != _payload_identity(validated):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if validated["kind"] == "session-asset" and manifest.get("asset_document") != validated:
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)


@dataclass(frozen=True, slots=True)
class CiphertextReceipt:
    path: Path
    sha256: str
    size: int
    event_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class DecryptedEnvelope:
    manifest: dict[str, Any]
    document: dict[str, Any]
    payload: bytes
    payload_sha256: str
    payload_size: int


@dataclass(frozen=True, slots=True)
class PreparedReceipt:
    event_id: str
    kind: str
    ciphertext_sha256: str
    ciphertext_size: int


class _Cancelled(Exception):
    pass


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None:
        try:
            cancelled = cancel_check()
        except Exception:  # noqa: BLE001 - cancellation failures fail closed
            cancelled = True
        if cancelled:
            raise _Cancelled


class _IteratorReader:
    def __init__(self, chunks: Sequence[bytes] | Any, cancel_check: Callable[[], bool] | None):
        self._iterator = iter((chunks,)) if isinstance(chunks, bytes) else iter(chunks)
        self._pending = b""
        self._cancel_check = cancel_check
        self.digest = hashlib.sha256()
        self.size = 0
        self.finished = False

    def read(self, amount: int = -1) -> bytes:
        if self.finished:
            return b""
        limit = _IO_CHUNK if amount is None or amount < 0 else max(1, amount)
        while len(self._pending) < limit:
            _check_cancel(self._cancel_check)
            try:
                chunk = next(self._iterator)
            except StopIteration:
                self.finished = True
                break
            if not isinstance(chunk, bytes):
                raise TypeError
            if not chunk:
                continue
            self.digest.update(chunk)
            self.size += len(chunk)
            if self.size > MAX_PAYLOAD_BYTES:
                raise OverflowError
            self._pending += chunk
        result = self._pending[:limit]
        self._pending = self._pending[limit:]
        return result


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        if os.name == "posix":
            raise
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_tar_member(archive: tarfile.TarFile, name: str, size: int, source: Any) -> None:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    archive.addfile(info, source)


def _write_envelope(
    stdin: Any,
    event: NormalizationEvent,
    manifest: Mapping[str, Any],
    payload: bytes | Sequence[bytes] | Any,
    cancel_check: Callable[[], bool] | None,
) -> _IteratorReader | None:
    asset_reader = None
    with tarfile.open(fileobj=stdin, mode="w|", format=tarfile.GNU_FORMAT) as archive:
        manifest_bytes = canonical_json(manifest)
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise OverflowError
        _write_tar_member(archive, "manifest.json", len(manifest_bytes), io.BytesIO(manifest_bytes))
        _check_cancel(cancel_check)
        if event.kind == "session-asset":
            asset_reader = _IteratorReader(payload, cancel_check)
            _write_tar_member(archive, "payload.bin", manifest["payload"]["size"], asset_reader)
        else:
            _write_tar_member(archive, "payload.json", len(payload), io.BytesIO(payload))
    return asset_reader


def _drain_stdout(stdout: Any, target: Path, errors: list[BaseException]) -> None:
    try:
        digest = hashlib.sha256()
        size = 0
        with target.open("wb") as handle:
            while True:
                chunk = stdout.read(_IO_CHUNK)
                if not chunk:
                    break
                written = handle.write(chunk)
                if written != len(chunk):
                    raise OSError
                digest.update(chunk)
                size += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        errors.append((digest.hexdigest(), size))
    except BaseException as error:  # noqa: BLE001 - owner thread converts safely
        errors.append(error)


def _drain_stderr(stderr: Any) -> None:
    try:
        while stderr.read(_IO_CHUNK):
            pass
    except OSError:
        pass


def _age_process(
    recipients: ResolvedRecipients,
    age_executable: str | os.PathLike[str] | None,
) -> subprocess.Popen[Any]:
    try:
        executable = Path(age_executable) if age_executable is not None else _managed_executable("age")
    except (OSError, ValueError, RuntimeError):
        _fail(CryptoErrorCode.AGE_UNAVAILABLE)
    args = [str(executable)]
    for recipient in recipients.ordered:
        args.extend(("-r", recipient))
    try:
        return subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except (OSError, ValueError):
        _fail(CryptoErrorCode.AGE_UNAVAILABLE)


def stream_encrypt(
    event: NormalizationEvent,
    recipients: ResolvedRecipients,
    output: str | os.PathLike[str],
    *,
    profile: CaptureProfile | None = None,
    payload: Sequence[bytes] | Any | None = None,
    age_executable: str | os.PathLike[str] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> CiphertextReceipt:
    """Stream one normalized event through age into an atomic ciphertext file."""

    if not isinstance(event, NormalizationEvent) or not isinstance(event.document, Mapping):
        _fail(CryptoErrorCode.DOCUMENT_INVALID)
    if event.kind != event.document.get("kind"):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    if not isinstance(recipients, ResolvedRecipients) or not recipients.ordered:
        _fail(CryptoErrorCode.RECIPIENT_SET_UNAVAILABLE)
    document = _document_or_fail(dict(event.document))
    if event.kind == "session-asset":
        if payload is None or event.asset_receipt is None:
            _fail(CryptoErrorCode.ASSET_PAYLOAD_MISSING)
        if event.asset_receipt.sha256 != document.get("sha256") or event.asset_receipt.size != document.get("size"):
            _fail(CryptoErrorCode.MANIFEST_MISMATCH)
        payload_size = document["size"]
        payload_sha256 = document["sha256"]
        envelope_payload: bytes | Sequence[bytes] | Any = payload
    else:
        envelope_payload = canonical_json(document)
        payload_size = len(envelope_payload)
        if payload_size > MAX_PAYLOAD_BYTES:
            _fail(CryptoErrorCode.PAYLOAD_SIZE_MISMATCH)
        payload_sha256 = hashlib.sha256(envelope_payload).hexdigest()
    effective_profile = profile or _profile_from_manifest_context(document, recipients)
    manifest = build_manifest(document, effective_profile, recipients, payload_sha256, payload_size)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    process = None
    output_errors: list[BaseException | tuple[str, int]] = []
    stderr_thread = None
    stdout_thread = None
    try:
        _check_cancel(cancel_check)
        process = _age_process(recipients, age_executable)
        stdout_thread = __import__("threading").Thread(target=_drain_stdout, args=(process.stdout, temporary, output_errors), daemon=True)
        stderr_thread = __import__("threading").Thread(target=_drain_stderr, args=(process.stderr,), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            asset_reader = _write_envelope(process.stdin, event, manifest, envelope_payload, cancel_check)
            process.stdin.close()
        except _Cancelled:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            raise
        except BrokenPipeError:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                return_code = process.wait(timeout=60)
            except subprocess.TimeoutExpired as error:
                raise CryptoError(CryptoErrorCode.AGE_FAILED) from error
            if return_code != 0:
                raise CryptoError(CryptoErrorCode.AGE_FAILED)
            raise CryptoError(CryptoErrorCode.OUTPUT_FAILED)
        except (OSError, TypeError, OverflowError, tarfile.TarError):
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            raise CryptoError(CryptoErrorCode.OUTPUT_FAILED)
        try:
            return_code = process.wait(timeout=60)
        except subprocess.TimeoutExpired as error:
            raise CryptoError(CryptoErrorCode.AGE_FAILED) from error
        stdout_thread.join()
        stderr_thread.join()
        if output_errors and isinstance(output_errors[0], BaseException):
            raise CryptoError(CryptoErrorCode.OUTPUT_FAILED)
        if return_code != 0:
            raise CryptoError(CryptoErrorCode.AGE_FAILED)
        if event.kind == "session-asset":
            if asset_reader is None or asset_reader.size != payload_size:
                raise CryptoError(CryptoErrorCode.PAYLOAD_SIZE_MISMATCH)
            if asset_reader.digest.hexdigest() != payload_sha256:
                raise CryptoError(CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH)
        _check_cancel(cancel_check)
        if not output_errors or not isinstance(output_errors[0], tuple):
            raise CryptoError(CryptoErrorCode.OUTPUT_FAILED)
        ciphertext_sha256, ciphertext_size = output_errors[0]
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        return CiphertextReceipt(destination, ciphertext_sha256, ciphertext_size, event.document["event_id"], event.kind)
    except _Cancelled:
        raise CryptoError(CryptoErrorCode.CANCELLED)
    except CryptoError:
        raise
    except (OSError, ValueError):
        raise CryptoError(CryptoErrorCode.OUTPUT_FAILED) from None
    finally:
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)
        temporary.unlink(missing_ok=True)


def _prepared_metadata(
    document: Mapping[str, Any], profile: CaptureProfile, recipients: ResolvedRecipients
) -> dict[str, object]:
    kind = document.get("kind")
    content_type = {
        "session-segment": "application/vnd.josh.codex-session-segment+json",
        "session-asset": "application/vnd.josh.codex-session-asset",
        "session-final": "application/vnd.josh.codex-session-final+json",
        "index-event": "application/vnd.josh.codex-index-event+json",
    }.get(kind)
    capture = document.get("capture")
    if not isinstance(kind, str) or content_type not in CONTENT_TYPES:
        _fail(CryptoErrorCode.DOCUMENT_INVALID)
    if not isinstance(capture, Mapping):
        if kind != "index-event":
            _fail(CryptoErrorCode.DOCUMENT_INVALID)
        capture = {"status": "complete", "policy_decision": "allow"}
    # Deliberately omit content_sha256/content_size.  Those values belong in
    # the encrypted manifest and are not public delivery metadata.
    result = {
        "object_kind": kind,
        "content_type": content_type,
        "destination_class": profile.destination.kind,
        "recipient_set_fingerprint": recipients.fingerprint,
        "capture_status": capture.get("status"),
        "policy_decision": capture.get("policy_decision"),
    }
    if profile.destination.binding_id is not None:
        result["destination_binding_id"] = profile.destination.binding_id
    return result


def encrypt_and_prepare(
    event: NormalizationEvent,
    outbox: PccOutbox,
    owner: str,
    profile: CaptureProfile,
    recipient_resolver: RecipientResolver,
    *,
    payload: Sequence[bytes] | Any | None = None,
    asset_payload: Sequence[bytes] | Any | None = None,
    age_executable: str | os.PathLike[str] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> PreparedReceipt:
    """Encrypt one #9 event and atomically hand ciphertext to #8.

    The trigger record must already be source-snapshotted.  Enqueue metadata
    never supplies recipients or payload bytes; both are resolved by the
    caller-owned host policy and the normalized event boundary respectively.
    """

    if not isinstance(event, NormalizationEvent) or not isinstance(outbox, PccOutbox):
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    if payload is not None and asset_payload is not None:
        _fail(CryptoErrorCode.ASSET_PAYLOAD_MISSING)
    if asset_payload is not None:
        payload = asset_payload
    event_id = event.document.get("event_id") if isinstance(event.document, Mapping) else None
    if not isinstance(event_id, str):
        _fail(CryptoErrorCode.DOCUMENT_INVALID)
    document = _document_or_fail(dict(event.document))
    if event.kind != document.get("kind"):
        _fail(CryptoErrorCode.MANIFEST_MISMATCH)
    _profile_binding(document, profile)
    try:
        queued = outbox.inspect_record(event_id)
    except OutboxError:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    if queued is None or queued.owner != owner:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
    _validate_queue_binding(event, document, queued, profile)
    if queued.resume_state in {
        QueueState.PREPARED_ENCRYPTED,
        QueueState.OBJECT_UPLOADED,
        QueueState.INDEX_PUBLISHED,
    } or queued.state is QueueState.COMMITTED:
        if queued.ciphertext_sha256 is None or queued.ciphertext_size is None:
            _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
        if "object_kind" in queued.metadata and queued.metadata["object_kind"] != document["kind"]:
            _fail(CryptoErrorCode.OUTBOX_PRECONDITION)
        return PreparedReceipt(event_id, document["kind"], queued.ciphertext_sha256, queued.ciphertext_size)
    if queued.resume_state is not QueueState.SOURCE_SNAPSHOTTED:
        _fail(CryptoErrorCode.OUTBOX_PRECONDITION)

    recipients = resolve_recipients(profile, recipient_resolver)
    if event.kind == "session-asset" and payload is None:
        _fail(CryptoErrorCode.ASSET_PAYLOAD_MISSING)

    outbox.root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{event_id}.age.pending.", dir=outbox.root)
    os.close(descriptor)
    temporary = Path(temporary_name)
    keep_temporary = False
    try:
        receipt = stream_encrypt(
            event,
            recipients,
            temporary,
            profile=profile,
            payload=payload,
            age_executable=age_executable,
            cancel_check=cancel_check,
        )
        try:
            prepared = outbox.prepare_encrypted_file(
                event_id,
                owner,
                receipt.path,
                metadata=_prepared_metadata(document, profile, recipients),
            )
        except OutboxError as error:
            raise CryptoError(CryptoErrorCode.PUBLICATION_FAILED) from error
        if prepared.state is QueueState.CAPTURE_GAP:
            try:
                outbox.prepared.preserve_capture_gap_ciphertext(event_id, receipt.path)
            except OutboxError:
                # Retain the encrypted temporary if the quarantine move also
                # fails.  It is still ciphertext-only and therefore safer to
                # leave for reconciliation than to delete silently.
                keep_temporary = True
            raise CryptoError(CryptoErrorCode.CAPTURE_GAP)
        if prepared.state is not QueueState.PREPARED_ENCRYPTED:
            _fail(CryptoErrorCode.PUBLICATION_FAILED)
        return PreparedReceipt(event_id, document["kind"], receipt.sha256, receipt.size)
    finally:
        if not keep_temporary:
            temporary.unlink(missing_ok=True)


def _profile_from_manifest_context(document: Mapping[str, Any], recipients: ResolvedRecipients) -> CaptureProfile:
    """Build only the profile fields needed by the pure manifest builder.

    Production callers use ``encrypt_and_prepare`` with the host profile.  The
    streaming primitive retains a narrow compatibility seam for direct tests;
    the recipient reference and document bindings remain authoritative.
    """

    from .policy import CaptureProfile, Destination, Limits

    return CaptureProfile(
        name="pcc",
        profile_id=document.get("profile_id", "profile-unknown"),
        workspace_id=document.get("workspace_id", "workspace-unknown"),
        allowed_sources=frozenset(),
        capture_mode="transcript-and-assets",
        triggers=frozenset({"manual"}),
        destination=Destination("local-only"),
        recipient_set_ref=recipients.reference,
        limits=Limits(1, 1, 1, 1, 1),
        allow_rules=(),
        deny_rules=(),
        downstream_memory=False,
        policy_version="1.0",
        provenance="private-host-config",
    )


def _identity_args(identity_paths: Sequence[str | os.PathLike[str]]) -> list[str]:
    args: list[str] = []
    for raw_path in identity_paths:
        path = Path(raw_path)
        try:
            mode = path.stat().st_mode
            if not path.is_file() or path.is_symlink() or mode & 0o077:
                _fail(CryptoErrorCode.AGE_FAILED)
        except OSError:
            _fail(CryptoErrorCode.AGE_FAILED)
        args.extend(("-i", str(path)))
    if not args:
        _fail(CryptoErrorCode.AGE_FAILED)
    return args


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo, maximum: int) -> bytes:
    if not member.isreg() or member.size < 0 or member.size > maximum:
        _fail(CryptoErrorCode.ENVELOPE_INVALID)
    fileobj = archive.extractfile(member)
    if fileobj is None:
        _fail(CryptoErrorCode.ENVELOPE_INVALID)
    chunks: list[bytes] = []
    remaining = member.size
    while remaining:
        chunk = fileobj.read(min(_IO_CHUNK, remaining))
        if not chunk:
            _fail(CryptoErrorCode.ENVELOPE_INVALID)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_decrypted_envelope(plaintext: bytes | bytearray | str | os.PathLike[str] | Any) -> DecryptedEnvelope:
    """Read and validate a decrypted two-member envelope."""

    close_source = False
    if isinstance(plaintext, (str, os.PathLike)):
        source = Path(plaintext).open("rb")  # noqa: SIM115 - source lifetime is closed in the shared finally block
        close_source = True
    elif isinstance(plaintext, (bytes, bytearray)):
        source = io.BytesIO(plaintext)
        close_source = True
    else:
        source = plaintext
    try:
        try:
            with tarfile.open(fileobj=source, mode="r:") as archive:
                members = []
                for _ in range(3):
                    member = archive.next()
                    if member is None:
                        break
                    members.append(member)
                if len(members) != 2 or members[0].name != "manifest.json":
                    _fail(CryptoErrorCode.ENVELOPE_INVALID)
                if members[1].name not in {"payload.json", "payload.bin"}:
                    _fail(CryptoErrorCode.ENVELOPE_INVALID)
                manifest_bytes = _read_member(archive, members[0], MAX_MANIFEST_BYTES)
                try:
                    manifest = json.loads(manifest_bytes)
                except (TypeError, ValueError, json.JSONDecodeError):
                    _fail(CryptoErrorCode.MANIFEST_INVALID)
                if not isinstance(manifest, dict):
                    _fail(CryptoErrorCode.MANIFEST_INVALID)
                payload = _read_member(archive, members[1], MAX_PAYLOAD_BYTES)
        except CryptoError:
            raise
        except (OSError, tarfile.TarError, EOFError):
            raise CryptoError(CryptoErrorCode.ENVELOPE_INVALID) from None

        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        kind = manifest.get("kind")
        if kind == "session-asset":
            document = manifest.get("asset_document")
            if not isinstance(document, dict) or members[1].name != "payload.bin":
                _fail(CryptoErrorCode.MANIFEST_MISMATCH)
        else:
            if members[1].name != "payload.json":
                _fail(CryptoErrorCode.MANIFEST_MISMATCH)
            try:
                document = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                _fail(CryptoErrorCode.DOCUMENT_INVALID)
            if not isinstance(document, dict) or canonical_json(document) != payload:
                _fail(CryptoErrorCode.MANIFEST_MISMATCH)
        validate_manifest(manifest, document, digest, size)
        if kind == "session-asset" and (document.get("sha256") != digest or document.get("size") != size):
            _fail(CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH)
        return DecryptedEnvelope(manifest, document, payload, digest, size)
    finally:
        if close_source:
            source.close()


def decrypt_envelope(
    ciphertext: str | os.PathLike[str],
    identity_paths: Sequence[str | os.PathLike[str]],
    output: str | os.PathLike[str] | None = None,
    *,
    age_executable: str | os.PathLike[str] | None = None,
) -> DecryptedEnvelope:
    """Decrypt through age into a restrictive ephemeral file and validate it."""

    source_path = Path(ciphertext)
    if not source_path.is_file() or source_path.is_symlink():
        _fail(CryptoErrorCode.AGE_FAILED)
    identities = _identity_args(identity_paths)
    try:
        executable = Path(age_executable) if age_executable is not None else _managed_executable("age")
    except (OSError, ValueError, RuntimeError):
        _fail(CryptoErrorCode.AGE_UNAVAILABLE)
    directory = source_path.parent
    descriptor, temporary_name = tempfile.mkstemp(prefix=".age-plain.", dir=directory)
    os.close(descriptor)
    temporary = Path(temporary_name)
    process = None
    errors: list[BaseException | tuple[str, int]] = []
    try:
        with source_path.open("rb") as source:
            try:
                process = subprocess.Popen(
                    [str(executable), "--decrypt", *identities],
                    stdin=source,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    close_fds=True,
                )
            except (OSError, ValueError):
                _fail(CryptoErrorCode.AGE_UNAVAILABLE)
            stdout_thread = __import__("threading").Thread(target=_drain_stdout, args=(process.stdout, temporary, errors), daemon=True)
            stderr_thread = __import__("threading").Thread(target=_drain_stderr, args=(process.stderr,), daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            return_code = process.wait()
            stdout_thread.join()
            stderr_thread.join()
        if errors and isinstance(errors[0], BaseException):
            _fail(CryptoErrorCode.OUTPUT_FAILED)
        if return_code != 0:
            _fail(CryptoErrorCode.AGE_FAILED)
        result = read_decrypted_envelope(temporary)
        if output is not None:
            del output
        return result
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        temporary.unlink(missing_ok=True)


__all__ = [
    "CiphertextReceipt",
    "CryptoError",
    "CryptoErrorCode",
    "DecryptedEnvelope",
    "RecipientSet",
    "ResolvedRecipients",
    "build_manifest",
    "decrypt_envelope",
    "encrypt_and_prepare",
    "read_decrypted_envelope",
    "resolve_recipients",
    "stream_encrypt",
    "validate_manifest",
]
