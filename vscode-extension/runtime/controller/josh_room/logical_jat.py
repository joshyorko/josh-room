"""Validated canonical descriptors for complete Room Store recovery points."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MAX_LOGICAL_JAT_BYTES = 64 * 1024
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOP_LEVEL = {
    "format_version",
    "payload_kind",
    "logical_jat_id",
    "dimension_id",
    "encryption_domain_id",
    "room_id",
    "created_at",
    "parent_logical_jat_id",
    "origin_room_id",
    "capture_policy_sha256",
    "workspace",
    "components",
    "source",
    "producer",
}
_WORKSPACE = {
    "engine",
    "repository_id",
    "repository_format",
    "snapshot_id",
    "tree_id",
    "parent_snapshot_id",
    "source_path",
    "logical_bytes",
    "data_added",
    "data_added_packed",
}
_COMPONENTS = {"rcc_environment", "homebrew_recovery", "hauler_content"}


def _object(value: object, name: str, required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"logical JAT {name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"logical JAT {name} keys must be strings")
    optional = optional or set()
    unknown = value.keys() - required - optional
    if unknown:
        raise ValueError(f"unknown logical JAT {name} field: {min(unknown)}")
    missing = required - value.keys()
    if missing:
        raise ValueError(f"missing logical JAT {name} field: {min(missing)}")
    return value


def _id(value: object, name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"logical JAT {name} is invalid")


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"logical JAT {name} is invalid")


def _integer(value: object, name: str, *, minimum: int = 0, maximum: int = (1 << 63) - 1) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"logical JAT {name} must be an integer in range")


def _version(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value)
    ):
        raise ValueError(f"logical JAT {name} is invalid")


def _content_digest(value: object, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"logical JAT {name} is invalid")


def _archive(value: dict[str, Any], name: str, member_basename: str) -> None:
    _sha256(value["archive_sha256"], f"component {name} archive digest")
    _integer(value["archive_size"], f"component {name} archive size", minimum=1)
    if value["member_basename"] != member_basename:
        raise ValueError(f"logical JAT component {name} member basename is invalid")


def _validate_component(value: object, name: str, workspace: dict[str, Any]) -> None:
    if value is None:
        return
    if name == "rcc_environment":
        component = _object(
            value,
            f"component {name}",
            {
                "kind", "snapshot", "archive_sha256", "archive_size", "member_basename",
                "artifact_digest", "specification_digest", "platform", "rcc_version", "robot_relative_path",
            },
            {"source_input_sha256"},
        )
        if component["kind"] != "rcca":
            raise ValueError(f"logical JAT component {name} kind mismatch")
        _archive(component, name, "rcc-environment.rcca")
        if "source_input_sha256" in component:
            _sha256(component["source_input_sha256"], f"component {name} source input digest")
        _content_digest(component["artifact_digest"], f"component {name} artifact digest")
        _content_digest(component["specification_digest"], f"component {name} specification digest")
        _id(component["platform"], f"component {name} platform")
        _version(component["rcc_version"], f"component {name} RCC version")
        relative_path = component["robot_relative_path"]
        if (
            not isinstance(relative_path, str)
            or len(relative_path) > 256
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9._/-]+", relative_path)
        ):
            raise ValueError(f"logical JAT component {name} robot relative path is invalid")
    elif name == "homebrew_recovery":
        component = _object(
            value,
            f"component {name}",
            {"kind", "snapshot", "archive_sha256", "archive_size", "member_basename"},
        )
        if component["kind"] != "homebrew-recovery":
            raise ValueError(f"logical JAT component {name} kind mismatch")
        _archive(component, name, "homebrew-recovery.tar.zst")
    else:
        component = _object(
            value,
            f"component {name}",
            {"kind", "snapshot", "archive_sha256", "archive_size", "member_basename", "references"},
            {"source_input_sha256", "hauler_version"},
        )
        if component["kind"] != "hauler-content":
            raise ValueError(f"logical JAT component {name} kind mismatch")
        _archive(component, name, "hauler-content.tar.zst")
        if "source_input_sha256" in component:
            _sha256(component["source_input_sha256"], f"component {name} source input digest")
        if "hauler_version" in component:
            _version(component["hauler_version"], f"component {name} Hauler version")
        references = component["references"]
        if not isinstance(references, list) or not 1 <= len(references) <= 4096:
            raise ValueError("logical JAT Hauler references are invalid")
        seen = set()
        for reference_value in references:
            reference = _object(
                reference_value,
                "Hauler reference",
                {"digest"},
                {"kind", "media_type"},
            )
            _content_digest(reference["digest"], "Hauler reference digest")
            if "media_type" in reference:
                media_type = reference["media_type"]
                if (
                    not isinstance(media_type, str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}", media_type) is None
                ):
                    raise ValueError("logical JAT Hauler reference media type is invalid")
            else:
                media_type = None
            if "kind" in reference and reference["kind"] not in {"image", "chart", "file"}:
                raise ValueError("logical JAT Hauler reference kind is invalid")
            if "kind" not in reference and "media_type" not in reference:
                raise ValueError("logical JAT Hauler reference needs kind or media type")
            key = (reference["digest"], reference.get("kind"), media_type)
            if key in seen:
                raise ValueError("logical JAT Hauler references must be unique")
            seen.add(key)
    snapshot = _object(component["snapshot"], f"component {name} snapshot", {"repository_id", "repository_format", "snapshot_id", "tree_id"})
    _sha256(snapshot["repository_id"], f"component {name} repository id")
    _integer(snapshot["repository_format"], f"component {name} repository format", minimum=2, maximum=2)
    _sha256(snapshot["snapshot_id"], f"component {name} snapshot id")
    _sha256(snapshot["tree_id"], f"component {name} tree id")
    if (
        snapshot["repository_id"] != workspace["repository_id"]
        or snapshot["repository_format"] != workspace["repository_format"]
    ):
        raise ValueError(f"logical JAT component {name} repository binding mismatch")


def _validate(body: object) -> dict[str, Any]:
    descriptor = _object(
        body,
        "descriptor",
        _TOP_LEVEL - {"parent_logical_jat_id", "origin_room_id"},
        {"parent_logical_jat_id", "origin_room_id"},
    )
    _integer(descriptor["format_version"], "format version", minimum=1, maximum=1)
    if descriptor["payload_kind"] != "room-store-v1":
        raise ValueError("unsupported logical JAT payload kind")
    for name in ("logical_jat_id", "dimension_id", "encryption_domain_id", "room_id"):
        _id(descriptor[name], name)
    if "origin_room_id" in descriptor:
        _id(descriptor["origin_room_id"], "origin room id")
        if descriptor["origin_room_id"] == descriptor["room_id"]:
            raise ValueError("logical JAT origin room must differ from the copied Room")
    if "parent_logical_jat_id" in descriptor:
        _id(descriptor["parent_logical_jat_id"], "parent logical JAT id")
    created_at = descriptor["created_at"]
    if not isinstance(created_at, str) or _RFC3339.fullmatch(created_at) is None:
        raise ValueError("logical JAT created_at must be RFC3339")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError("logical JAT created_at must be RFC3339") from error
    _sha256(descriptor["capture_policy_sha256"], "capture policy digest")

    workspace = _object(
        descriptor["workspace"],
        "workspace",
        _WORKSPACE - {"parent_snapshot_id"},
        {"parent_snapshot_id"},
    )
    if workspace["engine"] != "restic":
        raise ValueError("unsupported logical JAT workspace engine")
    _sha256(workspace["repository_id"], "workspace repository id")
    _integer(workspace["repository_format"], "workspace repository format", minimum=2, maximum=2)
    _sha256(workspace["snapshot_id"], "workspace snapshot id")
    _sha256(workspace["tree_id"], "workspace tree id")
    if "parent_snapshot_id" in workspace:
        _sha256(workspace["parent_snapshot_id"], "workspace parent snapshot id")
    if workspace["source_path"] != ".":
        raise ValueError("logical JAT workspace source path must be the relative root")
    for name in ("logical_bytes", "data_added", "data_added_packed"):
        _integer(workspace[name], f"workspace {name}")

    components = _object(descriptor["components"], "components", _COMPONENTS)
    for name in _COMPONENTS:
        _validate_component(components[name], name, workspace)

    source = _object(descriptor["source"], "source", set(), {"git_commit", "dirty"})
    if "git_commit" in source and (
        not isinstance(source["git_commit"], str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source["git_commit"]) is None
    ):
        raise ValueError("logical JAT source git commit is invalid")
    if "dirty" in source and type(source["dirty"]) is not bool:
        raise ValueError("logical JAT source dirty marker must be a boolean")
    producer = _object(
        descriptor["producer"],
        "producer",
        {"josh_room_version", "restic_version", "source_platform", "restore_platforms"},
    )
    _version(producer["josh_room_version"], "Josh Room version")
    _version(producer["restic_version"], "restic version")
    supported_platforms = {"linux-x64", "win32-x64"}
    if not isinstance(producer["source_platform"], str) or producer["source_platform"] not in supported_platforms:
        raise ValueError("logical JAT source platform is invalid")
    restore_platforms = producer["restore_platforms"]
    if (
        not isinstance(restore_platforms, list)
        or not restore_platforms
        or any(not isinstance(platform, str) or platform not in supported_platforms for platform in restore_platforms)
        or len(set(restore_platforms)) != len(restore_platforms)
        or producer["source_platform"] not in restore_platforms
    ):
        raise ValueError("logical JAT restore platforms are invalid")
    return descriptor


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON number: {value}")


@dataclass(frozen=True, slots=True, init=False)
class LogicalJat:
    """A validated descriptor stored as canonical JSON, independent of lineage."""

    _canonical_json: str
    sha256: str

    @classmethod
    def from_dict(cls, body: object) -> LogicalJat:
        descriptor = _validate(body)
        canonical = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded = canonical.encode("utf-8")
        if len(encoded) > MAX_LOGICAL_JAT_BYTES:
            raise ValueError("logical JAT descriptor exceeds the 65536-byte size limit")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_canonical_json", canonical)
        object.__setattr__(instance, "sha256", hashlib.sha256(encoded).hexdigest())
        return instance

    @classmethod
    def from_json(cls, value: str | bytes) -> LogicalJat:
        if isinstance(value, bytes):
            if len(value) > MAX_LOGICAL_JAT_BYTES:
                raise ValueError("logical JAT descriptor exceeds the 65536-byte size limit")
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("logical JAT descriptor must be UTF-8 JSON") from error
        elif isinstance(value, str):
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("logical JAT descriptor must be UTF-8 JSON") from error
            if len(encoded) > MAX_LOGICAL_JAT_BYTES:
                raise ValueError("logical JAT descriptor exceeds the 65536-byte size limit")
            text = value
        else:
            raise TypeError("logical JAT descriptor JSON must be str or bytes")
        try:
            body = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError("logical JAT descriptor is invalid JSON") from error
        return cls.from_dict(body)

    def to_json(self) -> str:
        return self._canonical_json

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_json)
