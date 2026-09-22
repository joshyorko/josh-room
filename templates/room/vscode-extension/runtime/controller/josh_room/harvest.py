"""Bounded PCC harvest orchestration over the existing source, normalizer, crypto and outbox contracts.

The command layer intentionally owns no source or provider implementation.  A controller
may be supplied callbacks for host policy/source preparation and delivery; this keeps the
CLI thin while making the state machine testable without credentials or a live provider.
"""
from __future__ import annotations

import json
import secrets
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .pcc_outbox import PccOutbox, QueueRecord, QueueState

SCHEMA = "josh-room.harvest"
SCHEMA_VERSION = {"major": 1, "minor": 0}


class HarvestError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _envelope(*, ok: bool, command: str, **body: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": dict(SCHEMA_VERSION),
        "ok": bool(ok),
        "command": command,
    }
    result.update(body)
    return result


def _record_public(record: QueueRecord) -> dict[str, object]:
    # Checkpoints are intentionally reduced to stable cursor metadata.  In particular,
    # never expose a host path accidentally supplied by an adapter or hook.
    checkpoint = record.checkpoint if isinstance(record.checkpoint, Mapping) else {}
    safe_checkpoint = {
        key: checkpoint[key]
        for key in ("source", "representation", "start", "end", "prefix_sha256")
        if key in checkpoint and key != "path"
    }
    return {
        "event_id": record.event_id,
        "session_id": record.session_id,
        "state": record.state.value,
        "resume_state": record.resume_state.value,
        "sequence": record.sequence,
        "event_ids": list(record.event_ids),
        "is_final": record.is_final,
        "checkpoint": safe_checkpoint,
        "metadata": dict(record.metadata),
        "failure_code": record.failure_code,
        "ciphertext_sha256": record.ciphertext_sha256,
        "ciphertext_size": record.ciphertext_size,
        "object_key": record.object_key,
        "index_id": record.index_id,
    }


def _owner() -> str:
    return "harvest-" + secrets.token_hex(8)


@dataclass(frozen=True)
class HarvestPlan:
    event_id: str
    session_id: str
    state: str
    action: str
    checkpoint: Mapping[str, object]
    source: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "state": self.state,
            "action": self.action,
            "checkpoint": dict(self.checkpoint),
            "source": dict(self.source),
        }


