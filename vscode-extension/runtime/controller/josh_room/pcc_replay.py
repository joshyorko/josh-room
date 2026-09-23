"""Public, bounded PCC evidence replay/export contract.

This module is deliberately a consumer seam.  It discovers encrypted index
objects through the #11 backend, verifies every ciphertext identity, decrypts
only with caller-approved identities, validates Session Evidence envelopes,
and emits inert JSONL records.  It never performs memory extraction or treats
transcript text as instructions.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pcc_crypto import DecryptedEnvelope, decrypt_envelope
from .r2 import evidence_object_key, validate_evidence_index_key
from .session_evidence import (
    CURRENT_MAJOR,
    CURRENT_MINOR,
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    validate_document,
)

SCHEMA = "josh-room.pcc-replay"
SCHEMA_VERSION = {"major": 1, "minor": 0}
_CURSOR_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_CURSOR_BYTES = 4096
_MAX_JSONL_RECORD_BYTES = 4 * 1024 * 1024
_MAX_REPLAY_PAGE_SIZE = 8
_MAX_REPLAY_INDEXES = 10_000
_MAX_REPLAY_CIPHERTEXT_BYTES = 80 * 1024 * 1024
_MAX_REPLAY_SEGMENT_BYTES = 4 * 1024 * 1024
_MAX_REPLAY_RECORDS_PER_SEGMENT = 128
_MAX_REPLAY_ASSETS_PER_SEGMENT = 32


class ReplayError(RuntimeError):
    """Stable public-safe replay failure; untrusted values are never echoed."""

    def __init__(self, code: str):
        self.code = str(code)
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ReplayLimits:
    page_size: int = _MAX_REPLAY_PAGE_SIZE
    max_indexes: int = 1000
    max_ciphertext_bytes: int = _MAX_REPLAY_CIPHERTEXT_BYTES

    def __post_init__(self) -> None:
        if type(self.page_size) is not int or not 0 < self.page_size <= _MAX_REPLAY_PAGE_SIZE:
            raise ValueError("replay page size is invalid")
        if type(self.max_indexes) is not int or not 0 < self.max_indexes <= _MAX_REPLAY_INDEXES:
            raise ValueError("replay index bound is invalid")
        if (
            type(self.max_ciphertext_bytes) is not int
            or not 0 < self.max_ciphertext_bytes <= _MAX_REPLAY_CIPHERTEXT_BYTES
        ):
            raise ValueError("replay ciphertext bound is invalid")


@dataclass(frozen=True, slots=True)
class ReplayCursor:
    profile_id: str
    destination: str
    last_index_key: str | None = None
    version: int = _CURSOR_VERSION

    def encode(self) -> str:
        body = {
            "version": self.version,
            "profile_id": self.profile_id,
            "destination": self.destination,
            "last_index_key": self.last_index_key,
        }
        encoded = base64.urlsafe_b64encode(canonical_json(body)).rstrip(b"=").decode("ascii")
        if len(encoded) > _MAX_CURSOR_BYTES:
            raise ReplayError("cursor-too-large")
        return encoded

    @classmethod
    def decode(cls, value: str, *, profile_id: str, destination: str) -> ReplayCursor:
        if not isinstance(value, str) or not value or len(value) > _MAX_CURSOR_BYTES:
            raise ReplayError("cursor-invalid")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            body = json.loads(raw)
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            raise ReplayError("cursor-invalid") from None
        if (
            not isinstance(body, dict)
            or body.get("version") != _CURSOR_VERSION
            or body.get("profile_id") != profile_id
            or body.get("destination") != destination
        ):
            raise ReplayError("cursor-invalid")
        last = body.get("last_index_key")
        if last is not None and (not isinstance(last, str) or not last.startswith("evidence/index/v1/")):
            raise ReplayError("cursor-invalid")
        return cls(profile_id, destination, last)


@dataclass(frozen=True, slots=True)
class QuarantineReceipt:
    reason_code: str
    index_key: str | None = None
    event_id: str | None = None
    ciphertext_sha256: str | None = None
    ciphertext_size: int | None = None
    cursor: str | None = None

    def to_dict(self, *, profile_id: str, destination: str) -> dict[str, Any]:
        identity = {
            "profile_id": profile_id,
            "destination": destination,
            "reason_code": self.reason_code,
            "index_key": self.index_key,
            "event_id": self.event_id,
            "ciphertext_sha256": self.ciphertext_sha256,
            "ciphertext_size": self.ciphertext_size,
        }
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "schema_version": dict(SCHEMA_VERSION),
            "type": "quarantine",
            "quarantine_id": hashlib.sha256(canonical_json(identity)).hexdigest(),
            "profile_id": profile_id,
            "destination": destination,
            "reason_code": self.reason_code,
        }
        for key, value in (
            ("index_key", self.index_key),
            ("event_id", self.event_id),
            ("ciphertext_sha256", self.ciphertext_sha256),
            ("ciphertext_size", self.ciphertext_size),
            ("cursor", self.cursor),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class ReplayPage:
    records: tuple[dict[str, Any], ...]
    quarantines: tuple[dict[str, Any], ...]
    cursor: str | None
    complete: bool
    inspected_indexes: int

    def jsonl(self) -> Iterator[str]:
        for item in (*self.records, *self.quarantines):
            encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > _MAX_JSONL_RECORD_BYTES:
                raise ReplayError("normalized-record-too-large")
            yield encoded
        summary = {
            "schema": SCHEMA,
            "schema_version": dict(SCHEMA_VERSION),
            "type": "summary",
            "next_cursor": self.cursor,
            "complete": self.complete,
            "inspected_indexes": self.inspected_indexes,
            "records": len(self.records),
            "quarantined": len(self.quarantines),
        }
        yield json.dumps(summary, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class _IndexRef:
    key: str
    ciphertext_sha256: str
    ciphertext_size: int


@dataclass(frozen=True, slots=True)
class _Evidence:
    index: _IndexRef
    index_document: dict[str, Any]
    document: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ChainEvidence:
    index: _IndexRef
    document: dict[str, Any]
    digest: str | None = None
    evidence: _Evidence | None = None


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ReplayError(code)
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ReplayError("digest-mismatch")
    return value


def _safe_ref(value: object) -> _IndexRef:
    if isinstance(value, Mapping):
        key, digest, size = value.get("key"), value.get("ciphertext_sha256"), value.get("ciphertext_size")
    else:
        key = getattr(value, "key", None)
        digest = getattr(value, "ciphertext_sha256", None)
        size = getattr(value, "ciphertext_size", None)
    if not isinstance(key, str):
        raise ReplayError("index-reference-invalid")
    try:
        key_digest = validate_evidence_index_key(key)
    except (TypeError, ValueError):
        raise ReplayError("index-reference-invalid") from None
    if digest != key_digest or type(size) is not int or size < 0:
        raise ReplayError("index-reference-invalid")
    return _IndexRef(key, key_digest, size)


def _payload_from_envelope(value: object) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    if isinstance(value, DecryptedEnvelope):
        return value.manifest, value.document, value.payload
    if isinstance(value, Mapping):
        manifest = value.get("manifest")
        document = value.get("document")
        payload = value.get("payload")
        if isinstance(manifest, Mapping) and isinstance(document, Mapping) and isinstance(payload, (bytes, bytearray)):
            return dict(manifest), dict(document), bytes(payload)
    raise ReplayError("corrupt-ciphertext")


def _error_code(result: object, fallback: str) -> str:
    errors = getattr(result, "errors", ())
    for error in errors:
        code = getattr(error, "code", None)
        if isinstance(code, str) and code:
            return code
    return fallback


class ReplayReader:
    """Bounded, repeatable reader over an explicit profile and destination.

    ``backend`` is the existing #11 R2-like object with index discovery and
    evidence reads.  ``decryptor`` is a test/Action-Server seam; production
    callers omit it and the approved local age identity is used.
    """

    def __init__(
        self,
        backend: object,
        *,
        profile_id: str,
        destination: str,
        workspace_id: str | None = None,
        identity_paths: Sequence[str | os.PathLike[str]] = (),
        decryptor: Callable[..., object] | None = None,
        age_executable: str | os.PathLike[str] | None = None,
        authorize: Callable[[str, str], bool] | None = None,
        limits: ReplayLimits | None = None,
    ) -> None:
        _identifier(profile_id, "profile-invalid")
        if not isinstance(destination, str) or not destination or len(destination) > 128 or any(ord(char) < 0x21 for char in destination):
            raise ReplayError("destination-invalid")
        if authorize is not None:
            try:
                allowed = authorize(profile_id, destination)
            except Exception:
                allowed = False
            if allowed is not True:
                raise ReplayError("profile-boundary-denied")
        if not hasattr(backend, "get_evidence_index_bytes") or not hasattr(backend, "get_evidence_bytes"):
            raise TypeError("replay backend is incomplete")
        self.backend = backend
        self.profile_id = profile_id
        self.destination = destination
        self.workspace_id = workspace_id
        self.identity_paths = tuple(Path(path) for path in identity_paths)
        self.decryptor = decryptor
        self.age_executable = age_executable
        self.limits = limits or ReplayLimits()

    def _discover(self) -> tuple[list[_IndexRef], bool]:
        discover = getattr(self.backend, "discover_evidence_indexes", None) or getattr(self.backend, "discover_index_events", None)
        if discover is None:
            raise ReplayError("index-discovery-unavailable")
        max_events = self.limits.max_indexes + 1
        try:
            refs = discover(max_events=max_events, page_size=self.limits.page_size)
        except TypeError:
            refs = discover(max_events, self.limits.page_size)
        if not isinstance(refs, Iterable):
            raise ReplayError("index-discovery-invalid")
        result: dict[str, _IndexRef] = {}
        raw_count = 0
        truncated = False
        for raw in refs:
            raw_count += 1
            try:
                ref = _safe_ref(raw)
            except ReplayError:
                continue
            result.setdefault(ref.key, ref)
            if len(result) > self.limits.max_indexes:
                truncated = True
                break
        if raw_count >= max_events:
            truncated = True
        selected = sorted(result.values(), key=lambda item: item.key)[: self.limits.max_indexes]
        return selected, truncated

    def inspect(self, *, cursor: str | None = None, limit: int | None = None) -> dict[str, Any]:
        bound = self.limits.page_size if limit is None else limit
        if type(bound) is not int or not 0 < bound <= self.limits.page_size:
            raise ReplayError("limit-invalid")
        decoded = ReplayCursor.decode(cursor, profile_id=self.profile_id, destination=self.destination) if cursor else ReplayCursor(self.profile_id, self.destination)
        all_refs, truncated = self._discover()
        refs = [item for item in all_refs if decoded.last_index_key is None or item.key > decoded.last_index_key]
        selected = refs[:bound]
        next_cursor = decoded.last_index_key
        if selected:
            next_cursor = selected[-1].key
        return {
            "schema": SCHEMA,
            "schema_version": dict(SCHEMA_VERSION),
            "ok": True,
            "metadata_only": True,
            "profile_id": self.profile_id,
            "workspace_id": self.workspace_id,
            "destination": self.destination,
            "indexes": [
                {"key": item.key, "ciphertext_sha256": item.ciphertext_sha256, "ciphertext_size": item.ciphertext_size}
                for item in selected
            ],
            "next_cursor": ReplayCursor(self.profile_id, self.destination, next_cursor).encode() if next_cursor else None,
            "complete": not truncated and len(refs) <= bound,
        }

    def _read_index(self, ref: _IndexRef) -> bytes:
        if ref.ciphertext_size > self.limits.max_ciphertext_bytes:
            raise ReplayError("ciphertext-too-large")
        try:
            body = self.backend.get_evidence_index_bytes(ref.key)
        except Exception as error:
            raise ReplayError("index-read-failed") from error
        if not isinstance(body, bytes) or len(body) > self.limits.max_ciphertext_bytes:
            raise ReplayError("ciphertext-too-large")
        if len(body) != ref.ciphertext_size or hashlib.sha256(body).hexdigest() != ref.ciphertext_sha256:
            raise ReplayError("digest-mismatch")
        return body

    def _read_object(self, digest: str, size: int) -> bytes:
        _digest(digest)
        if type(size) is not int or size < 0 or size > self.limits.max_ciphertext_bytes:
            raise ReplayError("ciphertext-size-invalid")
        key = evidence_object_key(digest)
        try:
            try:
                body = self.backend.get_evidence_bytes(key, expected_size=size)
            except TypeError:
                body = self.backend.get_evidence_bytes(key)
        except Exception as error:
            raise ReplayError("evidence-read-failed") from error
        if not isinstance(body, bytes) or len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise ReplayError("digest-mismatch")
        return body

    def _decrypt(self, body: bytes, *, expected_kind: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
        if self.decryptor is not None:
            try:
                value = self.decryptor(body, expected_kind=expected_kind, identity_paths=self.identity_paths)
            except TypeError:
                try:
                    value = self.decryptor(body, expected_kind)
                except TypeError:
                    value = self.decryptor(body)
            return _payload_from_envelope(value)
        if not self.identity_paths:
            raise ReplayError("identity-unavailable")
        with tempfile.NamedTemporaryFile(prefix=".pcc-replay-", suffix=".age", mode="wb", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(body)
        try:
            try:
                value = decrypt_envelope(temporary, self.identity_paths, age_executable=self.age_executable)
            except Exception as error:
                raise ReplayError("decrypt-failed") from error
            return _payload_from_envelope(value)
        finally:
            temporary.unlink(missing_ok=True)


    def _validate_index(self, ref: _IndexRef, manifest: dict[str, Any], document: dict[str, Any], payload: bytes) -> _Evidence:
        result = validate_document(document)
        if result.disposition is ValidationDisposition.QUARANTINED:
            raise ReplayError(_error_code(result, "unknown-major"))
        if result.disposition is not ValidationDisposition.ACCEPTED:
            raise ReplayError(_error_code(result, "index-invalid"))
        if document.get("kind") != "index-event":
            raise ReplayError("index-kind-mismatch")
        profile = manifest.get("profile")
        if not isinstance(profile, Mapping) or profile.get("id") != self.profile_id:
            raise ReplayError("profile-boundary-denied")
        manifest_workspace = profile.get("workspace_id")
        if self.workspace_id is not None and manifest_workspace != self.workspace_id:
            raise ReplayError("profile-boundary-denied")
        policy = manifest.get("policy")
        if not isinstance(policy, Mapping) or policy.get("decision") not in {"allow", "local-only"}:
            raise ReplayError("policy-mismatch")
        if policy.get("destination") is not None and policy.get("destination") != self.destination:
            raise ReplayError("policy-mismatch")
        expected_kind = document.get("evidence_kind")
        expected_event = document.get("evidence_event_id")
        if not isinstance(expected_kind, str) or not isinstance(expected_event, str):
            raise ReplayError("index-invalid")
        if document.get("content_type") not in {
            "application/vnd.josh.codex-session-segment+json",
            "application/vnd.josh.codex-session-asset",
            "application/vnd.josh.codex-session-final+json",
        }:
            raise ReplayError("index-invalid")
        object_body = self._read_object(str(document.get("ciphertext_sha256")), int(document.get("ciphertext_size")))
        evidence_manifest, evidence_document, evidence_payload = self._decrypt(object_body, expected_kind=expected_kind)
        validated = validate_document(evidence_document)
        if validated.disposition is ValidationDisposition.QUARANTINED:
            raise ReplayError(_error_code(validated, "unknown-major"))
        if validated.disposition is not ValidationDisposition.ACCEPTED:
            raise ReplayError(_error_code(validated, "evidence-invalid"))
        if evidence_document.get("kind") != expected_kind or evidence_document.get("event_id") != expected_event:
            raise ReplayError("manifest-mismatch")
        evidence_profile = evidence_manifest.get("profile")
        if not isinstance(evidence_profile, Mapping) or evidence_profile.get("id") != self.profile_id:
            raise ReplayError("profile-boundary-denied")
        if self.workspace_id is not None and evidence_profile.get("workspace_id") != self.workspace_id:
            raise ReplayError("profile-boundary-denied")
        evidence_capture = evidence_document.get("capture")
        if not isinstance(evidence_capture, Mapping):
            raise ReplayError("policy-mismatch")
        if evidence_capture.get("status") == "quarantined":
            raise ReplayError("capture-quarantined")
        evidence_policy = evidence_manifest.get("policy")
        evidence_decision = evidence_capture.get("policy_decision")
        if (
            not isinstance(evidence_policy, Mapping)
            or evidence_decision not in {"allow", "local-only"}
            or evidence_policy.get("decision") != evidence_decision
            or evidence_policy.get("status") != evidence_capture.get("status")
            or evidence_policy.get("sensitivity") != evidence_capture.get("sensitivity")
        ):
            raise ReplayError("policy-mismatch")
        if evidence_policy.get("destination") is not None and evidence_policy.get("destination") != self.destination:
            raise ReplayError("policy-mismatch")
        if evidence_document.get("kind") == "session-segment":
            records_payload = canonical_json(evidence_document.get("records", []))
            if evidence_document.get("content_sha256") != hashlib.sha256(records_payload).hexdigest():
                raise ReplayError("digest-mismatch")
            if evidence_document.get("content_size") != len(records_payload):
                raise ReplayError("digest-mismatch")
            if (
                len(records_payload) > _MAX_REPLAY_SEGMENT_BYTES
                or evidence_document.get("record_count", 0) > _MAX_REPLAY_RECORDS_PER_SEGMENT
                or len(evidence_document.get("asset_refs", ())) > _MAX_REPLAY_ASSETS_PER_SEGMENT
            ):
                raise ReplayError("segment-too-large")
        elif evidence_document.get("kind") == "session-asset":
            if evidence_document.get("sha256") != hashlib.sha256(evidence_payload).hexdigest() or evidence_document.get("size") != len(evidence_payload):
                raise ReplayError("digest-mismatch")
        index_content = document.get("content_sha256")
        if expected_kind != "session-asset" and index_content != evidence_document.get("content_sha256"):
            raise ReplayError("digest-mismatch")
        if expected_kind == "session-asset" and index_content not in {canonical_digest(evidence_document), evidence_document.get("sha256")}:
            raise ReplayError("digest-mismatch")
        return _Evidence(ref, document, evidence_document)

    def _quarantine(self, ref: _IndexRef, reason: str, *, event_id: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        return QuarantineReceipt(reason, ref.key, event_id, ref.ciphertext_sha256, ref.ciphertext_size, cursor).to_dict(profile_id=self.profile_id, destination=self.destination)

    def _normalized(self, item: _Evidence, segment: Mapping[str, Any], record: Mapping[str, Any], index: int) -> dict[str, Any]:
        session_id = segment.get("session_id")
        checkpoint = segment.get("checkpoint")
        identity = {
            "profile_id": self.profile_id,
            "workspace_id": segment.get("workspace_id"),
            "session_id": session_id,
            "segment_event_id": segment.get("event_id"),
            "segment_content_sha256": segment.get("content_sha256"),
            "checkpoint": checkpoint,
            "record_index": index,
            "record": record,
        }
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "schema_version": dict(SCHEMA_VERSION),
            "type": "record",
            "idempotency_key": hashlib.sha256(canonical_json(identity)).hexdigest(),
            "profile_id": self.profile_id,
            "workspace_id": segment.get("workspace_id"),
            "session": {
                key: segment[key]
                for key in ("session_id", "thread_id", "turn_id")
                if key in segment
            },
            "source": copy.deepcopy(segment.get("source", {})),
            "checkpoint": copy.deepcopy(checkpoint),
            "segment": {
                "event_id": segment.get("event_id"),
                "content_sha256": segment.get("content_sha256"),
                "content_size": segment.get("content_size"),
                "ciphertext_sha256": item.index_document.get("ciphertext_sha256"),
                "ciphertext_size": item.index_document.get("ciphertext_size"),
            },
            "record_index": index,
            "record": copy.deepcopy(dict(record)),
            "producer_trust": "untrusted",
        }
        if isinstance(segment.get("asset_refs"), list):
            result["asset_refs"] = copy.deepcopy(segment["asset_refs"])
        encoded = canonical_json(result)
        if len(encoded) > _MAX_JSONL_RECORD_BYTES:
            raise ReplayError("normalized-record-too-large")
        return result

    def _chain_evidence(self, item: _Evidence, *, retain_segment: bool) -> _ChainEvidence:
        document = item.document
        kind = document.get("kind")
        if kind == "session-segment":
            fields = ("kind", "event_id", "session_id", "checkpoint", "previous_segment_sha256", "asset_refs")
            digest = canonical_digest(document)
            evidence = item if retain_segment else None
        elif kind == "session-asset":
            fields = ("kind", "event_id", "session_id", "asset_id", "sha256", "size")
            digest = None
            evidence = None
        else:
            fields = ("kind", "event_id", "session_id", "last_segment_sha256")
            digest = None
            evidence = None
        metadata = {key: document[key] for key in fields if key in document}
        return _ChainEvidence(item.index, metadata, digest, evidence)
    def _verify_chain(self, items: list[_ChainEvidence], quarantines: list[dict[str, Any]]) -> list[_ChainEvidence]:
        valid = [item for item in items if item.document.get("kind") in {"session-segment", "session-asset", "session-final"}]
        segments = [item for item in valid if item.document.get("kind") == "session-segment"]
        assets = [item for item in valid if item.document.get("kind") == "session-asset"]
        finals = [item for item in valid if item.document.get("kind") == "session-final"]
        asset_keys = {
            (item.document.get("session_id"), item.document.get("asset_id"), item.document.get("sha256"), item.document.get("size"))
            for item in assets
        }
        by_session: dict[object, list[_ChainEvidence]] = {}
        finals_by_session: dict[object, list[_ChainEvidence]] = {}
        for item in segments:
            by_session.setdefault(item.document.get("session_id"), []).append(item)
        for item in finals:
            finals_by_session.setdefault(item.document.get("session_id"), []).append(item)
        accepted: list[_ChainEvidence] = []
        reported: set[tuple[str, str]] = set()

        def quarantine(item: _ChainEvidence, reason: str) -> None:
            key = (item.index.key, reason)
            if key not in reported:
                quarantines.append(self._quarantine(item.index, reason, event_id=str(item.document.get("event_id"))))
                reported.add(key)

        for session_id in by_session.keys() | finals_by_session.keys():
            group = sorted(
                by_session.get(session_id, ()),
                key=lambda item: (
                    (item.document.get("checkpoint") or {}).get("start", 0),
                    (item.document.get("checkpoint") or {}).get("end", 0),
                    str(item.document.get("event_id", "")),
                ),
            )
            previous: str | None = None
            failed = False
            for item in group:
                document = item.document
                declared = document.get("previous_segment_sha256")
                chain_bad = declared != previous and not (declared is None and previous is None)
                refs = document.get("asset_refs", ())
                missing_asset = not isinstance(refs, list) or any(
                    (session_id, ref.get("asset_id"), ref.get("sha256"), ref.get("size")) not in asset_keys
                    for ref in refs
                    if isinstance(ref, Mapping)
                )
                if chain_bad:
                    quarantine(item, "broken-chain")
                    failed = True
                if missing_asset:
                    quarantine(item, "missing-asset")
                    failed = True
                if failed and not chain_bad and not missing_asset:
                    quarantine(item, "broken-chain")
                previous = item.digest
            session_finals = finals_by_session.get(session_id, ())
            for final in session_finals:
                declared = final.document.get("last_segment_sha256")
                if declared is not None and declared != previous:
                    quarantine(final, "broken-chain")
                    failed = True
                elif failed:
                    quarantine(final, "broken-chain")
            if failed:
                for item in group:
                    quarantine(item, "broken-chain")
            else:
                accepted.extend(group)
                accepted.extend(session_finals)
        return accepted

    def export(self, *, cursor: str | None = None, limit: int | None = None) -> ReplayPage:
        bound = self.limits.page_size if limit is None else limit
        if type(bound) is not int or not 0 < bound <= self.limits.page_size:
            raise ReplayError("limit-invalid")
        decoded = ReplayCursor.decode(cursor, profile_id=self.profile_id, destination=self.destination) if cursor else ReplayCursor(self.profile_id, self.destination)
        refs, truncated = self._discover()
        if truncated:
            resume_cursor = ReplayCursor(self.profile_id, self.destination, decoded.last_index_key).encode() if decoded.last_index_key else None
            return ReplayPage((), (), resume_cursor, False, 0)
        available = [item for item in refs if decoded.last_index_key is None or item.key > decoded.last_index_key]
        selected = available[:bound]
        last_key = selected[-1].key if selected else decoded.last_index_key
        next_cursor = ReplayCursor(self.profile_id, self.destination, last_key).encode() if last_key else None
        if not selected:
            return ReplayPage((), (), next_cursor, not truncated, 0)
        selected_keys = {item.key for item in selected}
        quarantines: list[dict[str, Any]] = []
        evidence: list[_ChainEvidence] = []
        for ref in refs:
            try:
                index_body = self._read_index(ref)
                manifest, document, payload = self._decrypt(index_body, expected_kind="index-event")
                item = self._validate_index(ref, manifest, document, payload)
                retain = ref.key in selected_keys and item.document.get("kind") == "session-segment"
                evidence.append(self._chain_evidence(item, retain_segment=retain))
            except ReplayError as error:
                if ref.key in selected_keys:
                    quarantines.append(self._quarantine(
                        ref,
                        error.code,
                        cursor=ReplayCursor(self.profile_id, self.destination, ref.key).encode(),
                    ))
            except Exception:
                if ref.key in selected_keys:
                    quarantines.append(self._quarantine(
                        ref,
                        "corrupt-ciphertext",
                        cursor=ReplayCursor(self.profile_id, self.destination, ref.key).encode(),
                    ))
        accepted = self._verify_chain(evidence, quarantines)
        quarantines = [item for item in quarantines if item.get("index_key") in selected_keys]
        records: list[dict[str, Any]] = []
        for chain_item in sorted(
            accepted,
            key=lambda value: (
                str(value.document.get("session_id", "")),
                (value.document.get("checkpoint") or {}).get("start", 0),
                str(value.document.get("event_id", "")),
            ),
        ):
            item = chain_item.evidence
            if item is None or item.document.get("kind") != "session-segment":
                continue
            document = item.document
            raw_records = document.get("records")
            if not isinstance(raw_records, list):
                quarantines.append(self._quarantine(item.index, "evidence-invalid", event_id=str(document.get("event_id"))))
                continue
            for index, record in enumerate(raw_records):
                if isinstance(record, Mapping):
                    records.append(self._normalized(item, document, record, index))
        return ReplayPage(tuple(records), tuple(quarantines), next_cursor, not truncated and len(available) <= bound, len(selected))

    def iter_jsonl(self, *, cursor: str | None = None, limit: int | None = None) -> Iterator[str]:
        yield from self.export(cursor=cursor, limit=limit).jsonl()


ReplayExporter = ReplayReader


def export_jsonl(reader: ReplayReader, *, cursor: str | None = None, limit: int | None = None) -> Iterator[str]:
    """Neutral JSONL convenience function for Action Server/MemoryD wrappers."""
    yield from reader.iter_jsonl(cursor=cursor, limit=limit)


__all__ = [
    "CURRENT_MAJOR",
    "CURRENT_MINOR",
    "SCHEMA",
    "SCHEMA_VERSION",
    "QuarantineReceipt",
    "ReplayCursor",
    "ReplayError",
    "ReplayExporter",
    "ReplayLimits",
    "ReplayPage",
    "ReplayReader",
    "export_jsonl",
]
