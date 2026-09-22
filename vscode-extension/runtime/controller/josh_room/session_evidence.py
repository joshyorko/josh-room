"""Transport-neutral Session Evidence Envelope v1 validation primitives.

This module validates the public contract only. It does not discover Codex
files, classify material, capture records, encrypt documents, or publish them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

SCHEMA_NAME = "codex-session-evidence"
CURRENT_MAJOR = 1
CURRENT_MINOR = 0
MAX_IDENTIFIER_LENGTH = 128
MAX_VERSION_LENGTH = 64
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REMOTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)+$")
BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9]$")
RFC3339_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)
PATH_METADATA_PATTERN = re.compile(r"^(?:path|.*_path)$")
CONTENT_TYPES = frozenset({
    "application/vnd.josh.codex-session-segment+json",
    "application/vnd.josh.codex-session-asset",
    "application/vnd.josh.codex-session-final+json",
    "application/vnd.josh.codex-index-event+json",
})
KINDS = {"session-segment", "session-asset", "session-final", "index-event"}
SURFACES = {"cli", "desktop", "vscode", "app-server", "import", "unknown"}
STATUSES = {"partial", "complete", "recovered", "quarantined"}
POLICY_DECISIONS = {"allow", "deny", "local-only", "quarantine"}
SENSITIVITIES = {"normal", "sensitive", "unknown"}
MEDIA_CATEGORIES = {"image", "audio", "video", "document", "archive", "other", "unknown"}
REPRESENTATIONS = {"active-jsonl", "archived-jsonl", "compressed-jsonl-zst", "unknown"}
DISCOVERY_STATES = {"discovered", "uploaded", "quarantined", "retracted"}


class ErrorCode(str, Enum):
    DOCUMENT_NOT_OBJECT = "document_not_object"
    UNKNOWN_SCHEMA = "unknown_schema"
    UNKNOWN_DOCUMENT_KIND = "unknown_document_kind"
    MISSING_FIELD = "missing_field"
    WRONG_TYPE = "wrong_type"
    INVALID_VERSION = "invalid_version"
    UNKNOWN_MAJOR = "unknown_major"
    INVALID_IDENTIFIER = "invalid_identifier"
    IDENTIFIER_TOO_LONG = "identifier_too_long"
    INVALID_DIGEST = "invalid_digest"
    DIGEST_MISMATCH = "digest_mismatch"
    INVALID_CHECKPOINT = "invalid_checkpoint"
    MISSING_CHECKPOINT = "missing_checkpoint"
    INVALID_ASSET_REFERENCE = "invalid_asset_reference"
    DUPLICATE_ASSET_REFERENCE = "duplicate_asset_reference"
    INVALID_REPOSITORY_PROVENANCE = "invalid_repository_provenance"
    INVALID_ENUM = "invalid_enum"
    INVALID_METADATA = "invalid_metadata"
    INVALID_TIMESTAMP = "invalid_timestamp"
    UNKNOWN_FIELD = "unknown_field"


class ValidationDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class ContractError:
    code: str
    path: str


@dataclass(frozen=True)
class ValidationResult:
    disposition: ValidationDisposition
    errors: tuple[ContractError, ...]
    document: dict[str, Any] | None


def canonical_json(value: Any) -> bytes:
    """Serialize JSON values deterministically as UTF-8 bytes.

    Envelope identity contains no host paths. Adapter-local paths therefore
    never enter this value; arbitrary transcript/tool text remains inert data
    and is hashed as supplied.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_document(document: Any) -> ValidationResult:
    if not isinstance(document, dict):
        return _result(ValidationDisposition.REJECTED, [ContractError(ErrorCode.DOCUMENT_NOT_OBJECT.value, "$")])

    errors: list[ContractError] = []
    _validate_path_metadata(document, errors)
    if document.get("schema_name") != SCHEMA_NAME:
        errors.append(ContractError(ErrorCode.UNKNOWN_SCHEMA.value, "$.schema_name"))

    kind = document.get("kind")
    if not isinstance(kind, str):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.kind"))
    elif kind not in KINDS:
        errors.append(ContractError(ErrorCode.UNKNOWN_DOCUMENT_KIND.value, "$.kind"))

    version = document.get("schema_version")
    major = _validate_version(version, errors)
    if major is not None and major != CURRENT_MAJOR:
        return _result(ValidationDisposition.QUARANTINED, [ContractError(ErrorCode.UNKNOWN_MAJOR.value, "$.schema_version.major")])

    for field in ("schema_name", "schema_version", "kind", "event_id"):
        if field not in document:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.{field}"))

    _validate_identifier_field(document, "event_id", "$.event_id", errors)
    for field in ("session_id", "thread_id", "turn_id", "profile_id", "workspace_id", "device_id"):
        if field in document:
            _validate_identifier_field(document, field, f"$.{field}", errors)

    if kind == "session-segment" or kind == "session-final":
        _validate_source(document.get("source"), errors)
        if "subagent" in document:
            _validate_subagent(document["subagent"], errors)
        for field in ("profile_id", "workspace_id", "device_id"):
            if field not in document:
                errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.{field}"))
        if "checkpoint" not in document:
            errors.append(ContractError(ErrorCode.MISSING_CHECKPOINT.value, "$.checkpoint"))
        else:
            _validate_checkpoint(document["checkpoint"], errors)
        _validate_record_metadata(document, errors, records_required=kind == "session-segment")
        _validate_capture(document.get("capture"), errors)
        if "repository" in document:
            _validate_repository(document["repository"], errors)
        if "observed_at" in document:
            _validate_timestamp(document["observed_at"], "$.observed_at", errors)
        if kind == "session-segment":
            if "previous_segment_sha256" in document:
                _validate_digest(document["previous_segment_sha256"], "$.previous_segment_sha256", errors)
            if "asset_refs" in document:
                _validate_asset_refs(document["asset_refs"], errors)
        elif "last_segment_sha256" in document:
            _validate_digest(document["last_segment_sha256"], "$.last_segment_sha256", errors)
    elif kind == "session-asset":
        for field in ("asset_id", "source_event_id", "sha256", "size", "media_category", "content_type", "capture"):
            if field not in document:
                errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.{field}"))
        _validate_identifier_field(document, "asset_id", "$.asset_id", errors)
        _validate_identifier_field(document, "source_event_id", "$.source_event_id", errors)
        if "sha256" in document:
            _validate_digest(document["sha256"], "$.sha256", errors)
        if "size" in document:
            _validate_size(document["size"], "$.size", errors)
        if "media_category" in document:
            _validate_enum(document["media_category"], MEDIA_CATEGORIES, "$.media_category", errors)
        if "content_type" in document and (not isinstance(document["content_type"], str) or not document["content_type"]):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.content_type"))
        if "capture" in document:
            _validate_capture(document["capture"], errors)
    elif kind == "index-event":
        for field in ("evidence_kind", "evidence_event_id", "content_sha256", "ciphertext_sha256", "ciphertext_size", "content_type", "discovery"):
            if field not in document:
                errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.{field}"))
        if "evidence_kind" in document:
            _validate_enum(document["evidence_kind"], KINDS - {"index-event"}, "$.evidence_kind", errors)
        _validate_identifier_field(document, "evidence_event_id", "$.evidence_event_id", errors)
        if "content_sha256" in document:
            _validate_digest(document["content_sha256"], "$.content_sha256", errors)
        if "ciphertext_sha256" in document:
            _validate_digest(document["ciphertext_sha256"], "$.ciphertext_sha256", errors)
        if "ciphertext_size" in document:
            _validate_size(document["ciphertext_size"], "$.ciphertext_size", errors)
        if "content_type" in document and (not isinstance(document["content_type"], str) or document["content_type"] not in CONTENT_TYPES):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.content_type"))
        discovery = document.get("discovery")
        if not isinstance(discovery, dict):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.discovery"))
        else:
            _validate_object_keys(discovery, {"state", "observed_at"}, "$.discovery", errors)
            _validate_enum(discovery.get("state"), DISCOVERY_STATES, "$.discovery.state", errors)
            if not isinstance(discovery.get("observed_at"), str) or not discovery["observed_at"]:
                errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.discovery.observed_at"))
            else:
                _validate_timestamp(discovery["observed_at"], "$.discovery.observed_at", errors)

    if errors:
        return _result(ValidationDisposition.REJECTED, errors)
    return _result(ValidationDisposition.ACCEPTED, [], document)