class HarvestController:
    """Own bounded lifecycle transitions, not source/policy/provider authority.

    ``prepare`` receives a claimed queue record and is expected to perform source
    reconciliation, normalization and encryption through #6/#9.  ``publish`` receives
    the claimed record and must use #11's prepared-only seam.  Neither callback is
    called by status, inspect or plan.
    """

    def __init__(
        self,
        outbox: PccOutbox,
        *,
        prepare: Callable[[PccOutbox, QueueRecord, str], object] | None = None,
        publish: Callable[[PccOutbox, QueueRecord, str], object] | None = None,
        owner_factory: Callable[[], str] = _owner,
    ) -> None:
        if not isinstance(outbox, PccOutbox):
            raise TypeError("outbox is required")
        self.outbox = outbox
        self.prepare = prepare
        self.publish = publish
        self.owner_factory = owner_factory

    def plan(self, event_id: str | None = None) -> dict[str, object]:
        inspection = self.outbox.inspect(event_id)
        records = [_record_public(record) for record in inspection.records]
        plans = []
        for record in inspection.records:
            state = record.state
            if state in {QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                action = "prepare"
            elif state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                action = "drain"
            elif state is QueueState.COMMITTED:
                action = "none"
            else:
                action = "inspect"
            plans.append(HarvestPlan(record.event_id, record.session_id, state.value, action, record.checkpoint, record.metadata).to_dict())
        return _envelope(
            ok=not bool(inspection.diagnostics),
            command="plan",
            content_free=True,
            records=records,
            plans=plans,
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )

    def status(self) -> dict[str, object]:
        inspection = self.outbox.inspect()
        counts = Counter(record.state.value for record in inspection.records)
        return _envelope(
            ok=not any(item.code == "storage-unavailable" for item in inspection.diagnostics),
            command="status",
            states={key: counts[key] for key in sorted(counts)},
            queued=sum(counts.get(state.value, 0) for state in (QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP)),
            prepared=sum(counts.get(state.value, 0) for state in (QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED)),
            committed=counts.get(QueueState.COMMITTED.value, 0),
            quarantined=counts.get(QueueState.QUARANTINED.value, 0),
            records=len(inspection.records),
            quarantined_files=inspection.quarantined_count,
            partial=inspection.partial_count,
            orphan_prepared=list(inspection.orphan_prepared or []),
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )

    def inspect(self, event_id: str | None = None) -> dict[str, object]:
        inspection = self.outbox.inspect(event_id)
        return _envelope(
            ok=not bool(inspection.diagnostics),
            command="inspect",
            metadata_only=True,
            records=[_record_public(record) for record in inspection.records],
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )

    def _claim_one(self) -> tuple[QueueRecord | None, str]:
        owner = self.owner_factory()
        return self.outbox.claim(owner), owner

    def _release(self, record: QueueRecord, owner: str) -> None:
        # #8 owns lease release; use it when present without duplicating its
        # transition semantics in this controller.
        release = getattr(self.outbox, "release", None)
        if callable(release):
            release(record.event_id, owner)

    def run(self, *, limit: int = 1, offline: bool = False) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 1000:
            raise HarvestError("invalid-limit")
        prepared: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []

        for _ in range(limit):
            record, owner = self._claim_one()
            if record is None:
                break
            try:
                if record.resume_state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                    prepared.append(_record_public(record))
                    if offline:
                        self._release(record, owner)
                    continue
                if self.prepare is None:
                    self.outbox.retry(record.event_id, owner, reason_code="prepare-unavailable")
                    failures.append({"event_id": record.event_id, "code": "prepare-unavailable"})
                    continue
                outcome = self.prepare(self.outbox, record, owner)
                current = self.outbox.inspect_record(record.event_id)
                prepared.append(_record_public(current or record))
                if offline:
                    self._release(record, owner)
                if outcome is not None and isinstance(outcome, Mapping):
                    prepared[-1]["prepare"] = dict(outcome)
            except Exception as error:  # noqa: BLE001 - callback details map to stable codes
                code = getattr(error, "code", None) or "prepare-failed"
                code = getattr(code, "value", code)
                try:
                    self.outbox.retry(record.event_id, owner, reason_code=str(code))
                except Exception:  # noqa: S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": str(code)})
        return _envelope(
            ok=not failures,
            command="run",
            offline=bool(offline),
            prepared=prepared,
            failures=failures,
            remaining=self.status()["queued"],
        )

    def drain(self, *, limit: int = 1) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 1000:
            raise HarvestError("invalid-limit")
        delivered: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        for _ in range(limit):
            record, owner = self._claim_one()
            if record is None:
                break
            if record.resume_state not in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                try:
                    self.outbox.retry(record.event_id, owner, reason_code="not-prepared")
                except Exception:  # noqa: S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": "not-prepared"})
                continue
            try:
                if self.publish is None:
                    raise HarvestError("publisher-unavailable")
                outcome = self.publish(self.outbox, record, owner)
                current = self.outbox.inspect_record(record.event_id)
                item = _record_public(current or record)
                if isinstance(outcome, Mapping):
                    item["publish"] = dict(outcome)
                delivered.append(item)
            except Exception as error:  # noqa: BLE001 - provider details map to stable codes
                code = getattr(error, "code", None) or "publish-failed"
                code = getattr(code, "value", code)
                try:
                    self.outbox.retry(record.event_id, owner, reason_code=str(code))
                except Exception:  # noqa: S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": str(code)})
        return _envelope(ok=not failures, command="drain", delivered=delivered, failures=failures, rescanned=False)

    def _transition(self, event_id: str, state: QueueState, reason: str) -> dict[str, object]:
        owner = self.owner_factory()
        record = self.outbox.inspect_record(event_id)
        if record is None:
            raise HarvestError("not-found")
        if record.owner is None:
            claimed = self.outbox.claim(owner)
            if claimed is None or claimed.event_id != event_id:
                raise HarvestError("lease-conflict")
        else:
            owner = record.owner
        updated = self.outbox.transition(event_id, owner, state, reason_code=reason)
        return _envelope(ok=True, command=state.value, record=_record_public(updated))

    def retry(self, event_id: str, reason: str = "operator-retry") -> dict[str, object]:
        return self._transition(event_id, QueueState.RETRYABLE_FAILURE, reason)

    def quarantine(self, event_id: str, reason: str = "operator-quarantine") -> dict[str, object]:
        return self._transition(event_id, QueueState.QUARANTINED, reason)

    def discard(self, event_id: str) -> dict[str, object]:
        # The outbox owns destructive deletion and intentionally has no delete seam.
        # Keep discard explicit without pretending to delete durable evidence.
        return self.quarantine(event_id, "operator-discard") | {"discard": "quarantined"}

    def reconcile(self, *, limit: int = 1000) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 10000:
            raise HarvestError("invalid-limit")
        inspection = self.outbox.inspect()
        repaired: list[dict[str, object]] = []
        for event_id in inspection.orphan_prepared[:limit]:
            repaired.append({"event_id": event_id, "state": "orphan-prepared"})
        return _envelope(
            ok=True,
            command="reconcile",
            bounded=True,
            repaired=repaired,
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )


def dump_result(result: Mapping[str, object]) -> str:
    return json.dumps(dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = ["HarvestController", "HarvestError", "SCHEMA", "SCHEMA_VERSION", "dump_result"]
