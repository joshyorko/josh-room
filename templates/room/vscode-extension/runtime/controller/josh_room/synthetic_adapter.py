"""Deterministic in-memory adapter used by the #5 contract tests and fixtures."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .adapter_contract import (
    AdapterDeclaration,
    AdapterError,
    AdapterErrorCode,
    AdapterInspection,
    BoundedRecordStream,
    Checkpoint,
    Decision,
    GateDecision,
    GateSet,
    LogicalSourceName,
    Plan,
    ProbeRequest,
    ProbeResult,
    ResolveResult,
    ResolveStatus,
    SourceEvent,
    SourceRecord,
    StreamLimits,
    StreamResult,
    _identifier,
    _label,
)

SUPPORTED_REPRESENTATIONS = ("active-jsonl", "archived-jsonl", "compressed-jsonl-zst")
APPROVED_REPRESENTATION_TRANSITIONS = (
    ("active-jsonl", "archived-jsonl"),
    ("archived-jsonl", "compressed-jsonl-zst"),
)
REPRESENTATION_PRIORITY = {name: index for index, name in enumerate(SUPPORTED_REPRESENTATIONS)}
_PLAN_ISSUANCE = MappingProxyType({})
_PLAN_ISSUANCE_LOCK = threading.RLock()
_REGISTRY_ADAPTERS = MappingProxyType({})
_REGISTRY_LOCK = threading.RLock()


def _evaluate_gate(gate, event: SourceEvent, declaration: AdapterDeclaration) -> GateDecision:
    gate_error = None
    try:
        decision = gate.evaluate(event, declaration)
    except Exception:  # noqa: BLE001 - caller gate exceptions must not escape or leak details
        gate_error = AdapterError(AdapterErrorCode.INVALID_RESULT, "caller gate evaluation failed")
    if gate_error is not None:
        raise gate_error
    if not isinstance(decision, GateDecision):
        raise AdapterError(AdapterErrorCode.INVALID_RESULT, "caller gate returned an invalid decision")
    return decision


def _plan_status(policy: GateDecision, material: GateDecision, *, source_known: bool) -> str:
    status = "ready" if source_known else "quarantine"
    for decision in (policy.decision, material.decision):
        if decision is Decision.QUARANTINE:
            status = "quarantine"
        elif decision is Decision.DENY:
            status = "blocked"
    return status


@dataclass(frozen=True, slots=True)
class SyntheticSource:
    logical_source: LogicalSourceName
    session_id: str
    source_id: str
    representation: str
    records: tuple[bytes, ...]
    _record_count: int = field(init=False, repr=False)
    _observed_size: int = field(init=False, repr=False)
    _prefix_offsets: tuple[int, ...] = field(init=False, repr=False)
    _prefix_digests: tuple[str, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_source", LogicalSourceName.parse(self.logical_source))
        if not isinstance(self.representation, str) or not self.representation:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "invalid representation")
        records = tuple(self.records)
        if any(not isinstance(record, bytes) for record in records):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "synthetic records must be bytes")
        object.__setattr__(self, "records", records)
        record_count, observed_size, offsets, digests = self._metadata(records)
        object.__setattr__(self, "_record_count", record_count)
        object.__setattr__(self, "_observed_size", observed_size)
        object.__setattr__(self, "_prefix_offsets", offsets)
        object.__setattr__(self, "_prefix_digests", digests)
        Checkpoint(
            logical_source=self.logical_source,
            session_id=self.session_id,
            source_id=self.source_id,
            representation=self.representation,
            next_record_index=0,
            next_byte_offset=0,
            observed_size=0,
            prefix_digest=digests[0],
        )

    @staticmethod
    def _metadata(records: tuple[bytes, ...]) -> tuple[int, int, tuple[int, ...], tuple[str, ...]]:
        digest = hashlib.sha256()
        offsets = [0]
        digests = [digest.hexdigest()]
        observed_size = 0
        for content in records:
            digest.update(str(len(content)).encode("ascii"))
            digest.update(b":")
            digest.update(content)
            observed_size += len(content)
            offsets.append(observed_size)
            digests.append(digest.hexdigest())
        return len(records), observed_size, tuple(offsets), tuple(digests)

    def _issued_snapshot(self) -> tuple[object, ...]:
        return (
            self.logical_source.value,
            self.session_id,
            self.source_id,
            self.representation,
            self.record_count,
            self.observed_size,
            self._prefix_digests[-1],
        )

    def _live_metadata(self) -> tuple[tuple[bytes, ...], int, int, tuple[int, ...], tuple[str, ...]]:
        try:
            if not isinstance(self.records, tuple):
                raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source records changed")
            records = tuple(self.records)
            if any(not isinstance(record, bytes) for record in records):
                raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source records changed")
            metadata = self._metadata(records)
        except AdapterError:
            raise
        except Exception:  # noqa: BLE001 - source corruption becomes a stable public conflict
            raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source records changed") from None
        expected = (
            self._record_count,
            self._observed_size,
            self._prefix_offsets,
            self._prefix_digests,
        )
        if metadata != expected:
            raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source records changed")
        return records, *metadata

    def _validate_live(self, expected_snapshot: tuple[object, ...] | None = None) -> tuple[bytes, ...]:
        records, record_count, observed_size, _offsets, digests = self._live_metadata()
        snapshot = (
            self.logical_source.value,
            self.session_id,
            self.source_id,
            self.representation,
            record_count,
            observed_size,
            digests[-1],
        )
        if expected_snapshot is not None and snapshot != expected_snapshot:
            raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source records changed")
        return records

    @property
    def observed_size(self) -> int:
        return self._observed_size

    @property
    def record_count(self) -> int:
        return self._record_count

    def prefix_digest(self, record_count: int) -> str:
        if not isinstance(record_count, int) or isinstance(record_count, bool) or not 0 <= record_count <= self.record_count:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "record cursor is out of range")
        return self._prefix_digests[record_count]

    def prefix_byte_offset(self, record_count: int) -> int:
        if not isinstance(record_count, int) or isinstance(record_count, bool) or not 0 <= record_count <= self.record_count:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "record cursor is out of range")
        return self._prefix_offsets[record_count]


class SyntheticAdapter:
    """A source adapter with no host side effects and explicit source mutation hooks."""

    __slots__ = (
        "_checkpoints",
        "_lock",
        "_selected",
        "_sources",
        "declaration",
    )

    def __init__(
        self,
        sources: Iterable[SyntheticSource],
        *,
        max_record_bytes: int = 1024,
        max_asset_bytes: int = 4096,
        max_open_records: int = 128,
        max_open_bytes: int = 64 * 1024,
    ) -> None:
        limits = StreamLimits(max_record_bytes, max_asset_bytes, max_open_records, max_open_bytes)
        declaration = AdapterDeclaration(
            name="synthetic",
            version="1",
            logical_sources=(LogicalSourceName.TRANSCRIPT, LogicalSourceName.SESSION_METADATA),
            approved_roots=("synthetic-root",),
            material_classes=("transcript", "metadata", "asset"),
            representations=SUPPORTED_REPRESENTATIONS,
            limits=limits,
            approved_representation_transitions=APPROVED_REPRESENTATION_TRANSITIONS,
        )
        source_list = list(sources)
        if any(not isinstance(source, SyntheticSource) for source in source_list):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "synthetic sources are invalid")
        object.__setattr__(self, "_sources", source_list)
        object.__setattr__(self, "_selected", {})
        object.__setattr__(self, "_checkpoints", {})
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "declaration", declaration)

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, "declaration"):
            raise AttributeError("synthetic adapter is sealed")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("synthetic adapter is sealed")

    def probe(self, request: ProbeRequest) -> ProbeResult:
        if not isinstance(request, ProbeRequest):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "probe request required")
        source = self._current(request.logical_source, request.session_id)
        return ProbeResult(
            logical_source=request.logical_source,
            session_id=request.session_id,
            source_exists=source is not None,
            capabilities=("checkpoint", "open", "resolve"),
            representations=() if source is None else (source.representation,),
        )

    def inspect(self) -> AdapterInspection:
        return AdapterInspection(
            adapter={"name": self.declaration.name, "version": self.declaration.version},
            logical_sources=tuple(sorted(source.value for source in self.declaration.logical_sources)),
            capabilities=("checkpoint", "open", "plan", "probe", "resolve"),
        )

    def plan(
        self,
        event: SourceEvent,
        gates: GateSet | None,
        prior_checkpoint: Checkpoint | None = None,
    ) -> Plan:
        global _PLAN_ISSUANCE

        if not isinstance(event, SourceEvent):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "source event required")
        if not isinstance(gates, GateSet):
            raise AdapterError(AdapterErrorCode.GATES_REQUIRED, "policy and material gates are required")
        if event.approved_root not in self.declaration.approved_roots:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "approved root is not declared")
        source = self._current(event.logical_source, event.session_id)
        if source is None:
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "logical source is not present")
        policy = _evaluate_gate(gates.policy, event, self.declaration)
        material = _evaluate_gate(gates.material, event, self.declaration)
        transitioned_from = None
        next_record_index = 0
        next_byte_offset = 0
        prefix_digest = source.prefix_digest(0)
        if prior_checkpoint is not None:
            self._validate_resume(source, prior_checkpoint)
            next_record_index = prior_checkpoint.next_record_index
            next_byte_offset = prior_checkpoint.next_byte_offset
            prefix_digest = prior_checkpoint.prefix_digest
            if source.representation != prior_checkpoint.representation:
                transitioned_from = prior_checkpoint.representation
        checkpoint = self._checkpoint(
            source,
            next_record_index,
            next_byte_offset,
            prefix_digest,
        )
        status = _plan_status(
            policy,
            material,
            source_known=source.representation in self.declaration.representations,
        )
        plan = Plan(
            adapter_name=self.declaration.name,
            adapter_version=self.declaration.version,
            event_id=event.event_id,
            logical_source=event.logical_source,
            session_id=event.session_id,
            source_id=source.source_id,
            representation=source.representation,
            approved_root=event.approved_root,
            estimated_records=source.record_count - next_record_index,
            estimated_bytes=source.observed_size - next_byte_offset,
            checkpoint=checkpoint,
            limits=self.declaration.limits,
            policy=policy,
            material=material,
            status=status,
            transitioned_from=transitioned_from,
        )
        issued_snapshot = (
            plan.adapter_name,
            plan.adapter_version,
            plan.event_id,
            plan.logical_source.value,
            plan.session_id,
            plan.source_id,
            plan.representation,
            plan.approved_root,
            plan.estimated_records,
            plan.estimated_bytes,
            (
                plan.checkpoint.logical_source.value,
                plan.checkpoint.session_id,
                plan.checkpoint.source_id,
                plan.checkpoint.representation,
                plan.checkpoint.next_record_index,
                plan.checkpoint.next_byte_offset,
                plan.checkpoint.observed_size,
                plan.checkpoint.prefix_digest,
            ),
            (
                plan.limits.max_record_bytes,
                plan.limits.max_asset_bytes,
                plan.limits.max_open_records,
                plan.limits.max_open_bytes,
            ),
            (
                plan.policy.decision.value,
                plan.policy.reason_codes,
                plan.policy.authority_ref,
            ),
            (
                plan.material.decision.value,
                plan.material.reason_codes,
                plan.material.authority_ref,
            ),
            plan.status.value,
            plan.transitioned_from,
        )
        with _PLAN_ISSUANCE_LOCK:
            issued = dict(_PLAN_ISSUANCE)
            issued[(id(self), id(plan))] = (
                self,
                plan,
                issued_snapshot,
                source._issued_snapshot(),
            )
            _PLAN_ISSUANCE = MappingProxyType(issued)
        return plan

    def open(self, plan: Plan, *, cancellation=None) -> BoundedRecordStream:
        if not isinstance(plan, Plan):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan required")
        source_snapshot = self._validate_plan(plan)
        if plan.status.value != "ready":
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not approved")
        source = self._current(plan.logical_source, plan.session_id)
        if source is None:
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "logical source is not present")
        if source.source_id != plan.source_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source identity changed")
        if source.representation != plan.representation:
            if source.representation not in self.declaration.representations:
                raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "source representation changed")
            raise AdapterError(AdapterErrorCode.SOURCE_REPRESENTATION_CHANGED, "source representation changed")
        expected_status = _plan_status(
            plan.policy,
            plan.material,
            source_known=source.representation in self.declaration.representations,
        )
        if plan.status.value != expected_status:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan gate decisions are invalid")
        if plan.checkpoint.representation not in self.declaration.representations:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "checkpoint representation is unknown")
        if plan.checkpoint.representation != plan.representation:
            raise AdapterError(AdapterErrorCode.SOURCE_REPRESENTATION_CHANGED, "plan representation changed")
        self._validate_resume(source, plan.checkpoint)
        if (
            plan.estimated_records != source.record_count - plan.checkpoint.next_record_index
            or plan.estimated_bytes != source.observed_size - plan.checkpoint.next_byte_offset
        ):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan estimates are invalid")
        source._validate_live(source_snapshot)
        checkpoint = Checkpoint(
            logical_source=plan.checkpoint.logical_source,
            session_id=plan.checkpoint.session_id,
            source_id=plan.checkpoint.source_id,
            representation=plan.checkpoint.representation,
            next_record_index=plan.checkpoint.next_record_index,
            next_byte_offset=plan.checkpoint.next_byte_offset,
            observed_size=plan.checkpoint.observed_size,
            prefix_digest=plan.checkpoint.prefix_digest,
        )
        declaration_limits = self.declaration.limits
        limits = StreamLimits(
            declaration_limits.max_record_bytes,
            declaration_limits.max_asset_bytes,
            declaration_limits.max_open_records,
            declaration_limits.max_open_bytes,
        )

        def records():
            for index in range(checkpoint.next_record_index, source.record_count):
                live_records = source._validate_live(source_snapshot)
                yield SourceRecord(
                    logical_source=source.logical_source,
                    session_id=source.session_id,
                    record_index=index,
                    content=live_records[index],
                    material_class=(
                        "metadata"
                        if source.logical_source is LogicalSourceName.SESSION_METADATA
                        else "transcript"
                    ),
                )

        def checkpoint_factory(count: int, byte_offset: int) -> Checkpoint:
            source._validate_live(source_snapshot)
            return self._checkpoint(
                source,
                checkpoint.next_record_index + count,
                checkpoint.next_byte_offset + byte_offset,
                source.prefix_digest(checkpoint.next_record_index + count),
            )

        return BoundedRecordStream(
            records(),
            checkpoint_factory,
            limits,
            cancellation=cancellation,
        )

    def checkpoint(self, result: StreamResult) -> Checkpoint:
        if not isinstance(result, StreamResult):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream result required")
        checkpoint = result.next_checkpoint
        with self._lock:
            source = self._current(checkpoint.logical_source, checkpoint.session_id)
            if source is None:
                raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "logical source is not present")
            if source.representation not in self.declaration.representations:
                raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "source representation is unknown")
            if checkpoint.representation not in self.declaration.representations:
                raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "checkpoint representation is unknown")
            self._validate_resume(source, checkpoint)
            published = self._checkpoint(
                source,
                checkpoint.next_record_index,
                checkpoint.next_byte_offset,
                checkpoint.prefix_digest,
            )
            key = (checkpoint.logical_source.value, checkpoint.session_id, checkpoint.source_id)
            current = self._checkpoints.get(key)
            if current is not None and current.representation != source.representation:
                self._validate_resume(source, current)
                current = self._checkpoint(
                    source,
                    current.next_record_index,
                    current.next_byte_offset,
                    current.prefix_digest,
                )
                self._checkpoints[key] = current
            if current is None or published.next_record_index > current.next_record_index:
                self._checkpoints[key] = published
            elif published.next_record_index == current.next_record_index:
                if (
                    published.next_byte_offset != current.next_byte_offset
                    or published.observed_size != current.observed_size
                    or published.prefix_digest != current.prefix_digest
                ):
                    raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "checkpoint prefix changed")
                self._checkpoints[key] = published
            return self._checkpoints[key]

    def resolve(
        self,
        session_id: str,
        prior_checkpoint: Checkpoint | None,
        logical_source: LogicalSourceName | None = None,
    ) -> ResolveResult:
        if prior_checkpoint is not None and not isinstance(prior_checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "checkpoint required")
        session_id = _identifier(session_id, "session id")
        logical_source = self._logical_source_for(session_id, prior_checkpoint, logical_source)
        if logical_source is None:
            return ResolveResult(ResolveStatus.NOT_FOUND, session_id)
        source = self._current(logical_source, session_id)
        if (
            prior_checkpoint is not None
            and prior_checkpoint.representation not in self.declaration.representations
        ):
            return ResolveResult(
                ResolveStatus.QUARANTINE,
                session_id,
                None if source is None else source.representation,
            )
        if source is None:
            return ResolveResult(ResolveStatus.NOT_FOUND, session_id)
        if source.representation not in self.declaration.representations:
            return ResolveResult(ResolveStatus.QUARANTINE, session_id, source.representation)
        if prior_checkpoint is None:
            return ResolveResult(ResolveStatus.FOUND, session_id, source.representation)
        try:
            self._validate_resume(source, prior_checkpoint)
        except AdapterError as error:
            if error.code is AdapterErrorCode.UNKNOWN_REPRESENTATION:
                return ResolveResult(ResolveStatus.QUARANTINE, session_id, source.representation)
            return ResolveResult(ResolveStatus.CONFLICT, session_id, source.representation)
        checkpoint = self._checkpoint(
            source,
            prior_checkpoint.next_record_index,
            prior_checkpoint.next_byte_offset,
            prior_checkpoint.prefix_digest,
        )
        if source.representation != prior_checkpoint.representation:
            return ResolveResult(ResolveStatus.MOVED, session_id, source.representation, checkpoint)
        return ResolveResult(ResolveStatus.FOUND, session_id, source.representation, checkpoint)

    def select_representation(
        self,
        session_id: str,
        representation: str,
        logical_source: LogicalSourceName | None = None,
    ) -> None:
        session_id = _identifier(session_id, "session id")
        representation = _label(representation, "representation")
        if representation not in self.declaration.representations:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "representation is not registered")
        with self._lock:
            logical_source = self._logical_source_for(session_id, None, logical_source)
            if logical_source is None or self._current(logical_source, session_id) is None:
                raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "logical source is not present")
            if not any(
                source.logical_source is logical_source
                and source.session_id == session_id
                and source.representation == representation
                for source in self._sources
            ):
                raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "representation is not present")
            self._selected[(logical_source.value, session_id)] = representation

    def mutate(
        self,
        kind: str,
        *,
        logical_source: LogicalSourceName = LogicalSourceName.TRANSCRIPT,
        session_id: str = "session-one",
    ) -> None:
        with self._lock:
            source = self._current(logical_source, session_id)
            if source is None:
                raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "logical source is not present")
            if kind == "prefix":
                changed = replace(source, records=(b"changed",) + source.records[1:])
            elif kind == "truncate":
                changed = replace(source, records=())
            elif kind == "replace":
                changed = replace(source, source_id="replacement-source")
            else:
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "unknown synthetic mutation")
            self._replace_source(source, changed)

    def _validate_plan(self, plan: Plan) -> tuple[object, ...]:
        try:
            with _PLAN_ISSUANCE_LOCK:
                issuance = _PLAN_ISSUANCE.get((id(self), id(plan)))
            if issuance is None or issuance[0] is not self or issuance[1] is not plan:
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not issued")
            issued_snapshot = issuance[2]
            source_snapshot = issuance[3]
            if (
                plan.adapter_name != self.declaration.name
                or plan.adapter_version != self.declaration.version
                or plan.logical_source not in self.declaration.logical_sources
                or plan.approved_root not in self.declaration.approved_roots
                or (
                    plan.limits.max_record_bytes,
                    plan.limits.max_asset_bytes,
                    plan.limits.max_open_records,
                    plan.limits.max_open_bytes,
                ) != (
                    self.declaration.limits.max_record_bytes,
                    self.declaration.limits.max_asset_bytes,
                    self.declaration.limits.max_open_records,
                    self.declaration.limits.max_open_bytes,
                )
            ):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan declaration is invalid")
            if not isinstance(plan.checkpoint, Checkpoint):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan checkpoint is invalid")
            if not isinstance(plan.limits, StreamLimits):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan limits are invalid")
            for decision in (plan.policy, plan.material):
                if not isinstance(decision, GateDecision):
                    raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan gate decisions are invalid")
                if GateDecision(
                    decision.decision,
                    decision.reason_codes,
                    decision.authority_ref,
                ) != decision:
                    raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan gate decisions are invalid")
            if plan.status.value != _plan_status(
                plan.policy,
                plan.material,
                source_known=plan.representation in self.declaration.representations,
            ):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan gate decisions are invalid")
            if plan.transitioned_from is not None and (
                plan.transitioned_from not in self.declaration.representations
                or (
                    plan.transitioned_from,
                    plan.representation,
                ) not in self.declaration.approved_representation_transitions
            ):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan transition is invalid")
            checkpoint = plan.checkpoint
            if (
                checkpoint.logical_source is not plan.logical_source
                or checkpoint.session_id != plan.session_id
                or checkpoint.source_id != plan.source_id
                or checkpoint.representation != plan.representation
            ):
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan checkpoint is invalid")
            current_snapshot = (
                plan.adapter_name,
                plan.adapter_version,
                plan.event_id,
                plan.logical_source.value,
                plan.session_id,
                plan.source_id,
                plan.representation,
                plan.approved_root,
                plan.estimated_records,
                plan.estimated_bytes,
                (
                    checkpoint.logical_source.value,
                    checkpoint.session_id,
                    checkpoint.source_id,
                    checkpoint.representation,
                    checkpoint.next_record_index,
                    checkpoint.next_byte_offset,
                    checkpoint.observed_size,
                    checkpoint.prefix_digest,
                ),
                (
                    plan.limits.max_record_bytes,
                    plan.limits.max_asset_bytes,
                    plan.limits.max_open_records,
                    plan.limits.max_open_bytes,
                ),
                (
                    plan.policy.decision.value,
                    plan.policy.reason_codes,
                    plan.policy.authority_ref,
                ),
                (
                    plan.material.decision.value,
                    plan.material.reason_codes,
                    plan.material.authority_ref,
                ),
                plan.status.value,
                plan.transitioned_from,
            )
            if current_snapshot != issued_snapshot:
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan fields changed")
            return source_snapshot
        except AdapterError:
            raise
        except Exception:  # noqa: BLE001 - malformed plans must produce a stable public error
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is invalid") from None

    def _logical_source_for(
        self,
        session_id: str,
        prior_checkpoint: Checkpoint | None,
        logical_source: LogicalSourceName | None,
    ) -> LogicalSourceName | None:
        if logical_source is not None:
            return LogicalSourceName.parse(logical_source)
        if prior_checkpoint is not None:
            return prior_checkpoint.logical_source
        candidates = {
            source.logical_source
            for source in self._sources
            if source.session_id == session_id
        }
        if len(candidates) > 1:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "logical source is required")
        return next(iter(candidates), None)

    def _current(self, logical_source: LogicalSourceName, session_id: str) -> SyntheticSource | None:
        logical_source = LogicalSourceName.parse(logical_source)
        with self._lock:
            candidates = [
                source for source in self._sources
                if source.logical_source is logical_source and source.session_id == session_id
            ]
            if not candidates:
                return None
            selected = self._selected.get((logical_source.value, session_id))
            if selected is not None:
                for source in candidates:
                    if source.representation == selected:
                        return source
            return min(candidates, key=lambda source: REPRESENTATION_PRIORITY.get(source.representation, 999))

    def _checkpoint(self, source, record_index: int, byte_offset: int, prefix_digest: str) -> Checkpoint:
        return Checkpoint(
            logical_source=source.logical_source,
            session_id=source.session_id,
            source_id=source.source_id,
            representation=source.representation,
            next_record_index=record_index,
            next_byte_offset=byte_offset,
            observed_size=source.observed_size,
            prefix_digest=prefix_digest,
        )

    def _validate_resume(self, source: SyntheticSource, checkpoint: Checkpoint) -> None:
        if not isinstance(checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "checkpoint required")
        _records, record_count, observed_size, offsets, digests = source._live_metadata()
        if checkpoint.logical_source is not source.logical_source or checkpoint.session_id != source.session_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "checkpoint source changed")
        if checkpoint.source_id != source.source_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source identity changed")
        if checkpoint.representation not in self.declaration.representations:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "checkpoint representation is unknown")
        if source.representation not in self.declaration.representations:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "source representation is unknown")
        if (
            checkpoint.representation != source.representation
            and (
                checkpoint.representation,
                source.representation,
            ) not in self.declaration.approved_representation_transitions
        ):
            raise AdapterError(AdapterErrorCode.SOURCE_REPRESENTATION_CHANGED, "representation transition is not approved")
        if checkpoint.next_record_index > record_count or checkpoint.next_byte_offset > observed_size:
            raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than checkpoint")
        if digests[checkpoint.next_record_index] != checkpoint.prefix_digest:
            raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source prefix changed")
        if checkpoint.next_byte_offset != offsets[checkpoint.next_record_index]:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "byte cursor is inconsistent")
        if observed_size < checkpoint.observed_size:
            raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than checkpoint")
        if observed_size != checkpoint.observed_size:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "observed size is inconsistent")

    def _replace_source(self, old: SyntheticSource, new: SyntheticSource) -> None:
        self._sources[self._sources.index(old)] = new


class BuiltinAdapterRegistry:
    __slots__ = ()

    def __init__(self, adapters: dict[str, SyntheticAdapter]):
        global _REGISTRY_ADAPTERS

        expected = {source.value for source in LogicalSourceName}
        if (
            not isinstance(adapters, dict)
            or set(adapters) != expected
            or any(not isinstance(adapter, SyntheticAdapter) for adapter in adapters.values())
        ):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "builtin registry is closed")
        with _REGISTRY_LOCK:
            registered = dict(_REGISTRY_ADAPTERS)
            registered[id(self)] = (self, MappingProxyType(dict(adapters)))
            _REGISTRY_ADAPTERS = MappingProxyType(registered)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("builtin adapter registry is sealed")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("builtin adapter registry is sealed")

    def logical_sources(self) -> tuple[str, ...]:
        return tuple(sorted(source.value for source in LogicalSourceName))

    def get(self, logical_source: str) -> SyntheticAdapter:
        key = LogicalSourceName.parse(logical_source).value
        with _REGISTRY_LOCK:
            entry = _REGISTRY_ADAPTERS.get(id(self))
        if entry is None or entry[0] is not self or key not in entry[1]:
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "unknown logical source")
        return entry[1][key]


def builtin_registry() -> BuiltinAdapterRegistry:
    transcript = SyntheticAdapter((SyntheticSource(
        LogicalSourceName.TRANSCRIPT, "synthetic-session", "synthetic-transcript", "active-jsonl", (b"synthetic",),
    ),))
    metadata = SyntheticAdapter((SyntheticSource(
        LogicalSourceName.SESSION_METADATA, "synthetic-session", "synthetic-metadata", "active-jsonl", (b"synthetic",),
    ),))
    return BuiltinAdapterRegistry({
        LogicalSourceName.SESSION_METADATA.value: metadata,
        LogicalSourceName.TRANSCRIPT.value: transcript,
    })
