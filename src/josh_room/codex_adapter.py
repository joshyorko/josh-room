"""Bounded Codex rollout adapter for the ``codex.transcript`` source.

The adapter consumes only an explicitly supplied pair of Codex session roots.
It never treats ``CODEX_HOME`` as a directory authority, reads indexes or
databases, or copies rollout records without an allowlist decision.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import zstandard
except ImportError:  # pragma: no cover - exercised only in minimal runtimes
    zstandard = None

from .adapter_contract import (
    AdapterDeclaration,
    AdapterError,
    AdapterErrorCode,
    AdapterInspection,
    BoundedRecordStream,
    CancellationToken,
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

UPSTREAM_CODEX_COMMIT = "a1f40f3f1326eff7c81b860a4f4e27c1be186e10"
UPSTREAM_CODEX_DATE = "2026-09-22"

SUPPORTED_REPRESENTATIONS = ("active-jsonl", "archived-jsonl", "compressed-jsonl-zst")
APPROVED_REPRESENTATION_TRANSITIONS = (
    ("active-jsonl", "archived-jsonl"),
    ("archived-jsonl", "compressed-jsonl-zst"),
)
_SURFACES = frozenset({"cli", "desktop", "vscode", "app-server", "subagent", "unknown"})
_JSONL_SUFFIXES = (".jsonl", ".jsonl.zst")
_COPY_SUFFIX = re.compile(r"-copy(?:-[0-9]+)?$")
_MAX_SESSION_SCAN_FILES = 128
_MAX_SESSION_SCAN_BYTES = 64 * 1024 * 1024
_MAX_SESSION_SCAN_LINES = 4096
_MAX_SESSION_SCAN_DEPTH = 8
_MAX_INLINE_VALUE_BYTES = 64 * 1024
_DEFAULT_MAX_RECORD_BYTES = 512 * 1024
_DEFAULT_MAX_OPEN_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_OPEN_RECORDS = 10_000
_MAX_ZSTD_WINDOW_BYTES = 8 * 1024 * 1024
_MAX_LEDGER_ENTRIES = 32


def _safe_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_prefix(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _source_id(session_id: str, path_key: str) -> str:
    identity = f"session:{session_id}|path:{path_key}"
    return "codex-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _safe_surface(value: object) -> str:
    return value if isinstance(value, str) and value in _SURFACES else "unknown"


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_INLINE_VALUE_BYTES:
        return value
    return None


def _value_metadata(value: object) -> dict[str, object]:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        return {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    encoded = _safe_json({"value": value})
    return {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}


def _sanitize_remote(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if "://" in text:
        _scheme, rest = text.split("://", 1)
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        if "/" not in rest:
            return None
        host, path = rest.split("/", 1)
        if not host or any(char in host for char in "\\ \t\r\n"):
            return None
        return f"{host.lower()}/{path.split('?', 1)[0].split('#', 1)[0].strip('/') or ''}".rstrip("/")
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        return f"{host.lower()}/{path.strip('/')}".rstrip("/")
    if re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+", text):
        return text.lower().strip("/")
    return None


@dataclass(frozen=True, slots=True)
class CodexRoots:
    """Explicit, canonical roots that may contain rollout files."""

    active: Path | str
    archived: Path | str

    def __post_init__(self) -> None:
        active = Path(self.active).expanduser().resolve(strict=False)
        archived = Path(self.archived).expanduser().resolve(strict=False)
        if active == archived:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "codex roots must be distinct")
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "archived", archived)


@dataclass(frozen=True, slots=True)
class CodexHookFacts:
    """Bounded, untrusted fields copied from a Codex lifecycle hook."""

    session_id: str
    cwd: Path | str
    transcript_path: Path | str
    surface: str = "unknown"
    subagent_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session id"))
        object.__setattr__(self, "cwd", Path(self.cwd).expanduser())
        object.__setattr__(self, "transcript_path", Path(self.transcript_path).expanduser())
        object.__setattr__(self, "surface", _safe_surface(self.surface))
        if self.subagent_type is not None:
            object.__setattr__(self, "subagent_type", _label(self.subagent_type, "subagent type"))


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    path_key: str
    representation: str
    session_id: str
    source_id: str
    surface: str
    subagent_type: str | None


@dataclass(frozen=True, slots=True)
class _RecordMeta:
    start: int
    end: int
    content: bytes
    material_class: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    candidate: _Candidate
    records: tuple[_RecordMeta, ...]
    prefix_digests: tuple[str, ...]
    boundaries: tuple[int, ...]
    stable_size: int
    content_digest: str
    content_prefix_digests: tuple[tuple[int, str], ...]
    malformed: bool
    unknown_major: bool
    unknown_kind: bool
    incomplete: bool


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    source_id: str
    path_key: str
    representation: str
    stable_size: int
    content_digest: str
    history: tuple[tuple[int, str], ...]
    latest_checkpoint: Checkpoint | None = None


@dataclass(frozen=True, slots=True)
class _IssuedPlan:
    plan_json: str
    snapshot: _Snapshot


def _record_id(record: Mapping[str, object]) -> str | None:
    for value in (record.get("session_id"), record.get("thread_id"), record.get("id")):
        if isinstance(value, str):
            try:
                return _identifier(value, "record id")
            except AdapterError:
                continue
    payload = record.get("payload")
    if isinstance(payload, Mapping):
        return _record_id(payload)
    return None


def _schema_is_unknown(record: Mapping[str, object]) -> bool:
    version = record.get("schema_version", record.get("version"))
    if version is None:
        return False
    if isinstance(version, Mapping):
        major = version.get("major")
        return not (type(major) is int and major == 1)
    return not (type(version) is int and version == 1)


def _message_text(payload: Mapping[str, object]) -> str | None:
    direct = _bounded_text(payload.get("text"))
    if direct is not None:
        return direct
    content = payload.get("content")
    if isinstance(content, str):
        return _bounded_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            text = _bounded_text(item.get("text"))
            if text is not None:
                parts.append(text)
        if parts:
            return "".join(parts)
    return None


def _safe_record(
    record: Mapping[str, object],
    *,
    session_id: str,
    surface: str,
    subagent_type: str | None,
) -> tuple[bytes, str] | None:
    kind = record.get("type")
    payload = record.get("payload")
    if not isinstance(kind, str) or not isinstance(payload, Mapping):
        return None
    if _schema_is_unknown(record):
        return None
    if kind in {"auth", "auth_refresh", "credential", "credentials", "login", "keyring", "secret"}:
        return None
    if kind == "session_meta":
        observed_id = _record_id(payload) or session_id
        if observed_id != session_id:
            return None
        body: dict[str, object] = {
            "record_kind": "session_meta",
            "session_id": session_id,
            "surface": _safe_surface(payload.get("surface", surface)),
        }
        if subagent_type is not None:
            body["subagent_type"] = subagent_type
        return _safe_json(body), "metadata"
    if kind == "response_item":
        item_type = payload.get("type")
        if item_type in {"reasoning", "encrypted_reasoning", "hidden_reasoning"} or "encrypted_content" in payload:
            return None
        if item_type == "message" and payload.get("role") in {"user", "assistant"}:
            text = _message_text(payload)
            if text is None:
                return None
            return _safe_json({"record_kind": "message", "role": payload["role"], "text": text}), "transcript"
        if item_type == "function_call":
            body = {"record_kind": "tool_call"}
            for key in ("name", "call_id", "tool_use_id"):
                value = payload.get(key)
                if isinstance(value, str) and len(value) <= 256:
                    body[key] = value
            if "arguments" in payload:
                body["arguments"] = _value_metadata(payload["arguments"])
            return _safe_json(body), "metadata"
        if item_type == "function_call_output":
            body = {"record_kind": "tool_result"}
            for key in ("call_id", "tool_use_id"):
                value = payload.get(key)
                if isinstance(value, str) and len(value) <= 256:
                    body[key] = value
            if "output" in payload:
                body["output"] = _value_metadata(payload["output"])
            return _safe_json(body), "metadata"
        return None
    if kind == "event_msg":
        event_type = payload.get("type")
        if event_type in {"user_message", "agent_message"}:
            text = _message_text(payload)
            if text is None:
                return None
            return _safe_json({"record_kind": "message", "role": "user" if event_type == "user_message" else "assistant", "text": text}), "transcript"
        if event_type == "token_count":
            body = {"record_kind": "usage"}
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
                value = payload.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    body[key] = value
            return _safe_json(body), "metadata"
        if event_type in {"approval", "permission"}:
            decision = payload.get("decision")
            if isinstance(decision, str) and len(decision) <= 64:
                return _safe_json({"record_kind": "approval", "decision": decision}), "metadata"
        if event_type in {"repo", "repository"}:
            remote = _sanitize_remote(payload.get("remote"))
            if remote is None:
                return None
            return _safe_json({"record_kind": "repository", "remote": remote}), "metadata"
        return None
    return None


class CodexTranscriptAdapter:
    """A bounded filesystem-backed implementation of ``LogicalSourceAdapter``."""

    __slots__ = ("_hook_facts", "_issued", "_ledger", "_poisoned", "_replaced", "_roots", "declaration")

    def __init__(
        self,
        roots: CodexRoots,
        *,
        hook_facts: CodexHookFacts | None = None,
        max_record_bytes: int = _DEFAULT_MAX_RECORD_BYTES,
        max_open_bytes: int = _DEFAULT_MAX_OPEN_BYTES,
        max_open_records: int = _DEFAULT_MAX_OPEN_RECORDS,
    ) -> None:
        if not isinstance(roots, CodexRoots):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "codex roots are required")
        limits = StreamLimits(max_record_bytes, max_record_bytes, max_open_records, max_open_bytes)
        declaration = AdapterDeclaration(
            name="codex-transcript",
            version="1",
            logical_sources=(LogicalSourceName.TRANSCRIPT,),
            approved_roots=("codex-sessions",),
            material_classes=("transcript", "metadata", "asset"),
            representations=SUPPORTED_REPRESENTATIONS,
            limits=limits,
            approved_representation_transitions=APPROVED_REPRESENTATION_TRANSITIONS,
        )
        object.__setattr__(self, "_roots", roots)
        object.__setattr__(self, "_hook_facts", hook_facts)
        object.__setattr__(self, "_issued", {})
        object.__setattr__(self, "_ledger", {})
        object.__setattr__(self, "_poisoned", set())
        object.__setattr__(self, "_replaced", set())
        object.__setattr__(self, "declaration", declaration)

    def probe(self, request: ProbeRequest) -> ProbeResult:
        if not isinstance(request, ProbeRequest):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "probe request required")
        candidate, ambiguous = self._select(request.session_id, None)
        return ProbeResult(
            logical_source=request.logical_source,
            session_id=request.session_id,
            source_exists=candidate is not None and not ambiguous,
            capabilities=("checkpoint", "open", "resolve"),
            representations=() if candidate is None or ambiguous else (candidate.representation,),
        )

    def inspect(self) -> AdapterInspection:
        return AdapterInspection(
            adapter={"name": self.declaration.name, "version": self.declaration.version},
            logical_sources=(LogicalSourceName.TRANSCRIPT.value,),
            capabilities=("checkpoint", "open", "plan", "probe", "resolve"),
        )

    def plan(
        self,
        event: SourceEvent,
        gates: GateSet,
        prior_checkpoint: Checkpoint | None = None,
        *,
        hook_facts: CodexHookFacts | None = None,
    ) -> Plan:
        if not isinstance(event, SourceEvent) or event.logical_source is not LogicalSourceName.TRANSCRIPT:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "transcript source event required")
        if event.approved_root not in self.declaration.approved_roots:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "approved root is not declared")
        if not isinstance(gates, GateSet):
            raise AdapterError(AdapterErrorCode.GATES_REQUIRED, "policy and material gates are required")
        policy = self._gate(gates.policy, event)
        material = self._gate(gates.material, event)
        candidate, ambiguous = self._select(event.session_id, prior_checkpoint, hook_facts=hook_facts)
        if candidate is None:
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "source is not available")
        try:
            snapshot = self._scan(candidate)
        except AdapterError:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "source cannot be safely read") from None
        if (
            ambiguous
            or snapshot.malformed
            or snapshot.unknown_major
            or snapshot.unknown_kind
            or snapshot.incomplete
            or policy.decision is Decision.QUARANTINE
            or material.decision is Decision.QUARANTINE
        ):
            status = "quarantine"
        elif policy.decision is Decision.DENY or material.decision is Decision.DENY:
            status = "blocked"
        else:
            status = "ready"
        if prior_checkpoint is not None:
            self._validate_resume(snapshot, prior_checkpoint)
            start_index = prior_checkpoint.next_record_index
            start_offset = prior_checkpoint.next_byte_offset
            prefix_digest = prior_checkpoint.prefix_digest
            transitioned_from = prior_checkpoint.representation if prior_checkpoint.representation != candidate.representation else None
        else:
            start_index = 0
            start_offset = 0
            prefix_digest = snapshot.prefix_digests[0]
            transitioned_from = None
        checkpoint = self._checkpoint(candidate, start_index, start_offset, prefix_digest, snapshot.stable_size)
        plan = Plan(
            adapter_name=self.declaration.name,
            adapter_version=self.declaration.version,
            event_id=event.event_id,
            logical_source=event.logical_source,
            session_id=event.session_id,
            source_id=candidate.source_id,
            representation=candidate.representation,
            approved_root=event.approved_root,
            estimated_records=max(0, len(snapshot.records) - start_index),
            estimated_bytes=max(0, snapshot.stable_size - start_offset),
            checkpoint=checkpoint,
            limits=self.declaration.limits,
            policy=policy,
            material=material,
            status=status,
            transitioned_from=transitioned_from,
        )
        self._issued[id(plan)] = _IssuedPlan(plan.to_json(), snapshot)
        return plan

    def open(self, plan: Plan, *, cancellation: CancellationToken | None = None) -> BoundedRecordStream:
        if not isinstance(plan, Plan):
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan required")
        issued = self._issued.get(id(plan))
        if issued is None:
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not issued by this adapter")
        try:
            if plan.to_json() != issued.plan_json:
                raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not issued by this adapter")
        except AdapterError:
            raise
        except Exception:  # noqa: BLE001 - mutated plans fail closed
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not issued by this adapter") from None
        candidate, ambiguous = self._select(plan.session_id, plan.checkpoint)
        if candidate is None or ambiguous:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source is ambiguous")
        try:
            snapshot = self._scan(candidate)
        except AdapterError:
            raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "source cannot be safely read") from None
        self._validate_resume(snapshot, plan.checkpoint)
        if plan.status.value != "ready":
            raise AdapterError(AdapterErrorCode.INVALID_REQUEST, "plan is not approved")
        if plan.representation != candidate.representation or plan.source_id != candidate.source_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source identity changed")
        start_index = plan.checkpoint.next_record_index
        planned_end = start_index + plan.estimated_records
        if len(snapshot.records) < planned_end:
            raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than plan")

        def records() -> Iterator[SourceRecord]:
            current = self._scan(candidate)
            self._validate_resume(current, plan.checkpoint)
            if len(current.records) < planned_end:
                raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than plan")
            for index, item in enumerate(current.records[start_index:planned_end], start_index):
                yield SourceRecord(
                    logical_source=LogicalSourceName.TRANSCRIPT,
                    session_id=plan.session_id,
                    record_index=index,
                    content=item.content,
                    material_class=item.material_class,
                )

        def checkpoint_factory(count: int, _bytes: int) -> Checkpoint:
            current = self._scan(candidate)
            index = plan.checkpoint.next_record_index + count
            if index > planned_end or index > len(current.records):
                raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than checkpoint")
            return self._checkpoint(
                candidate,
                index,
                current.boundaries[index],
                current.prefix_digests[index],
                current.stable_size,
            )

        return BoundedRecordStream(records(), checkpoint_factory, self.declaration.limits, cancellation=cancellation)

    def checkpoint(self, result: StreamResult) -> Checkpoint:
        if not isinstance(result, StreamResult):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "stream result required")
        checkpoint = result.next_checkpoint
        candidate, ambiguous = self._select(checkpoint.session_id, checkpoint)
        if candidate is None or ambiguous:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source is ambiguous")
        snapshot = self._scan(candidate)
        self._validate_resume(snapshot, checkpoint)
        published = self._checkpoint(
            candidate,
            checkpoint.next_record_index,
            checkpoint.next_byte_offset,
            checkpoint.prefix_digest,
            snapshot.stable_size,
        )
        self._remember_checkpoint(published)
        return published

    def resolve(
        self,
        session_id: str,
        prior_checkpoint: Checkpoint | None,
        logical_source: LogicalSourceName | None = None,
        *,
        hook_facts: CodexHookFacts | None = None,
    ) -> ResolveResult:
        session_id = _identifier(session_id, "session id")
        if logical_source is not None and LogicalSourceName.parse(logical_source) is not LogicalSourceName.TRANSCRIPT:
            return ResolveResult(ResolveStatus.NOT_FOUND, session_id)
        if prior_checkpoint is not None and not isinstance(prior_checkpoint, Checkpoint):
            raise AdapterError(AdapterErrorCode.INVALID_CHECKPOINT, "checkpoint required")
        candidate, ambiguous = self._select(session_id, prior_checkpoint, hook_facts=hook_facts)
        if candidate is None:
            return ResolveResult(ResolveStatus.QUARANTINE if ambiguous else ResolveStatus.NOT_FOUND, session_id)
        try:
            snapshot = self._scan(candidate)
        except AdapterError:
            return ResolveResult(ResolveStatus.QUARANTINE, session_id, candidate.representation)
        if candidate.source_id in self._replaced:
            return ResolveResult(ResolveStatus.CONFLICT, session_id, candidate.representation)
        if ambiguous or snapshot.malformed or snapshot.unknown_major or snapshot.unknown_kind or snapshot.incomplete:
            return ResolveResult(ResolveStatus.QUARANTINE, session_id, candidate.representation)
        if prior_checkpoint is None:
            return ResolveResult(ResolveStatus.FOUND, session_id, candidate.representation)
        try:
            self._validate_resume(snapshot, prior_checkpoint)
        except AdapterError:
            return ResolveResult(ResolveStatus.CONFLICT, session_id, candidate.representation)
        checkpoint = self._checkpoint(
            candidate,
            prior_checkpoint.next_record_index,
            prior_checkpoint.next_byte_offset,
            prior_checkpoint.prefix_digest,
            snapshot.stable_size,
        )
        status = ResolveStatus.MOVED if candidate.representation != prior_checkpoint.representation else ResolveStatus.FOUND
        return ResolveResult(status, session_id, candidate.representation, checkpoint)

    def _gate(self, gate: object, event: SourceEvent) -> GateDecision:
        try:
            result = gate.evaluate(event, self.declaration)
        except Exception:  # noqa: BLE001 - caller details are not public adapter data
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "caller gate evaluation failed") from None
        if not isinstance(result, GateDecision):
            raise AdapterError(AdapterErrorCode.INVALID_RESULT, "caller gate returned an invalid decision")
        return result

    def _checkpoint(self, candidate: _Candidate, index: int, offset: int, digest: str, observed_size: int) -> Checkpoint:
        return Checkpoint(
            logical_source=LogicalSourceName.TRANSCRIPT,
            session_id=candidate.session_id,
            source_id=candidate.source_id,
            representation=candidate.representation,
            next_record_index=index,
            next_byte_offset=offset,
            observed_size=observed_size,
            prefix_digest=digest,
        )

    def _remember_checkpoint(self, checkpoint: Checkpoint) -> None:
        entry = self._ledger.get(checkpoint.source_id)
        previous = None if entry is None else entry.latest_checkpoint
        if previous is not None:
            if checkpoint.next_record_index < previous.next_record_index:
                raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "checkpoint regressed")
            if checkpoint.next_record_index == previous.next_record_index and (
                checkpoint.next_byte_offset != previous.next_byte_offset
                or checkpoint.prefix_digest != previous.prefix_digest
                or checkpoint.observed_size < previous.observed_size
            ):
                raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "checkpoint changed")
        if entry is not None:
            self._ledger[checkpoint.source_id] = _LedgerEntry(
                entry.source_id,
                entry.path_key,
                entry.representation,
                entry.stable_size,
                entry.content_digest,
                entry.history,
                checkpoint,
            )

    def _validate_resume(self, snapshot: _Snapshot, checkpoint: Checkpoint) -> None:
        if checkpoint.logical_source is not LogicalSourceName.TRANSCRIPT or checkpoint.session_id != snapshot.candidate.session_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "checkpoint source changed")
        if checkpoint.source_id != snapshot.candidate.source_id:
            raise AdapterError(AdapterErrorCode.SOURCE_REPLACED, "source identity changed")
        if checkpoint.representation != snapshot.candidate.representation and (
            checkpoint.representation,
            snapshot.candidate.representation,
        ) not in APPROVED_REPRESENTATION_TRANSITIONS:
            raise AdapterError(AdapterErrorCode.SOURCE_REPRESENTATION_CHANGED, "representation transition is not approved")
        if checkpoint.next_record_index > len(snapshot.records) or checkpoint.next_byte_offset > snapshot.stable_size:
            raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than checkpoint")
        if checkpoint.next_record_index >= len(snapshot.boundaries):
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "record cursor is inconsistent")
        if checkpoint.next_byte_offset != snapshot.boundaries[checkpoint.next_record_index]:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "byte cursor is inconsistent")
        if checkpoint.observed_size < checkpoint.next_byte_offset:
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "observed size is inconsistent")
        if checkpoint.observed_size > snapshot.stable_size:
            raise AdapterError(AdapterErrorCode.SOURCE_TRUNCATED, "source is shorter than checkpoint")
        ledger = self._ledger.get(checkpoint.source_id)
        if (
            ledger is not None
            and ledger.latest_checkpoint is not None
            and checkpoint.observed_size < ledger.stable_size
            and checkpoint.observed_size != ledger.latest_checkpoint.observed_size
        ):
            raise AdapterError(AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT, "observed size is inconsistent")
        if snapshot.prefix_digests[checkpoint.next_record_index] != checkpoint.prefix_digest:
            raise AdapterError(AdapterErrorCode.SOURCE_PREFIX_CHANGED, "source prefix changed")

    def _select(
        self,
        session_id: str,
        prior_checkpoint: Checkpoint | None,
        *,
        hook_facts: CodexHookFacts | None = None,
    ) -> tuple[_Candidate | None, bool]:
        facts = hook_facts or self._hook_facts
        hook_candidate = None
        if facts is not None and facts.session_id == session_id:
            hook_candidate = self._candidate_from_path(facts.transcript_path, session_id, facts)
        candidates, search_incomplete = self._fallback_candidates(session_id, facts)
        if hook_candidate is not None and all(item.path != hook_candidate.path for item in candidates):
            candidates.append(hook_candidate)
        if not candidates:
            return None, False
        if prior_checkpoint is not None:
            matching: list[_Candidate] = []
            for candidate in candidates:
                try:
                    snapshot = self._scan(candidate)
                    self._validate_resume(snapshot, prior_checkpoint)
                except AdapterError:
                    continue
                matching.append(candidate)
            if len(matching) == 1:
                return matching[0], search_incomplete
            if len(candidates) == 1:
                return candidates[0], search_incomplete
            return (matching[0] if matching else candidates[0]), len(matching) != 1 or search_incomplete
        if hook_candidate is not None and len(candidates) == 1:
            return hook_candidate, search_incomplete
        return (hook_candidate or (candidates[0] if candidates else None)), len(candidates) > 1 or search_incomplete

    def _candidate_from_path(self, path: Path, session_id: str, facts: CodexHookFacts | None = None) -> _Candidate | None:
        raw_path = path.expanduser()
        if not raw_path.is_absolute() and facts is not None:
            raw_path = Path(facts.cwd) / raw_path
        raw = Path(os.path.abspath(os.fspath(raw_path)))
        canonical = raw.resolve(strict=False)
        if not self._raw_path_is_safe(raw):
            return None
        root_name = self._root_name(canonical)
        if root_name is None or not canonical.is_file() or canonical.name.startswith("."):
            return None
        representation = self._representation(canonical, root_name)
        if representation is None or not self._path_matches_session(canonical, session_id):
            return None
        root = self._roots.active if root_name == "active" else self._roots.archived
        path_key = canonical.relative_to(root).as_posix()
        if path_key.endswith(".zst"):
            path_key = path_key.removesuffix(".zst")
        return _Candidate(
            canonical,
            path_key,
            representation,
            session_id,
            _source_id(session_id, path_key),
            _safe_surface("unknown" if facts is None else facts.surface),
            None if facts is None else facts.subagent_type,
        )

    def _raw_path_is_safe(self, raw: Path) -> bool:
        for root in (self._roots.active, self._roots.archived):
            try:
                relative = raw.relative_to(root)
            except ValueError:
                continue
            current = root
            for part in relative.parts:
                current /= part
                try:
                    if stat.S_ISLNK(current.lstat().st_mode):
                        return False
                except OSError:
                    return False
            return True
        return False

    def _fallback_candidates(self, session_id: str, facts: CodexHookFacts | None) -> tuple[list[_Candidate], bool]:
        candidates: list[_Candidate] = []
        search_incomplete = False
        for root_name, root in (("active", self._roots.active), ("archived", self._roots.archived)):
            paths, bounded = self._bounded_files(root)
            search_incomplete = search_incomplete or bounded
            for path in paths:
                if not self._path_matches_session(path, session_id):
                    continue
                candidate = self._candidate_from_path(path, session_id, facts)
                if candidate is not None:
                    candidates.append(candidate)
        return candidates, search_incomplete

    def _bounded_files(self, root: Path) -> tuple[list[Path], bool]:
        if not root.is_dir():
            return [], False
        found: list[Path] = []
        stack: list[tuple[Path, int]] = [(root, 0)]
        file_count = 0
        byte_count = 0
        started = time.monotonic()
        bounded = False
        while stack and file_count < _MAX_SESSION_SCAN_FILES and byte_count < _MAX_SESSION_SCAN_BYTES:
            current, depth = stack.pop()
            if depth > _MAX_SESSION_SCAN_DEPTH:
                bounded = True
                continue
            try:
                entries = sorted(os.scandir(current), key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in entries:
                if time.monotonic() - started > 1.0:
                    return found, True
                try:
                    path = Path(entry.path).resolve(strict=False)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(_JSONL_SUFFIXES):
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                found.append(path)
                file_count += 1
                byte_count += min(size, _MAX_SESSION_SCAN_BYTES - byte_count)
                if file_count >= _MAX_SESSION_SCAN_FILES or byte_count >= _MAX_SESSION_SCAN_BYTES:
                    bounded = True
                    break
        if stack:
            bounded = True
        return found, bounded

    def _path_matches_session(self, path: Path, session_id: str) -> bool:
        name = path.name
        for suffix in (".jsonl.zst", ".jsonl"):
            if name.endswith(suffix):
                stem = name[: -len(suffix)]
                break
        else:
            return False
        if not stem.startswith("rollout-"):
            return False
        token = stem.removeprefix("rollout-")
        token = _COPY_SUFFIX.sub("", token)
        if token == session_id:
            return True
        return self._peek_session_id(path) == session_id

    def _peek_session_id(self, path: Path) -> str | None:
        try:
            with self._open_bytes(path) as handle:
                line = handle.readline(_DEFAULT_MAX_RECORD_BYTES + 1)
        except Exception:  # noqa: BLE001 - probing failures are treated as no candidate
            return None
        if not line.endswith(b"\n") or len(line) > _DEFAULT_MAX_RECORD_BYTES:
            return None
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            return None
        return _record_id(value) if isinstance(value, Mapping) else None

    @contextmanager
    def _open_bytes(self, path: Path) -> Iterator[Any]:
        if not self._raw_path_is_safe(path) or not path.is_file():
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "source cannot be opened") from None
        try:
            raw = path.open("rb")
        except OSError:
            raise AdapterError(AdapterErrorCode.UNKNOWN_SOURCE, "source cannot be opened") from None
        if path.name.endswith(".jsonl.zst"):
            if zstandard is None:
                raw.close()
                raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "compressed source support is unavailable") from None
            try:
                decoder = zstandard.ZstdDecompressor(
                    max_window_size=_MAX_ZSTD_WINDOW_BYTES // 1024,
                )
                reader = io.BufferedReader(decoder.stream_reader(raw))
            except Exception as error:  # decoder failures are opaque
                raw.close()
                raise AdapterError(AdapterErrorCode.UNKNOWN_REPRESENTATION, "compressed source is invalid") from error
            try:
                yield reader
            finally:
                reader.close()
                raw.close()
        else:
            try:
                yield raw
            finally:
                raw.close()

    def _scan(self, candidate: _Candidate) -> _Snapshot:
        records: list[_RecordMeta] = []
        prefix_digests = [hashlib.sha256(b"").hexdigest()]
        boundaries = [0]
        content_prefix_digests: list[tuple[int, str]] = [(0, hashlib.sha256(b"").hexdigest())]
        digest = hashlib.sha256()
        offset = 0
        line_count = 0
        stable_size = 0
        malformed = False
        unknown_major = False
        unknown_kind = False
        incomplete = False
        try:
            with self._open_bytes(candidate.path) as handle:
                while (
                    len(records) < _MAX_SESSION_SCAN_FILES
                    and stable_size <= _MAX_SESSION_SCAN_BYTES
                    and line_count < _MAX_SESSION_SCAN_LINES
                ):
                    line = handle.readline(_DEFAULT_MAX_RECORD_BYTES + 1)
                    if not line:
                        break
                    line_count += 1
                    if len(line) > _DEFAULT_MAX_RECORD_BYTES:
                        total = len(line)
                        complete = line.endswith(b"\n")
                        line_digest = hashlib.sha256(line)
                        digest.update(line)
                        while not complete and total <= _DEFAULT_MAX_RECORD_BYTES * 8:
                            chunk = handle.readline(64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            line_digest.update(chunk)
                            digest.update(chunk)
                            complete = chunk.endswith(b"\n")
                        if not complete or total > _DEFAULT_MAX_RECORD_BYTES * 8:
                            malformed = True
                            break
                        end = offset + total
                        stable_size = end
                        offset = end
                        records.append(_RecordMeta(
                            end - total,
                            end,
                            _safe_json({
                                "record_kind": "asset",
                                "decision": "deferred",
                                "bytes": total,
                                "sha256": line_digest.hexdigest(),
                            }),
                            "asset",
                        ))
                        content_prefix_digests.append((end, digest.hexdigest()))
                        if total > self.declaration.limits.max_record_bytes:
                            unknown_kind = True
                        boundaries.append(end)
                        prefix_digests.append(digest.hexdigest())
                        continue
                    if not line.endswith(b"\n"):
                        break
                    end = offset + len(line)
                    stable_size = end
                    offset = end
                    digest.update(line)
                    content_prefix_digests.append((end, digest.hexdigest()))
                    try:
                        value = json.loads(line)
                    except (TypeError, ValueError):
                        malformed = True
                        continue
                    if not isinstance(value, Mapping):
                        malformed = True
                        continue
                    if _schema_is_unknown(value):
                        unknown_major = True
                        continue
                    if self._is_excluded(value):
                        continue
                    safe = _safe_record(
                        value,
                        session_id=candidate.session_id,
                        surface=candidate.surface,
                        subagent_type=candidate.subagent_type,
                    )
                    if safe is None:
                        unknown_kind = True
                        continue
                    content, material_class = safe
                    if len(content) > self.declaration.limits.max_record_bytes:
                        unknown_kind = True
                        continue
                    records.append(_RecordMeta(end - len(line), end, content, material_class))
                    boundaries.append(end)
                    prefix_digests.append(digest.hexdigest())
                if (
                    len(records) >= _MAX_SESSION_SCAN_FILES
                    or stable_size >= _MAX_SESSION_SCAN_BYTES
                    or line_count >= _MAX_SESSION_SCAN_LINES
                ):
                    incomplete = True
        except AdapterError:
            raise
        except Exception:  # noqa: BLE001 - parser/decompressor failures become quarantine
            malformed = True
        snapshot = _Snapshot(
            candidate,
            tuple(records),
            tuple(prefix_digests),
            tuple(boundaries),
            stable_size,
            digest.hexdigest(),
            tuple(content_prefix_digests),
            malformed,
            unknown_major,
            unknown_kind,
            incomplete,
        )
        previous = self._ledger.get(candidate.source_id)
        poisoned = candidate.source_id in self._poisoned
        if previous is not None:
            previous_digest = dict(snapshot.content_prefix_digests).get(previous.stable_size)
            if snapshot.stable_size < previous.stable_size or previous_digest != previous.content_digest:
                self._poisoned.add(candidate.source_id)
                self._replaced.add(candidate.source_id)
                poisoned = True
        if poisoned and not snapshot.malformed:
            snapshot = _Snapshot(
                snapshot.candidate,
                snapshot.records,
                snapshot.prefix_digests,
                snapshot.boundaries,
                snapshot.stable_size,
                snapshot.content_digest,
                snapshot.content_prefix_digests,
                True,
                snapshot.unknown_major,
                snapshot.unknown_kind,
                snapshot.incomplete,
            )
        self._ledger[candidate.source_id] = _LedgerEntry(
            candidate.source_id,
            candidate.path_key,
            candidate.representation,
            snapshot.stable_size,
            snapshot.content_digest,
            snapshot.content_prefix_digests,
            None if previous is None else previous.latest_checkpoint,
        )
        while len(self._ledger) > _MAX_LEDGER_ENTRIES:
            oldest = next(iter(self._ledger))
            if oldest == candidate.source_id:
                break
            self._ledger.pop(oldest)
        return snapshot

    @staticmethod
    def _is_excluded(record: Mapping[str, object]) -> bool:
        kind = record.get("type")
        payload = record.get("payload")
        if kind in {"auth", "auth_refresh", "credential", "credentials", "login", "keyring", "secret"}:
            return True
        if kind == "response_item" and isinstance(payload, Mapping):
            return payload.get("type") in {"reasoning", "encrypted_reasoning", "hidden_reasoning"} or "encrypted_content" in payload
        return False

    def _root_name(self, path: Path) -> str | None:
        for name, root in (("active", self._roots.active), ("archived", self._roots.archived)):
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return name
        return None

    @staticmethod
    def _representation(path: Path, root_name: str) -> str | None:
        if path.name.endswith(".jsonl.zst"):
            return "compressed-jsonl-zst" if root_name == "archived" else None
        if path.name.endswith(".jsonl"):
            return "active-jsonl" if root_name == "active" else "archived-jsonl"
        return None


CodexAdapter = CodexTranscriptAdapter


def codex_transcript_adapter(roots: CodexRoots, **kwargs: object) -> CodexTranscriptAdapter:
    return CodexTranscriptAdapter(roots, **kwargs)


__all__ = [
    "APPROVED_REPRESENTATION_TRANSITIONS",
    "SUPPORTED_REPRESENTATIONS",
    "UPSTREAM_CODEX_COMMIT",
    "UPSTREAM_CODEX_DATE",
    "CodexAdapter",
    "CodexHookFacts",
    "CodexRoots",
    "CodexTranscriptAdapter",
    "codex_transcript_adapter",
]
