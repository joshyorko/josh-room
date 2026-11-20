"""Closed, transport-neutral ViewStateProvider contract and strict validators."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import inspect
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, runtime_checkable

SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ATTACHMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MEDIA_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
READONLY_SQL_START_RE = re.compile(r"^\s*(?:SELECT|WITH|EXPLAIN)\b", re.IGNORECASE)
DISALLOWED_SQL_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|REINDEX|PRAGMA)\b",
    re.IGNORECASE,
)

ALLOWED_EMBED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "text/html",
    }
)
ALLOWED_PREVIEW_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/x-diff",
        "application/json",
        "application/x-josh-run+json",
    }
)
ALLOWED_PREVIEW_STATUSES = frozenset({"available", "unavailable", "redacted", "omitted"})
ALLOWED_NODE_KINDS = frozenset({"directory", "file", "run", "artifact"})
ALLOWED_LINK_RELATIONS = frozenset(
    {
        "producer",
        "output",
        "consumer",
        "depends_on",
        "caused_by",
        "references",
        "artifact_of",
        "configures",
        "verifies",
    }
)
MAX_PREVIEW_BODY_CHARS = 16384
ALLOWED_VIEW_MODES = frozenset({"3d", "grid"})
ALLOWED_LEGEND_SHAPES = frozenset({"solid", "notch", "hollow"})
NON_SENSITIVE_MARKINGS = frozenset({"public", "synthetic"})
SENSITIVITY_ORDER = {
    "public": 0,
    "synthetic": 1,
    "internal": 2,
    "sensitive": 3,
    "private": 4,
    "confidential": 5,
    "restricted": 6,
    "regulated": 7,
}
SENSITIVITY_LEVELS = frozenset(SENSITIVITY_ORDER)


class ViewValidationError(ValueError):
    """Typed public-safe validation error for Room View contracts."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_safe_label(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not SAFE_LABEL_RE.fullmatch(value):
        raise ViewValidationError("invalid-label", f"invalid {field_name}")
    return value


def validate_attachment_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not ATTACHMENT_NAME_RE.fullmatch(value)
        or ".." in value
    ):
        raise ViewValidationError("invalid-attachment-name", "invalid attachment name")
    return value


def validate_sha256(value: object, field_name: str = "digest") -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ViewValidationError("invalid-digest", f"invalid {field_name}")
    return value


def validate_media_type(value: object) -> str:
    if not isinstance(value, str) or not MEDIA_TYPE_RE.fullmatch(value.lower()):
        raise ViewValidationError("invalid-media-type", "invalid media type")
    return value.lower()


def validate_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not ISO_TIMESTAMP_RE.fullmatch(value):
        raise ViewValidationError("invalid-timestamp", f"invalid {field_name}")
    return value


def _validate_non_empty_text(value: object, field_name: str, *, max_len: int = 160) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_len
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise ViewValidationError("invalid-text", f"invalid {field_name}")
    return value


def _validate_multiline_text(value: object, field_name: str, *, max_len: int = MAX_PREVIEW_BODY_CHARS) -> str:
    if not isinstance(value, str) or len(value) > max_len:
        raise ViewValidationError("invalid-preview-body", f"invalid {field_name}")
    for ch in value:
        code = ord(ch)
        if code == 0x7F or (code < 0x20 and ch not in {"\n", "\r", "\t"}):
            raise ViewValidationError("control-chars", f"control character in {field_name}")
    return value