def minimal_unencrypted_metadata(
    document: Mapping[str, Any], *, ciphertext_sha256: str, ciphertext_size: int, content_type: str
) -> dict[str, Any]:
    result = validate_document(dict(document))
    if result.disposition is not ValidationDisposition.ACCEPTED:
        raise ValueError("document is not valid session evidence")
    metadata_errors: list[ContractError] = []
    _validate_digest(ciphertext_sha256, "$.ciphertext_sha256", metadata_errors)
    if metadata_errors:
        raise ValueError("ciphertext metadata is invalid")
    if type(ciphertext_size) is not int or ciphertext_size < 0:
        raise ValueError("ciphertext metadata is invalid")
    if not isinstance(content_type, str) or content_type not in CONTENT_TYPES:
        raise ValueError("content type metadata is invalid")
    return {
        "schema_family": SCHEMA_NAME,
        "schema_major": CURRENT_MAJOR,
        "ciphertext_sha256": ciphertext_sha256,
        "ciphertext_size": ciphertext_size,
        "content_type": content_type,
    }


def _result(disposition: ValidationDisposition, errors: list[ContractError], document: dict | None = None) -> ValidationResult:
    return ValidationResult(disposition, tuple(errors), copy.deepcopy(document) if document is not None else None)


def _validate_version(version: Any, errors: list[ContractError]) -> int | None:
    if not isinstance(version, dict):
        errors.append(ContractError(ErrorCode.INVALID_VERSION.value, "$.schema_version"))
        return None
    _validate_object_keys(version, {"major", "minor"}, "$.schema_version", errors)
    if type(version.get("major")) is not int or type(version.get("minor")) is not int:
        errors.append(ContractError(ErrorCode.INVALID_VERSION.value, "$.schema_version"))
        return None
    if version["major"] < 0 or version["minor"] < 0:
        errors.append(ContractError(ErrorCode.INVALID_VERSION.value, "$.schema_version"))
        return None
    return version["major"]


