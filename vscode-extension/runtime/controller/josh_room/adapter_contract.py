"""Closed, transport-neutral logical source adapter contracts.

This module deliberately contains no filesystem, network, secret, or policy
authority implementation.  Callers provide the two gates required by
``LogicalSourceAdapter.plan`` and own every destination/material decision.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol

from ._stream_state import bind as _bind_stream_state
from ._stream_state import get as _get_stream_state

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AdapterError(AdapterErrorCode.INVALID_REQUEST, f"invalid {field_name}")
    return value


def _label(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise AdapterError(AdapterErrorCode.INVALID_REQUEST, f"invalid {field_name}")
    return value


def _digest(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, f"invalid {field_name}")
    return value


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_object(body: str, code: AdapterErrorCode) -> dict[str, object]:
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise AdapterError(code, "invalid JSON") from None
    if not isinstance(value, dict):
        raise AdapterError(code, "JSON object required")
    return value


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    LOCAL_ONLY = "local-only"
    QUARANTINE = "quarantine"


class AdapterErrorCode(str, Enum):
    CANCELLED = "cancelled"
    GATES_REQUIRED = "gates-required"
    INVALID_CHECKPOINT = "invalid-checkpoint"
    INVALID_REQUEST = "invalid-request"
    INVALID_RESULT = "invalid-result"
    RECORD_OVERSIZE = "record-oversize"
    SOURCE_CURSOR_INCONSISTENT = "source-cursor-inconsistent"
    SOURCE_PREFIX_CHANGED = "source-prefix-changed"
    SOURCE_REPRESENTATION_CHANGED = "source-representation-changed"
    SOURCE_REPLACED = "source-replaced"
    SOURCE_TRUNCATED = "source-truncated"
    STREAM_LIMIT = "stream-limit"
    UNKNOWN_REPRESENTATION = "unknown-representation"
    UNKNOWN_SOURCE = "unknown-source"


class StreamStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class ResolveStatus(str, Enum):
    FOUND = "found"
    MOVED = "moved"
    CONFLICT = "conflict"
    QUARANTINE = "quarantine"
    NOT_FOUND = "not-found"


class PlanStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    QUARANTINE = "quarantine"


class AdapterError(Exception):
    """Typed public-safe error; it intentionally carries no source record."""

    def __init__(self, code: AdapterErrorCode, message: str | None = None):
        try:
            self.code = AdapterErrorCode(code)
        except (TypeError, ValueError):
            self.code = AdapterErrorCode.INVALID_REQUEST
        # Public errors expose only a stable code-derived message.  Detail
        # strings may contain paths, URLs, credentials, or records.
        self.message = self.code.value.replace("-", " ")
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"


class LogicalSourceName(str, Enum):
    TRANSCRIPT = "codex.transcript"
    SESSION_METADATA = "codex.session-metadata"

    @classmethod
    def parse(cls, value: str) -> LogicalSourceName:
        try:
            return cls(value)
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "unknown logical source") from None


@dataclass(frozen=True, slots=True)
class GateDecision:
    decision: Decision
    reason_codes: tuple[str, ...]
    authority_ref: str

    def __post_init__(self) -> None:
        try:
            decision = Decision(self.decision)
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "invalid gate decision") from None
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "reason_codes", tuple(_label(code, "reason code") for code in self.reason_codes))
        object.__setattr__(self, "authority_ref", _label(self.authority_ref, "authority reference"))

    def to_dict(self) -> dict[str, object]:
        return {
            "authority_ref": self.authority_ref,
            "decision": self.decision.value,
            "reason_codes": list(self.reason_codes),
        }


class PolicyGate(Protocol):
    def evaluate(self, event: SourceEvent, declaration: AdapterDeclaration) -> GateDecision: ...


class MaterialGate(Protocol):
    def evaluate(self, event: SourceEvent, declaration: AdapterDeclaration) -> GateDecision: ...


@dataclass(frozen=True, slots=True)
class GateSet:
    policy: PolicyGate
    material: MaterialGate

    def __post_init__(self) -> None:
        if (
            self.policy is None
            or self.material is None
            or not callable(getattr(self.policy, "evaluate", None))
            or not callable(getattr(self.material, "evaluate", None))
        ):
            raise AdapterError(AdapterErrorCode.GATES_REQUIRED, "policy and material gates are required")


class LogicalSourceAdapter(Protocol):
    declaration: AdapterDeclaration

    def probe(self, request: ProbeRequest) -> ProbeResult: ...

    def plan(
        self,
        event: SourceEvent,
        gates: GateSet,
        prior_checkpoint: Checkpoint | None = None,
    ) -> Plan: ...

    def open(self, plan: Plan, *, cancellation: CancellationToken | None = None) -> BoundedRecordStream: ...

    def checkpoint(self, result: StreamResult) -> Checkpoint: ...

    def resolve(
        self,
        session_id: str,
        prior_checkpoint: Checkpoint | None,
        logical_source: LogicalSourceName | None = None,
    ) -> ResolveResult: ...

    def inspect(self) -> AdapterInspection: ...


@dataclass(frozen=True, slots=True)
class StreamLimits:
    max_record_bytes: int
    max_asset_bytes: int
    max_open_records: int
    max_open_bytes: int

    def __post_init__(self) -> None:
        if any(not _positive_int(value) for value in (
            self.max_record_bytes,
            self.max_asset_bytes,
            self.max_open_records,
            self.max_open_bytes,
        )):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "stream limits must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_asset_bytes": self.max_asset_bytes,
            "max_open_bytes": self.max_open_bytes,
            "max_open_records": self.max_open_records,
            "max_record_bytes": self.max_record_bytes,
        }


@dataclass(frozen=True, slots=True)
class AdapterDeclaration:
    name: str
    version: str
    logical_sources: tuple[LogicalSourceName, ...]
    approved_roots: tuple[str, ...]
    material_classes: tuple[str, ...]
    representations: tuple[str, ...]
    limits: StreamLimits
    approved_representation_transitions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _label(self.name, "adapter name"))
        object.__setattr__(self, "version", _label(self.version, "adapter version"))
        object.__setattr__(
            self,
            "logical_sources",
            tuple(LogicalSourceName.parse(source) for source in self.logical_sources),
        )
        object.__setattr__(self, "approved_roots", tuple(_label(root, "approved root") for root in self.approved_roots))
        object.__setattr__(self, "material_classes", tuple(_label(item, "material class") for item in self.material_classes))
        object.__setattr__(self, "representations", tuple(_label(item, "representation") for item in self.representations))
        try:
            transitions = tuple(
                (
                    _label(transition[0], "source representation"),
                    _label(transition[1], "target representation"),
                )
                for transition in self.approved_representation_transitions
                if isinstance(transition, (tuple, list)) and len(transition) == 2
            )
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "invalid representation transition") from None
        if len(transitions) != len(self.approved_representation_transitions):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "invalid representation transition")
        if len(set(transitions)) != len(transitions) or any(source == target for source, target in transitions):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "invalid representation transition")
        if any(
            source not in self.representations or target not in self.representations
            for source, target in transitions
        ):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "representation transition is not declared")
        object.__setattr__(self, "approved_representation_transitions", transitions)
        if not isinstance(self.limits, StreamLimits):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "stream limits are required")
        if not self.logical_sources or not self.approved_roots or not self.material_classes or not self.representations:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "adapter declaration is incomplete")

    def to_dict(self) -> dict[str, object]:
        return {
            "approved_roots": list(self.approved_roots),
            "limits": self.limits.to_dict(),
            "logical_sources": [source.value for source in self.logical_sources],
            "material_classes": list(self.material_classes),
            "name": self.name,
            "representations": list(self.representations),
            "approved_representation_transitions": [list(transition) for transition in self.approved_representation_transitions],
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    logical_source: LogicalSourceName
    session_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))


@dataclass(frozen=True, slots=True)
class SourceEvent:
    event_id: str
    logical_source: LogicalSourceName
    session_id: str
    approved_root: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event id"))
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        object.__setattr__(self, "approved_root", _label(self.approved_root, "approved root"))


@dataclass(frozen=True, slots=True)
class ProbeResult:
    logical_source: LogicalSourceName
    session_id: str
    source_exists: bool
    capabilities: tuple[str, ...]
    representations: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        if not isinstance(self.source_exists, bool):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "source existence must be boolean")
        object.__setattr__(self, "capabilities", tuple(_label(item, "capability") for item in self.capabilities))
        object.__setattr__(
            self,
            "representations",
            tuple(_label(item, "representation") for item in self.representations),
        )

    def to_json(self) -> str:
        return _canonical_json({
            "capabilities": list(self.capabilities),
            "logical_source": self.logical_source.value,
            "representations": list(self.representations),
            "session_id": self.session_id,
            "source_exists": self.source_exists,
        })


@dataclass(frozen=True, slots=True)
class Checkpoint:
    logical_source: LogicalSourceName
    session_id: str
    source_id: str
    representation: str
    next_record_index: int
    next_byte_offset: int
    observed_size: int
    prefix_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source id"))
        object.__setattr__(self, "representation", _label(self.representation, "representation"))
        if any(not _nonnegative_int(value) for value in (
            self.next_record_index,
            self.next_byte_offset,
            self.observed_size,
        )):
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "checkpoint offsets must be non-negative")
        object.__setattr__(self, "prefix_digest", _digest(self.prefix_digest, "prefix digest"))

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_source": self.logical_source.value,
            "next_byte_offset": self.next_byte_offset,
            "next_record_index": self.next_record_index,
            "observed_size": self.observed_size,
            "prefix_digest": self.prefix_digest,
            "representation": self.representation,
            "session_id": self.session_id,
            "source_id": self.source_id,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, body: str) -> Checkpoint:
        value = _parse_object(body, AdapterErrorCode.INVALID_CHECKPOINT)
        required = {
            "logical_source", "next_byte_offset", "next_record_index", "observed_size",
            "prefix_digest", "representation", "session_id", "source_id",
        }
        if set(value) != required:
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "checkpoint fields are not canonical")
        try:
            return cls(
                logical_source=LogicalSourceName.parse(value["logical_source"]),
                session_id=value["session_id"],
                source_id=value["source_id"],
                representation=value["representation"],
                next_record_index=value["next_record_index"],
                next_byte_offset=value["next_byte_offset"],
                observed_size=value["observed_size"],
                prefix_digest=value["prefix_digest"],
            )
        except AdapterError:
            raise
        except (KeyError, TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "invalid checkpoint values") from None


@dataclass(frozen=True, slots=True)
class Plan:
    adapter_name: str
    adapter_version: str
    event_id: str
    logical_source: LogicalSourceName
    session_id: str
    source_id: str
    representation: str
    approved_root: str
    estimated_records: int
    estimated_bytes: int
    checkpoint: Checkpoint
    limits: StreamLimits
    policy: GateDecision
    material: GateDecision
    status: PlanStatus = PlanStatus.READY
    transitioned_from: str | None = None
    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter_name", _label(self.adapter_name, "adapter name"))
        object.__setattr__(self, "adapter_version", _label(self.adapter_version, "adapter version"))
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event id"))
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source id"))
        object.__setattr__(self, "representation", _label(self.representation, "representation"))
        object.__setattr__(self, "approved_root", _label(self.approved_root, "approved root"))
        try:
            status = PlanStatus(self.status)
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "invalid plan status") from None
        object.__setattr__(self, "status", status)
        if not isinstance(self.checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan checkpoint required")
        if not isinstance(self.limits, StreamLimits):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan limits required")
        if not isinstance(self.policy, GateDecision) or not isinstance(self.material, GateDecision):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "plan gate decisions required")
        if any(not _nonnegative_int(value) for value in (self.estimated_records, self.estimated_bytes)):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan estimates must be non-negative")
        if self.transitioned_from is not None:
            object.__setattr__(self, "transitioned_from", _label(self.transitioned_from, "representation"))

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter": {"name": self.adapter_name, "version": self.adapter_version},
            "approved_root": self.approved_root,
            "checkpoint": self.checkpoint.to_dict(),
            "estimated_bytes": self.estimated_bytes,
            "estimated_records": self.estimated_records,
            "event_id": self.event_id,
            "limits": self.limits.to_dict(),
            "logical_source": self.logical_source.value,
            "material": self.material.to_dict(),
            "policy": self.policy.to_dict(),
            "representation": self.representation,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "status": self.status.value,
            "transitioned_from": self.transitioned_from,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class SourceRecord:
    logical_source: LogicalSourceName
    session_id: str
    record_index: int
    content: bytes
    material_class: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        object.__setattr__(self, "material_class", _label(self.material_class, "material class"))
        if not _nonnegative_int(self.record_index):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "record index must be non-negative")
        if not isinstance(self.content, bytes):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "record content must be bytes")

    @property
    def byte_size(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class StreamResult:
    status: StreamStatus
    records_emitted: int
    bytes_emitted: int
    next_checkpoint: Checkpoint

    def __post_init__(self) -> None:
        try:
            status = StreamStatus(self.status)
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "invalid stream status") from None
        object.__setattr__(self, "status", status)
        if any(not _nonnegative_int(value) for value in (self.records_emitted, self.bytes_emitted)):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream counts must be non-negative")
        if not isinstance(self.next_checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream checkpoint required")

    def to_dict(self) -> dict[str, object]:
        return {
            "bytes_emitted": self.bytes_emitted,
            "next_checkpoint": self.next_checkpoint.to_dict(),
            "records_emitted": self.records_emitted,
            "status": self.status.value,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


AdapterResult = StreamResult


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class _StreamBounds:
    max_record_bytes: int
    max_asset_bytes: int
    max_open_records: int
    max_open_bytes: int


@dataclass(slots=True)
class _StreamState:
    records: Iterator[SourceRecord]
    checkpoint_factory: object
    cancellation: CancellationToken
    bounds: _StreamBounds
    record_count: int
    byte_count: int
    done: bool
    status: StreamStatus
    result: StreamResult
    lock: object


class _WeakReferenceable:
    __slots__ = ("__weakref__",)


class BoundedRecordStream(_WeakReferenceable, Iterator[SourceRecord]):
    """A pull stream: one source record is requested per ``next`` call."""

    __slots__ = ()

    def __init__(
        self,
        records: Iterable[SourceRecord],
        checkpoint_factory,
        limits: StreamLimits,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if not isinstance(limits, StreamLimits):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "stream limits are required")
        copied_limits = StreamLimits(
            limits.max_record_bytes,
            limits.max_asset_bytes,
            limits.max_open_records,
            limits.max_open_bytes,
        )
        initial_result = StreamResult(StreamStatus.PARTIAL, 0, 0, checkpoint_factory(0, 0))
        state = _StreamState(
            records=iter(records),
            checkpoint_factory=checkpoint_factory,
            cancellation=cancellation or CancellationToken(),
            bounds=_StreamBounds(
                max_record_bytes=copied_limits.max_record_bytes,
                max_asset_bytes=copied_limits.max_asset_bytes,
                max_open_records=copied_limits.max_open_records,
                max_open_bytes=copied_limits.max_open_bytes,
            ),
            record_count=0,
            byte_count=0,
            done=False,
            status=StreamStatus.PARTIAL,
            result=initial_result,
            lock=threading.RLock(),
        )
        _bind_stream_state(self, state)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("bounded record stream is empty-slotted")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("bounded record stream is empty-slotted")

    def __iter__(self) -> BoundedRecordStream:
        return self

    def __next__(self) -> SourceRecord:
        state = _get_stream_state(self)
        if not isinstance(state, _StreamState):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream state is unavailable")
        with state.lock:
            def refresh_result() -> None:
                state.result = StreamResult(
                    state.status,
                    state.record_count,
                    state.byte_count,
                    state.checkpoint_factory(state.record_count, state.byte_count),
                )

            if state.done:
                raise StopIteration
            if state.cancellation.cancelled:
                state.status = StreamStatus.CANCELLED
                state.done = True
                refresh_result()
                raise AdapterError(AdapterErrorCode.CANCELLED, "stream cancelled")
            if state.record_count >= state.bounds.max_open_records:
                state.done = True
                refresh_result()
                raise AdapterError(AdapterErrorCode.STREAM_LIMIT, "record limit exceeded")
            try:
                record = next(state.records)
            except StopIteration:
                state.status = StreamStatus.COMPLETE
                state.done = True
                refresh_result()
                raise
            if not isinstance(record, SourceRecord):
                state.done = True
                raise AdapterError(AdapterErrorCode.INVALID_RESULT, "adapter emitted an invalid record")
            if record.material_class == "asset" and record.byte_size > state.bounds.max_asset_bytes:
                state.done = True
                raise AdapterError(AdapterErrorCode.RECORD_OVERSIZE, "asset exceeds declared limit")
            if record.byte_size > state.bounds.max_record_bytes:
                state.done = True
                raise AdapterError(AdapterErrorCode.RECORD_OVERSIZE, "record exceeds declared limit")
            if state.byte_count + record.byte_size > state.bounds.max_open_bytes:
                state.done = True
                refresh_result()
                raise AdapterError(AdapterErrorCode.STREAM_LIMIT, "byte limit exceeded")
            state.record_count += 1
            state.byte_count += record.byte_size
            refresh_result()
            return record

    @property
    def result(self) -> StreamResult:
        state = _get_stream_state(self)
        if not isinstance(state, _StreamState):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream state is unavailable")
        with state.lock:
            return state.result


@dataclass(frozen=True, slots=True)
class AdapterInspection:
    adapter: Mapping[str, str]
    logical_sources: tuple[str, ...]
    capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, Mapping) or set(self.adapter) != {"name", "version"}:
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "invalid adapter inspection")
        object.__setattr__(
            self,
            "adapter",
            MappingProxyType({
                "name": _label(self.adapter["name"], "adapter name"),
                "version": _label(self.adapter["version"], "adapter version"),
            }),
        )
        object.__setattr__(
            self,
            "logical_sources",
            tuple(LogicalSourceName.parse(source).value for source in self.logical_sources),
        )
        object.__setattr__(self, "capabilities", tuple(_label(item, "capability") for item in self.capabilities))

    def to_json(self) -> str:
        if not isinstance(self.adapter, Mapping) or set(self.adapter) != {"name", "version"}:
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "invalid adapter inspection")
        return _canonical_json({
            "adapter": {
                "name": _label(self.adapter["name"], "adapter name"),
                "version": _label(self.adapter["version"], "adapter version"),
            },
            "capabilities": list(self.capabilities),
            "logical_sources": list(self.logical_sources),
        })


@dataclass(frozen=True, slots=True)
class ResolveResult:
    status: ResolveStatus
    session_id: str
    representation: str | None = None
    checkpoint: Checkpoint | None = None

    def __post_init__(self) -> None:
        try:
            status = ResolveStatus(self.status)
        except (TypeError, ValueError):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "invalid resolve status") from None
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        if self.representation is not None:
            object.__setattr__(self, "representation", _label(self.representation, "representation"))
        if self.checkpoint is not None and not isinstance(self.checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "resolve checkpoint required")

    def to_json(self) -> str:
        body: dict[str, object] = {"session_id": self.session_id, "status": self.status.value}
        if self.representation is not None:
            body["representation"] = self.representation
        if self.checkpoint is not None:
            body["checkpoint"] = self.checkpoint.to_dict()
        return _canonical_json(body)