def _validate_scalar(value: object, field_name: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ViewValidationError("invalid-scalar", f"non-finite numeric value in {field_name}")
        return value
    if isinstance(value, str):
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ViewValidationError("invalid-scalar", f"control character in {field_name}")
        return value
    raise ViewValidationError("invalid-scalar", f"unsupported non-scalar value in {field_name}")


def _validate_scalar_mapping(raw: object, field_name: str) -> MappingProxyType[str, str | int | float | bool | None]:
    if not isinstance(raw, Mapping):
        raise ViewValidationError("invalid-mapping", f"{field_name} must be a mapping")
    cleaned: dict[str, str | int | float | bool | None] = {}
    for key, val in raw.items():
        safe_key = validate_safe_label(key, f"{field_name} key")
        cleaned[safe_key] = _validate_scalar(val, f"{field_name}.{safe_key}")
    return MappingProxyType(cleaned)


@dataclass(frozen=True, slots=True)
class StageDescriptor:
    id: str
    name: str
    order: int
    terminal: bool = False
    failure: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_safe_label(self.id, "stage id"))
        object.__setattr__(self, "name", _validate_non_empty_text(self.name, "stage name", max_len=64))
        if type(self.order) is not int or self.order < 0:
            raise ViewValidationError("invalid-stage", "stage order must be a non-negative integer")
        if type(self.terminal) is not bool or type(self.failure) is not bool:
            raise ViewValidationError("invalid-stage", "stage flags must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "order": self.order,
            "terminal": self.terminal,
            "failure": self.failure,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> StageDescriptor:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-stage", "stage must be an object")
        return cls(
            id=raw.get("id"),  # type: ignore[arg-type]
            name=raw.get("name"),  # type: ignore[arg-type]
            order=raw.get("order"),  # type: ignore[arg-type]
            terminal=bool(raw.get("terminal", False)),
            failure=bool(raw.get("failure", False)),
        )


@dataclass(frozen=True, slots=True)
class LegendEntry:
    label: str
    color: str
    shape: str = "solid"

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _validate_non_empty_text(self.label, "legend label", max_len=48))
        if not isinstance(self.color, str) or not HEX_COLOR_RE.fullmatch(self.color):
            raise ViewValidationError("invalid-color", "legend color must be #RRGGBB")
        if self.shape not in ALLOWED_LEGEND_SHAPES:
            raise ViewValidationError("invalid-shape", "legend shape must be solid, notch, or hollow")

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "color": self.color, "shape": self.shape}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> LegendEntry:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-legend", "legend entry must be an object")
        return cls(
            label=raw.get("label"),  # type: ignore[arg-type]
            color=raw.get("color"),  # type: ignore[arg-type]
            shape=str(raw.get("shape", "solid")),
        )


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    name: str
    min: float | None = None
    max: float | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_non_empty_text(self.name, "metric name", max_len=64))
        for bound_name in ("min", "max"):
            bound = getattr(self, bound_name)
            if bound is not None:
                if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(float(bound)):
                    raise ViewValidationError("invalid-metric", f"metric {bound_name} must be finite")
                object.__setattr__(self, bound_name, float(bound))
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ViewValidationError("invalid-metric", "metric min cannot exceed max")
        if self.unit is not None:
            object.__setattr__(self, "unit", _validate_non_empty_text(self.unit, "metric unit", max_len=24))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"name": self.name}
        if self.min is not None:
            result["min"] = self.min
        if self.max is not None:
            result["max"] = self.max
        if self.unit is not None:
            result["unit"] = self.unit
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MetricDescriptor:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-metric", "metric must be an object")
        return cls(
            name=raw.get("name"),  # type: ignore[arg-type]
            min=raw.get("min"),  # type: ignore[arg-type]
            max=raw.get("max"),  # type: ignore[arg-type]
            unit=raw.get("unit"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class SqlPreset:
    id: str
    name: str
    sql: str
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_safe_label(self.id, "sql preset id"))
        object.__setattr__(self, "name", _validate_non_empty_text(self.name, "sql preset name", max_len=64))
        if not isinstance(self.sql, str) or not self.sql.strip() or len(self.sql) > 2048:
            raise ViewValidationError("invalid-sql-preset", "sql preset query is invalid")
        if not READONLY_SQL_START_RE.match(self.sql) or DISALLOWED_SQL_RE.search(self.sql):
            raise ViewValidationError("invalid-sql-preset", "sql preset must be read-only")
        if self.description:
            object.__setattr__(
                self,
                "description",
                _validate_non_empty_text(self.description, "sql preset description", max_len=160),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "sql": self.sql,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SqlPreset:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-sql-preset", "sql preset must be an object")
        return cls(
            id=raw.get("id"),  # type: ignore[arg-type]
            name=raw.get("name"),  # type: ignore[arg-type]
            sql=raw.get("sql"),  # type: ignore[arg-type]
            description=str(raw.get("description", "")),
        )


@dataclass(frozen=True, slots=True)
class ViewDescriptor:
    title: str
    stages: tuple[StageDescriptor, ...]
    group_label: str
    legend: Mapping[str, LegendEntry]
    metric: MetricDescriptor | None = None
    view: str = "3d"
    tool: str = "josh-room"
    tool_version: str = "0.1.0"
    sensitivity: str = "synthetic"
    sql_presets: tuple[SqlPreset, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _validate_non_empty_text(self.title, "descriptor title", max_len=96))
        stages = tuple(self.stages)
        if not stages or any(not isinstance(stage, StageDescriptor) for stage in stages):
            raise ViewValidationError("invalid-descriptor", "descriptor requires valid stages")
        stage_ids = [stage.id for stage in stages]
        if len(set(stage_ids)) != len(stage_ids):
            raise ViewValidationError("duplicate-stage", "stage ids must be unique")
        object.__setattr__(self, "stages", stages)
        object.__setattr__(
            self,
            "group_label",
            _validate_non_empty_text(self.group_label, "group label", max_len=48),
        )
        if not isinstance(self.legend, Mapping) or not self.legend:
            raise ViewValidationError("invalid-legend", "descriptor requires a non-empty legend")
        validated_legend: dict[str, LegendEntry] = {}
        for key, entry in self.legend.items():
            safe_key = validate_safe_label(key, "legend kind")
            if not isinstance(entry, LegendEntry):
                raise ViewValidationError("invalid-legend", "legend values must be LegendEntry instances")
            validated_legend[safe_key] = entry
        object.__setattr__(self, "legend", MappingProxyType(validated_legend))
        if self.metric is not None and not isinstance(self.metric, MetricDescriptor):
            raise ViewValidationError("invalid-metric", "metric must be a MetricDescriptor")
        if self.view not in ALLOWED_VIEW_MODES:
            raise ViewValidationError("invalid-view-mode", "view mode must be '3d' or 'grid'")
        object.__setattr__(self, "tool", validate_safe_label(self.tool, "tool"))
        object.__setattr__(self, "tool_version", validate_safe_label(self.tool_version, "tool version"))
        if not isinstance(self.sensitivity, str) or self.sensitivity.lower() not in SENSITIVITY_LEVELS:
            raise ViewValidationError("invalid-sensitivity", "unsupported sensitivity level")
        object.__setattr__(self, "sensitivity", self.sensitivity.lower())
        presets = tuple(self.sql_presets)
        if any(not isinstance(preset, SqlPreset) for preset in presets):
            raise ViewValidationError("invalid-sql-preset", "sql_presets must contain SqlPreset instances")
        preset_ids = [preset.id for preset in presets]
        if len(set(preset_ids)) != len(preset_ids):
            raise ViewValidationError("duplicate-sql-preset", "sql preset ids must be unique")
        object.__setattr__(self, "sql_presets", presets)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "title": self.title,
            "stages": [stage.to_dict() for stage in self.stages],
            "group_label": self.group_label,
            "legend": {key: entry.to_dict() for key, entry in self.legend.items()},
            "view": self.view,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "sensitivity": self.sensitivity,
            "sql_presets": [preset.to_dict() for preset in self.sql_presets],
        }
        if self.metric is not None:
            result["metric"] = self.metric.to_dict()
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ViewDescriptor:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-descriptor", "descriptor must be an object")
        raw_stages = raw.get("stages")
        if not isinstance(raw_stages, list):
            raise ViewValidationError("invalid-descriptor", "stages must be a list")
        raw_legend = raw.get("legend")
        if not isinstance(raw_legend, Mapping):
            raise ViewValidationError("invalid-legend", "legend must be an object")
        raw_metric = raw.get("metric")
        raw_presets = raw.get("sql_presets", [])
        if not isinstance(raw_presets, list):
            raise ViewValidationError("invalid-sql-preset", "sql_presets must be a list")
        return cls(
            title=raw.get("title"),  # type: ignore[arg-type]
            stages=tuple(StageDescriptor.from_dict(s) for s in raw_stages),
            group_label=raw.get("group_label"),  # type: ignore[arg-type]
            legend={str(k): LegendEntry.from_dict(v) for k, v in raw_legend.items()},
            metric=MetricDescriptor.from_dict(raw_metric) if raw_metric is not None else None,
            view=str(raw.get("view", "3d")),
            tool=str(raw.get("tool", "josh-room")),
            tool_version=str(raw.get("tool_version", "0.1.0")),
            sensitivity=str(raw.get("sensitivity", "synthetic")),
            sql_presets=tuple(SqlPreset.from_dict(p) for p in raw_presets),
        )


@dataclass(frozen=True, slots=True)
class StaleStatus:
    reason: str
    last_ok: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", validate_safe_label(self.reason, "stale reason"))
        if self.last_ok is not None:
            object.__setattr__(self, "last_ok", validate_timestamp(self.last_ok, "stale last_ok"))

    def to_dict(self) -> dict[str, object]:
        return {"reason": self.reason, "last_ok": self.last_ok}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> StaleStatus:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-stale", "stale must be an object")
        return cls(
            reason=raw.get("reason"),  # type: ignore[arg-type]
            last_ok=raw.get("last_ok"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class GroupState:
    id: str
    name: str
    order: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_safe_label(self.id, "group id"))
        object.__setattr__(self, "name", _validate_non_empty_text(self.name, "group name", max_len=64))
        if self.order is not None and (type(self.order) is not int or self.order < 0):
            raise ViewValidationError("invalid-group", "group order must be a non-negative integer")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"id": self.id, "name": self.name}
        if self.order is not None:
            result["order"] = self.order
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> GroupState:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-group", "group must be an object")
        return cls(
            id=raw.get("id"),  # type: ignore[arg-type]
            name=raw.get("name"),  # type: ignore[arg-type]
            order=raw.get("order"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    name: str
    media_type: str
    size: int
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_attachment_name(self.name))
        object.__setattr__(self, "media_type", validate_media_type(self.media_type))
        if type(self.size) is not int or self.size < 0:
            raise ViewValidationError("invalid-attachment-size", "attachment size must be a non-negative integer")
        object.__setattr__(self, "digest", validate_sha256(self.digest, "attachment digest"))

    @property
    def embeddable(self) -> bool:
        return self.media_type in ALLOWED_EMBED_MEDIA_TYPES

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "digest": self.digest,
            "embeddable": self.embeddable,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> AttachmentRef:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-attachment", "attachment ref must be an object")
        return cls(
            name=raw.get("name"),  # type: ignore[arg-type]
            media_type=raw.get("media_type"),  # type: ignore[arg-type]
            size=raw.get("size"),  # type: ignore[arg-type]
            digest=raw.get("digest"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PreviewContent:
    """Bounded, sanitized read-only preview slot attached to an inspectable object."""

    slot: str
    media_type: str
    title: str
    status: str = "available"
    body: str | None = None
    reason: str | None = None
    language: str | None = None
    truncated: bool = False
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slot", validate_safe_label(self.slot, "preview slot"))
        mt = validate_media_type(self.media_type)
        if mt not in ALLOWED_PREVIEW_MEDIA_TYPES and self.status == "available":
            raise ViewValidationError("disallowed-preview-media-type", f"unsupported preview media type {mt}")
        object.__setattr__(self, "media_type", mt)
        object.__setattr__(self, "title", _validate_non_empty_text(self.title, "preview title", max_len=96))
        if self.status not in ALLOWED_PREVIEW_STATUSES:
            raise ViewValidationError("invalid-preview-status", "invalid preview status")
        if self.status == "available":
            if self.body is None:
                raise ViewValidationError("invalid-preview-body", "available preview requires body text")
            object.__setattr__(
                self,
                "body",
                _validate_multiline_text(self.body, f"preview.{self.slot}.body", max_len=MAX_PREVIEW_BODY_CHARS),
            )
        else:
            if self.body is not None:
                raise ViewValidationError(
                    "invalid-preview-body",
                    "unavailable/redacted/omitted preview must not carry body text",
                )
            if not self.reason:
                raise ViewValidationError("invalid-preview-reason", "unavailable preview requires a reason")
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                _validate_non_empty_text(self.reason, "preview reason", max_len=160),
            )
        if self.language is not None:
            object.__setattr__(self, "language", validate_safe_label(self.language, "preview language"))
        if type(self.truncated) is not bool:
            raise ViewValidationError("invalid-preview", "preview truncated must be boolean")
        if self.summary is not None:
            object.__setattr__(
                self,
                "summary",
                _validate_non_empty_text(self.summary, "preview summary", max_len=160),
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "slot": self.slot,
            "media_type": self.media_type,
            "title": self.title,
            "status": self.status,
            "truncated": self.truncated,
        }
        if self.body is not None:
            result["body"] = self.body
        if self.reason is not None:
            result["reason"] = self.reason
        if self.language is not None:
            result["language"] = self.language
        if self.summary is not None:
            result["summary"] = self.summary
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> PreviewContent:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-preview", "preview must be an object")
        return cls(
            slot=raw.get("slot"),  # type: ignore[arg-type]
            media_type=raw.get("media_type"),  # type: ignore[arg-type]
            title=raw.get("title"),  # type: ignore[arg-type]
            status=str(raw.get("status", "available")),
            body=raw.get("body"),  # type: ignore[arg-type]
            reason=raw.get("reason"),  # type: ignore[arg-type]
            language=raw.get("language"),  # type: ignore[arg-type]
            truncated=bool(raw.get("truncated", False)),
            summary=raw.get("summary"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ObjectLink:
    """Explicit directional relationship tracing producer -> output -> consumer."""

    relation: str
    target_id: str
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.relation, str) or self.relation not in ALLOWED_LINK_RELATIONS:
            raise ViewValidationError("invalid-link-relation", "unsupported link relation")
        object.__setattr__(self, "target_id", validate_safe_label(self.target_id, "link target_id"))
        if self.label:
            object.__setattr__(self, "label", _validate_non_empty_text(self.label, "link label", max_len=96))

    def to_dict(self) -> dict[str, str]:
        result = {"relation": self.relation, "target_id": self.target_id}
        if self.label:
            result["label"] = self.label
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ObjectLink:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-link", "link must be an object")
        return cls(
            relation=raw.get("relation"),  # type: ignore[arg-type]
            target_id=raw.get("target_id"),  # type: ignore[arg-type]
            label=str(raw.get("label", "")),
        )


@dataclass(frozen=True, slots=True)
class NodeState:
    """Arbitrary-depth provider-defined hierarchy node for NAVIGATE & INSPECT."""

    id: str
    parent_id: str | None
    node_kind: str
    label: str
    updated_at: str
    kind: str | None = None
    metric: float | None = None
    summary: str | None = None
    order: int | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    previews: tuple[PreviewContent, ...] = ()
    links: tuple[ObjectLink, ...] = ()
    fields: Mapping[str, str | int | float | bool | None] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_safe_label(self.id, "node id"))
        if self.parent_id is not None:
            object.__setattr__(self, "parent_id", validate_safe_label(self.parent_id, "node parent_id"))
        if self.node_kind not in ALLOWED_NODE_KINDS:
            raise ViewValidationError("invalid-node-kind", "node_kind must be directory, file, run, or artifact")
        object.__setattr__(self, "label", _validate_non_empty_text(self.label, "node label", max_len=96))
        object.__setattr__(self, "updated_at", validate_timestamp(self.updated_at, "node updated_at"))
        if self.kind is not None:
            object.__setattr__(self, "kind", validate_safe_label(self.kind, "node kind"))
        elif self.node_kind != "directory":
            raise ViewValidationError("invalid-node", "leaf nodes must declare a legend kind")
        if self.metric is not None:
            if isinstance(self.metric, bool) or not isinstance(self.metric, (int, float)) or not math.isfinite(float(self.metric)):
                raise ViewValidationError("invalid-metric", "node metric must be finite")
            object.__setattr__(self, "metric", float(self.metric))
        if self.summary is not None:
            object.__setattr__(self, "summary", _validate_non_empty_text(self.summary, "node summary", max_len=160))
        if self.order is not None and (type(self.order) is not int or self.order < 0):
            raise ViewValidationError("invalid-node", "node order must be a non-negative integer")
        attachments = tuple(self.attachments)
        if any(not isinstance(att, AttachmentRef) for att in attachments):
            raise ViewValidationError("invalid-attachment", "attachments must contain AttachmentRef instances")
        if len({att.name for att in attachments}) != len(attachments):
            raise ViewValidationError("duplicate-attachment", "attachment names per node must be unique")
        object.__setattr__(self, "attachments", attachments)
        previews = tuple(self.previews)
        if any(not isinstance(prev, PreviewContent) for prev in previews):
            raise ViewValidationError("invalid-preview", "previews must contain PreviewContent instances")
        if len({prev.slot for prev in previews}) != len(previews):
            raise ViewValidationError("duplicate-preview", "preview slots per node must be unique")
        object.__setattr__(self, "previews", previews)
        links = tuple(self.links)
        if any(not isinstance(link, ObjectLink) for link in links):
            raise ViewValidationError("invalid-link", "links must contain ObjectLink instances")
        if len({(link.relation, link.target_id) for link in links}) != len(links):
            raise ViewValidationError("duplicate-link", "duplicate (relation, target_id) link on node")
        object.__setattr__(self, "links", links)
        object.__setattr__(self, "fields", _validate_scalar_mapping(self.fields, "node fields"))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "parent_id": self.parent_id,
            "node_kind": self.node_kind,
            "label": self.label,
            "updated_at": self.updated_at,
            "attachments": [att.to_dict() for att in self.attachments],
            "previews": [prev.to_dict() for prev in self.previews],
            "links": [link.to_dict() for link in self.links],
            "fields": dict(self.fields),
        }
        if self.kind is not None:
            result["kind"] = self.kind
        if self.metric is not None:
            result["metric"] = self.metric
        if self.summary is not None:
            result["summary"] = self.summary
        if self.order is not None:
            result["order"] = self.order
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> NodeState:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-node", "node must be an object")
        raw_atts = raw.get("attachments", [])
        raw_previews = raw.get("previews", [])
        raw_links = raw.get("links", [])
        if not isinstance(raw_atts, list) or not isinstance(raw_previews, list) or not isinstance(raw_links, list):
            raise ViewValidationError("invalid-node", "node attachments, previews, and links must be lists")
        return cls(
            id=raw.get("id"),  # type: ignore[arg-type]
            parent_id=raw.get("parent_id"),  # type: ignore[arg-type]
            node_kind=raw.get("node_kind"),  # type: ignore[arg-type]
            label=raw.get("label"),  # type: ignore[arg-type]
            updated_at=raw.get("updated_at"),  # type: ignore[arg-type]
            kind=raw.get("kind"),  # type: ignore[arg-type]
            metric=raw.get("metric"),  # type: ignore[arg-type]
            summary=raw.get("summary"),  # type: ignore[arg-type]
            order=raw.get("order"),  # type: ignore[arg-type]
            attachments=tuple(AttachmentRef.from_dict(att) for att in raw_atts),
            previews=tuple(PreviewContent.from_dict(p) for p in raw_previews),
            links=tuple(ObjectLink.from_dict(lk) for lk in raw_links),
            fields=raw.get("fields", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ItemState:
    id: str
    group: str
    stage: str
    kind: str
    label: str
    updated_at: str
    metric: float | None = None
    attachments: tuple[AttachmentRef, ...] = ()
    previews: tuple[PreviewContent, ...] = ()
    links: tuple[ObjectLink, ...] = ()
    fields: Mapping[str, str | int | float | bool | None] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_safe_label(self.id, "item id"))
        object.__setattr__(self, "group", validate_safe_label(self.group, "item group"))
        object.__setattr__(self, "stage", validate_safe_label(self.stage, "item stage"))
        object.__setattr__(self, "kind", validate_safe_label(self.kind, "item kind"))
        object.__setattr__(self, "label", _validate_non_empty_text(self.label, "item label", max_len=160))
        object.__setattr__(self, "updated_at", validate_timestamp(self.updated_at, "item updated_at"))
        if self.metric is not None:
            if isinstance(self.metric, bool) or not isinstance(self.metric, (int, float)) or not math.isfinite(float(self.metric)):
                raise ViewValidationError("invalid-metric", "item metric must be finite")
            object.__setattr__(self, "metric", float(self.metric))
        attachments = tuple(self.attachments)
        if any(not isinstance(att, AttachmentRef) for att in attachments):
            raise ViewValidationError("invalid-attachment", "attachments must contain AttachmentRef instances")
        att_names = [att.name for att in attachments]
        if len(set(att_names)) != len(att_names):
            raise ViewValidationError("duplicate-attachment", "attachment names per item must be unique")
        object.__setattr__(self, "attachments", attachments)
        previews = tuple(self.previews)
        if any(not isinstance(prev, PreviewContent) for prev in previews):
            raise ViewValidationError("invalid-preview", "previews must contain PreviewContent instances")
        if len({prev.slot for prev in previews}) != len(previews):
            raise ViewValidationError("duplicate-preview", "preview slots per item must be unique")
        object.__setattr__(self, "previews", previews)
        links = tuple(self.links)
        if any(not isinstance(link, ObjectLink) for link in links):
            raise ViewValidationError("invalid-link", "links must contain ObjectLink instances")
        if len({(link.relation, link.target_id) for link in links}) != len(links):
            raise ViewValidationError("duplicate-link", "duplicate (relation, target_id) link on item")
        object.__setattr__(self, "links", links)
        object.__setattr__(self, "fields", _validate_scalar_mapping(self.fields, "item fields"))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "id": self.id,
            "group": self.group,
            "stage": self.stage,
            "kind": self.kind,
            "label": self.label,
            "updated_at": self.updated_at,
            "attachments": [att.to_dict() for att in self.attachments],
            "fields": dict(self.fields),
        }
        if self.metric is not None:
            result["metric"] = self.metric
        if self.previews:
            result["previews"] = [prev.to_dict() for prev in self.previews]
        if self.links:
            result["links"] = [link.to_dict() for link in self.links]
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ItemState:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-item", "item must be an object")
        raw_attachments = raw.get("attachments", [])
        raw_previews = raw.get("previews", [])
        raw_links = raw.get("links", [])
        if not isinstance(raw_attachments, list) or not isinstance(raw_previews, list) or not isinstance(raw_links, list):
            raise ViewValidationError("invalid-attachment", "attachments, previews, and links must be lists")
        return cls(
            id=raw.get("id"),  # type: ignore[arg-type]
            group=raw.get("group"),  # type: ignore[arg-type]
            stage=raw.get("stage"),  # type: ignore[arg-type]
            kind=raw.get("kind"),  # type: ignore[arg-type]
            label=raw.get("label"),  # type: ignore[arg-type]
            updated_at=raw.get("updated_at"),  # type: ignore[arg-type]
            metric=raw.get("metric"),  # type: ignore[arg-type]
            attachments=tuple(AttachmentRef.from_dict(att) for att in raw_attachments),
            previews=tuple(PreviewContent.from_dict(p) for p in raw_previews),
            links=tuple(ObjectLink.from_dict(lk) for lk in raw_links),
            fields=raw.get("fields", {}),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class StepState:
    item_id: str
    name: str
    at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", validate_safe_label(self.item_id, "step item_id"))
        object.__setattr__(self, "name", _validate_non_empty_text(self.name, "step name", max_len=64))
        object.__setattr__(self, "at", validate_timestamp(self.at, "step at"))

    def to_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "name": self.name, "at": self.at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> StepState:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-step", "step must be an object")
        return cls(
            item_id=raw.get("item_id"),  # type: ignore[arg-type]
            name=raw.get("name"),  # type: ignore[arg-type]
            at=raw.get("at"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class ViewState:
    taken_at: str
    since: str
    groups: tuple[GroupState, ...]
    items: tuple[ItemState, ...]
    steps: tuple[StepState, ...] = ()
    summary: Mapping[str, str | int | float | bool | None] = field(default_factory=lambda: MappingProxyType({}))
    stale: StaleStatus | None = None
    nodes: tuple[NodeState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "taken_at", validate_timestamp(self.taken_at, "state taken_at"))
        object.__setattr__(self, "since", validate_timestamp(self.since, "state since"))
        groups = tuple(self.groups)
        if any(not isinstance(group, GroupState) for group in groups):
            raise ViewValidationError("invalid-group", "groups must contain GroupState instances")
        group_ids = [group.id for group in groups]
        if len(set(group_ids)) != len(group_ids):
            raise ViewValidationError("duplicate-group", "group ids must be unique")
        object.__setattr__(self, "groups", groups)

        items = tuple(self.items)
        if any(not isinstance(item, ItemState) for item in items):
            raise ViewValidationError("invalid-item", "items must contain ItemState instances")
        item_ids = [item.id for item in items]
        if len(set(item_ids)) != len(item_ids):
            raise ViewValidationError("duplicate-item", "item ids must be unique")
        object.__setattr__(self, "items", items)

        nodes = tuple(self.nodes)
        if any(not isinstance(node, NodeState) for node in nodes):
            raise ViewValidationError("invalid-node", "nodes must contain NodeState instances")
        node_ids = [node.id for node in nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ViewValidationError("duplicate-node", "node ids must be unique")
        object.__setattr__(self, "nodes", nodes)

        steps = tuple(self.steps)
        if any(not isinstance(step, StepState) for step in steps):
            raise ViewValidationError("invalid-step", "steps must contain StepState instances")
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "summary", _validate_scalar_mapping(self.summary, "summary"))
        if self.stale is not None and not isinstance(self.stale, StaleStatus):
            raise ViewValidationError("invalid-stale", "stale must be a StaleStatus")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "taken_at": self.taken_at,
            "since": self.since,
            "groups": [group.to_dict() for group in self.groups],
            "items": [item.to_dict() for item in self.items],
            "steps": [step.to_dict() for step in self.steps],
            "summary": dict(self.summary),
        }
        if self.nodes:
            result["nodes"] = [node.to_dict() for node in self.nodes]
        if self.stale is not None:
            result["stale"] = self.stale.to_dict()
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ViewState:
        if not isinstance(raw, Mapping):
            raise ViewValidationError("invalid-state", "state must be an object")
        raw_groups = raw.get("groups", [])
        raw_items = raw.get("items", [])
        raw_steps = raw.get("steps", [])
        raw_nodes = raw.get("nodes", [])
        if (
            not isinstance(raw_groups, list)
            or not isinstance(raw_items, list)
            or not isinstance(raw_steps, list)
            or not isinstance(raw_nodes, list)
        ):
            raise ViewValidationError("invalid-state", "groups, items, steps, and nodes must be lists")
        raw_stale = raw.get("stale")
        return cls(
            taken_at=raw.get("taken_at"),  # type: ignore[arg-type]
            since=raw.get("since"),  # type: ignore[arg-type]
            groups=tuple(GroupState.from_dict(g) for g in raw_groups),
            items=tuple(ItemState.from_dict(i) for i in raw_items),
            steps=tuple(StepState.from_dict(s) for s in raw_steps),
            summary=raw.get("summary", {}),  # type: ignore[arg-type]
            stale=StaleStatus.from_dict(raw_stale) if raw_stale is not None else None,
            nodes=tuple(NodeState.from_dict(n) for n in raw_nodes),
        )


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    media_type: str
    data: bytes
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_attachment_name(self.name))
        object.__setattr__(self, "media_type", validate_media_type(self.media_type))
        if not isinstance(self.data, bytes):
            raise ViewValidationError("invalid-attachment-bytes", "attachment data must be bytes")
        actual = hashlib.sha256(self.data).hexdigest()
        if not self.sha256:
            object.__setattr__(self, "sha256", actual)
        else:
            expected = validate_sha256(self.sha256, "attachment sha256")
            if expected != actual:
                raise ViewValidationError("digest-mismatch", "attachment sha256 does not match payload bytes")

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def embeddable(self) -> bool:
        return self.media_type in ALLOWED_EMBED_MEDIA_TYPES


@runtime_checkable
class ViewStateProvider(Protocol):
    """Closed, transport-neutral provider protocol for Room View."""

    def describe(self) -> ViewDescriptor: ...

    def state(self) -> ViewState: ...

    def attachment(self, item_id: str, name: str) -> Attachment | None: ...


def compute_node_paths(nodes: tuple[NodeState, ...]) -> dict[str, str]:
    """Compute synthetic UNIX display paths ('/', '/src', '/src/core/scheduler.py') from parent links."""
    by_id = {node.id: node for node in nodes}
    cache: dict[str, str] = {}

    def resolve(node_id: str) -> str:
        if node_id in cache:
            return cache[node_id]
        node = by_id[node_id]
        if node.parent_id is None:
            path = "/"
        else:
            parent_path = resolve(node.parent_id)
            seg = node.label.strip("/")
            path = f"/{seg}" if parent_path == "/" else f"{parent_path}/{seg}"
        cache[node_id] = path
        return path

    for node in nodes:
        resolve(node.id)
    return cache


def validate_state_against_descriptor(descriptor: ViewDescriptor, state: ViewState) -> None:
    """Verify referential integrity between descriptor and state."""
    if not isinstance(descriptor, ViewDescriptor):
        raise ViewValidationError("invalid-descriptor", "descriptor must be a ViewDescriptor")
    if not isinstance(state, ViewState):
        raise ViewValidationError("invalid-state", "state must be a ViewState")
    group_ids = {group.id for group in state.groups}
    stage_ids = {stage.id for stage in descriptor.stages}
    legend_kinds = set(descriptor.legend.keys())
    item_ids = {item.id for item in state.items}
    node_by_id = {node.id: node for node in state.nodes}
    known_object_ids = item_ids | set(node_by_id.keys())

    for item in state.items:
        if item.group not in group_ids:
            raise ViewValidationError("unknown-group", f"item references unknown group {item.group}")
        if item.stage not in stage_ids:
            raise ViewValidationError("unknown-stage", f"item references unknown stage {item.stage}")
        if item.kind not in legend_kinds:
            raise ViewValidationError("unknown-kind", f"item references unknown legend kind {item.kind}")
        for link in item.links:
            if link.target_id not in known_object_ids:
                raise ViewValidationError("unknown-link-target", f"item link targets unknown object {link.target_id}")

    if state.nodes:
        roots = [node for node in state.nodes if node.parent_id is None]
        if len(roots) != 1 or roots[0].node_kind != "directory":
            raise ViewValidationError("invalid-tree-root", "nodes hierarchy must have exactly one root directory")
        for node in state.nodes:
            if node.parent_id is not None:
                parent = node_by_id.get(node.parent_id)
                if parent is None:
                    raise ViewValidationError("unknown-parent", f"node references unknown parent {node.parent_id}")
                if parent.node_kind != "directory":
                    raise ViewValidationError("non-directory-parent", f"node parent {node.parent_id} must be a directory")
            if node.kind is not None and node.kind not in legend_kinds:
                raise ViewValidationError("unknown-kind", f"node references unknown legend kind {node.kind}")
            # Cycle detection
            seen: set[str] = {node.id}
            cur = node.parent_id
            while cur is not None:
                if cur in seen:
                    raise ViewValidationError("tree-cycle", f"cycle detected at node {node.id}")
                seen.add(cur)
                cur = node_by_id[cur].parent_id
            for link in node.links:
                if link.target_id not in known_object_ids:
                    raise ViewValidationError("unknown-link-target", f"node link targets unknown object {link.target_id}")

    for step in state.steps:
        if step.item_id not in known_object_ids:
            raise ViewValidationError("unknown-step-item", f"step references unknown item {step.item_id}")


def _instantiate_provider_target(target: object) -> ViewStateProvider:
    """Instantiate a class or factory target, or return an already-constructed provider instance."""
    if inspect.isclass(target) or (callable(target) and not isinstance(target, ViewStateProvider)):
        provider = target()
    else:
        provider = target
    if inspect.isclass(provider) or not isinstance(provider, ViewStateProvider):
        raise ViewValidationError("invalid-provider", "loaded object does not implement ViewStateProvider")
    return provider


def load_provider(spec: str) -> ViewStateProvider:
    """Load a ViewStateProvider by builtin name, entry point, or module:attr."""
    if not isinstance(spec, str) or not spec.strip():
        raise ViewValidationError("invalid-provider", "provider specification is required")
    spec = spec.strip()

    from .synthetic_provider import SyntheticPipelineProvider, SyntheticUnixFsProvider

    builtins = {
        "synthetic": lambda: SyntheticPipelineProvider(preset="default"),
        "synthetic-small": lambda: SyntheticPipelineProvider(preset="small"),
        "synthetic-large": lambda: SyntheticPipelineProvider(preset="large"),
        "synthetic-sensitive": lambda: SyntheticPipelineProvider(preset="sensitive"),
        "synthetic-unix": lambda: SyntheticUnixFsProvider(),
    }
    if spec in builtins:
        return builtins[spec]()

    if ":" not in spec:
        eps = importlib.metadata.entry_points(group="josh_room.view_providers")
        for ep in eps:
            if ep.name == spec:
                return _instantiate_provider_target(ep.load())
        raise ViewValidationError("unknown-provider", f"unknown view provider {spec!r}")

    module_name, attr_name = spec.split(":", 1)
    if not module_name or not attr_name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module_name):
        raise ViewValidationError("invalid-provider", "invalid module:attr provider specification")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", attr_name):
        raise ViewValidationError("invalid-provider", "invalid provider attribute name")
    try:
        module = importlib.import_module(module_name)
        target = getattr(module, attr_name)
    except (ImportError, AttributeError) as error:
        raise ViewValidationError("provider-load-failed", f"could not load provider {spec!r}") from error
    return _instantiate_provider_target(target)