def _validate_identifier_field(document: Mapping[str, Any], field: str, path: str, errors: list[ContractError]) -> None:
    if field not in document:
        return
    value = document[field]
    if not isinstance(value, str):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, path))
    elif len(value) > MAX_IDENTIFIER_LENGTH:
        errors.append(ContractError(ErrorCode.IDENTIFIER_TOO_LONG.value, path))
    elif not IDENTIFIER_PATTERN.fullmatch(value):
        errors.append(ContractError(ErrorCode.INVALID_IDENTIFIER.value, path))


def _validate_source(source: Any, errors: list[ContractError]) -> None:
    if not isinstance(source, dict):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.source"))
        return
    _validate_object_keys(source, {"surface", "adapter", "adapter_version", "codex_version"}, "$.source", errors)
    _validate_enum(source.get("surface"), SURFACES, "$.source.surface", errors)
    for field in ("adapter", "adapter_version", "codex_version"):
        if field in source:
            value = source[field]
            limit = MAX_VERSION_LENGTH if field != "adapter" else MAX_IDENTIFIER_LENGTH
            if not isinstance(value, str):
                errors.append(ContractError(ErrorCode.WRONG_TYPE.value, f"$.source.{field}"))
            elif len(value) > limit:
                errors.append(ContractError(ErrorCode.IDENTIFIER_TOO_LONG.value, f"$.source.{field}"))
            elif not IDENTIFIER_PATTERN.fullmatch(value):
                errors.append(ContractError(ErrorCode.INVALID_IDENTIFIER.value, f"$.source.{field}"))
    if "adapter" not in source:
        errors.append(ContractError(ErrorCode.MISSING_FIELD.value, "$.source.adapter"))
    if "adapter_version" not in source:
        errors.append(ContractError(ErrorCode.MISSING_FIELD.value, "$.source.adapter_version"))
    if "surface" not in source:
        errors.append(ContractError(ErrorCode.MISSING_FIELD.value, "$.source.surface"))


def _validate_subagent(subagent: Any, errors: list[ContractError]) -> None:
    if not isinstance(subagent, dict):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.subagent"))
        return
    _validate_object_keys(subagent, {"id", "type"}, "$.subagent", errors)
    for field in ("id", "type"):
        if field not in subagent:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.subagent.{field}"))
        else:
            _validate_identifier_field(subagent, field, f"$.subagent.{field}", errors)


def _validate_checkpoint(checkpoint: Any, errors: list[ContractError]) -> None:
    if not isinstance(checkpoint, dict):
        errors.append(ContractError(ErrorCode.INVALID_CHECKPOINT.value, "$.checkpoint"))
        return
    _validate_object_keys(checkpoint, {"source", "representation", "start", "end", "prefix_sha256"}, "$.checkpoint", errors)
    for field in ("source", "representation", "start", "end", "prefix_sha256"):
        if field not in checkpoint:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.checkpoint.{field}"))
    if "source" in checkpoint:
        _validate_identifier_field(checkpoint, "source", "$.checkpoint.source", errors)
    if "representation" in checkpoint:
        _validate_enum(checkpoint["representation"], REPRESENTATIONS, "$.checkpoint.representation", errors)
    for field in ("start", "end"):
        if field in checkpoint and (type(checkpoint[field]) is not int or checkpoint[field] < 0):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, f"$.checkpoint.{field}"))
    if type(checkpoint.get("start")) is int and type(checkpoint.get("end")) is int and checkpoint["end"] < checkpoint["start"]:
        errors.append(ContractError(ErrorCode.INVALID_CHECKPOINT.value, "$.checkpoint"))
    if "prefix_sha256" in checkpoint:
        _validate_digest(checkpoint["prefix_sha256"], "$.checkpoint.prefix_sha256", errors)


def _validate_record_metadata(document: Mapping[str, Any], errors: list[ContractError], *, records_required: bool) -> None:
    for field in ("record_count", "content_size"):
        if field not in document:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.{field}"))
        else:
            _validate_size(document[field], f"$.{field}", errors)
    _validate_digest(document.get("content_sha256"), "$.content_sha256", errors)
    if not records_required:
        return
    if not isinstance(document.get("records"), list) or not all(isinstance(record, dict) for record in document["records"]):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.records"))
    else:
        if type(document.get("record_count")) is int and document["record_count"] != len(document["records"]):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.record_count"))
        try:
            payload = canonical_json(document["records"])
        except (TypeError, ValueError, OverflowError):
            errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.records"))
            return
        if type(document.get("content_size")) is int and document["content_size"] != len(payload):
            errors.append(ContractError(ErrorCode.DIGEST_MISMATCH.value, "$.content_size"))
        if isinstance(document.get("content_sha256"), str) and document["content_sha256"] != hashlib.sha256(payload).hexdigest():
            errors.append(ContractError(ErrorCode.DIGEST_MISMATCH.value, "$.content_sha256"))


def _validate_asset_refs(refs: Any, errors: list[ContractError]) -> None:
    if not isinstance(refs, list):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.asset_refs"))
        return
    seen: set[str] = set()
    for index, reference in enumerate(refs):
        path = f"$.asset_refs[{index}]"
        if not isinstance(reference, dict):
            errors.append(ContractError(ErrorCode.INVALID_ASSET_REFERENCE.value, path))
            continue
        asset_id = reference.get("asset_id")
        if isinstance(asset_id, str) and asset_id in seen:
            errors.append(ContractError(ErrorCode.DUPLICATE_ASSET_REFERENCE.value, f"{path}.asset_id"))
        if isinstance(asset_id, str):
            seen.add(asset_id)
        _validate_identifier_field(reference, "asset_id", f"{path}.asset_id", errors)
        _validate_digest(reference.get("sha256"), f"{path}.sha256", errors)
        _validate_size(reference.get("size"), f"{path}.size", errors)
        _validate_enum(reference.get("media_category"), MEDIA_CATEGORIES, f"{path}.media_category", errors)
        if set(reference) - {"asset_id", "sha256", "size", "media_category"}:
            errors.append(ContractError(ErrorCode.INVALID_ASSET_REFERENCE.value, path))


def _validate_repository(repository: Any, errors: list[ContractError]) -> None:
    if not isinstance(repository, dict):
        errors.append(ContractError(ErrorCode.INVALID_REPOSITORY_PROVENANCE.value, "$.repository"))
        return
    required = {"remote", "commit", "branch", "dirty"}
    _validate_object_keys(repository, required, "$.repository", errors)
    for field in required:
        if field not in repository:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.repository.{field}"))
    if set(repository) - required or required - set(repository):
        return
    remote = repository["remote"]
    commit = repository["commit"]
    branch = repository["branch"]
    if remote != "unknown" and (not isinstance(remote, str) or len(remote) > MAX_IDENTIFIER_LENGTH or not REMOTE_PATTERN.fullmatch(remote)):
        errors.append(ContractError(ErrorCode.INVALID_REPOSITORY_PROVENANCE.value, "$.repository.remote"))
    if commit != "unknown" and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit)):
        errors.append(ContractError(ErrorCode.INVALID_REPOSITORY_PROVENANCE.value, "$.repository.commit"))
    if branch != "unknown" and (not isinstance(branch, str) or len(branch) > MAX_IDENTIFIER_LENGTH or not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or "//" in branch):
        errors.append(ContractError(ErrorCode.INVALID_REPOSITORY_PROVENANCE.value, "$.repository.branch"))
    if not isinstance(repository["dirty"], (bool, str)):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.repository.dirty"))
    elif isinstance(repository["dirty"], str) and repository["dirty"] != "unknown":
        errors.append(ContractError(ErrorCode.INVALID_REPOSITORY_PROVENANCE.value, "$.repository.dirty"))


def _validate_capture(capture: Any, errors: list[ContractError]) -> None:
    if not isinstance(capture, dict):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.capture"))
        return
    _validate_object_keys(capture, {"status", "policy_decision", "sensitivity", "counters"}, "$.capture", errors)
    for field, values in (("status", STATUSES), ("policy_decision", POLICY_DECISIONS), ("sensitivity", SENSITIVITIES)):
        if field not in capture:
            errors.append(ContractError(ErrorCode.MISSING_FIELD.value, f"$.capture.{field}"))
        else:
            _validate_enum(capture[field], values, f"$.capture.{field}", errors)
    counters = capture.get("counters")
    if "counters" not in capture:
        errors.append(ContractError(ErrorCode.MISSING_FIELD.value, "$.capture.counters"))
    elif not isinstance(counters, dict):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.capture.counters"))
    else:
        for field, value in counters.items():
            if not isinstance(field, str) or type(value) is not int or value < 0:
                errors.append(ContractError(ErrorCode.WRONG_TYPE.value, "$.capture.counters"))


def _validate_digest(value: Any, path: str, errors: list[ContractError]) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        errors.append(ContractError(ErrorCode.INVALID_DIGEST.value, path))


def _validate_size(value: Any, path: str, errors: list[ContractError]) -> None:
    if type(value) is not int or value < 0:
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, path))


def _validate_enum(value: Any, values: set[Any], path: str, errors: list[ContractError]) -> None:
    try:
        valid = value in values
    except TypeError:
        valid = False
    if not valid:
        errors.append(ContractError(ErrorCode.INVALID_ENUM.value, path))


def _validate_object_keys(value: Mapping[str, Any], allowed: set[str], path: str, errors: list[ContractError]) -> None:
    for key in value:
        if key not in allowed:
            errors.append(ContractError(ErrorCode.UNKNOWN_FIELD.value, f"{path}.{key}"))


def _validate_path_metadata(document: Mapping[str, Any], errors: list[ContractError]) -> None:
    for field in document:
        if isinstance(field, str) and PATH_METADATA_PATTERN.fullmatch(field):
            errors.append(ContractError(ErrorCode.INVALID_METADATA.value, f"$.{field}"))


def _validate_timestamp(value: Any, path: str, errors: list[ContractError]) -> None:
    if not isinstance(value, str):
        errors.append(ContractError(ErrorCode.WRONG_TYPE.value, path))
        return
    if not RFC3339_TIMESTAMP_PATTERN.fullmatch(value):
        errors.append(ContractError(ErrorCode.INVALID_TIMESTAMP.value, path))
        return
    try:
        normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        datetime.fromisoformat(normalized)
    except (ValueError, OverflowError):
        errors.append(ContractError(ErrorCode.INVALID_TIMESTAMP.value, path))
