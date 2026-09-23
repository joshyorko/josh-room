"""Bounded PCC harvest orchestration over the existing source, normalizer, crypto and outbox contracts.

The command layer intentionally owns no source or provider implementation.  A controller
may be supplied callbacks for host policy/source preparation and delivery; this keeps the
CLI thin while making the state machine testable without credentials or a live provider.
"""
from __future__ import annotations

import json
import secrets
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .pcc_outbox import OutboxError, PccOutbox, QueueRecord, QueueState

SCHEMA = "josh-room.harvest"
SCHEMA_VERSION = {"major": 1, "minor": 0}


class HarvestError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

def _public_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for key in sorted(value):
        if not isinstance(key, str) or key.startswith("_") or key.lower() in {
            "path", "source_path", "filename", "cwd", "error", "secret", "token", "credential",
        }:
            continue
        item = value[key]
        if isinstance(item, str):
            if len(item) > 256 or any(marker in item.lower() for marker in ("secret", "token", "credential", "/")):
                continue
            result[key] = item
        elif item is None or isinstance(item, (bool, int, float)):
            result[key] = item

_SAFE_CODES = frozenset({
    "device-unavailable", "capture-authority-unavailable", "normalization-event-required",
    "normalized-event-invalid", "child-lease-unavailable", "provider-authority-unavailable",
    "provider-unavailable", "prepare-failed", "publish-failed", "not-prepared", "policy-denied",
    "policy-config-unavailable", "profile-unavailable", "source-unavailable", "asset-payload-unavailable",
    "recipient-authority-unavailable", "child-limit",
})

def _safe_code(value: object, fallback: str) -> str:
    candidate = getattr(value, "value", value)
    return candidate if isinstance(candidate, str) and candidate in _SAFE_CODES else fallback


def _envelope(*, ok: bool, command: str, **body: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": dict(SCHEMA_VERSION),
        "ok": bool(ok),
        "command": command,
    }
    result.update(body)
    if not result["ok"]:
        result.setdefault("error", "operation-failed")
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
        "expanded_event_ids": list(record.expanded_event_ids),
        "expanded_checkpoint": dict(record.expanded_checkpoint) if isinstance(record.expanded_checkpoint, Mapping) else None,
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
        profile: object | None = None,
        recipient_resolver: Callable[[str], object] | None = None,
        backend: object | None = None,
        index_ciphertext: bytes | Path | None = None,
        policy_check: Callable[[QueueRecord], Mapping[str, object]] | None = None,
    ) -> None:
        if not isinstance(outbox, PccOutbox):
            raise TypeError("outbox is required")
        self.outbox = outbox
        self.profile = profile
        self.recipient_resolver = recipient_resolver
        self.backend = backend
        self.index_ciphertext = index_ciphertext
        self.policy_check = policy_check
        self.prepare = prepare or self._prepare_default
        self.publish = publish or self._publish_default
        self.owner_factory = owner_factory

    def _publication_allowed(self, record: QueueRecord) -> bool:
        if self.policy_check is not None:
            try:
                current = self.policy_check(record)
            except Exception:  # noqa: BLE001 - host policy failures fail closed
                return False
            if current.get("decision") != "allow" or current.get("destination") != "private-r2":
                return False
        if self.profile is not None and getattr(getattr(self.profile, "destination", None), "kind", None) != "private-r2":
            return False
        destination = record.metadata.get("destination_class")
        decision = record.metadata.get("policy_decision")
        return decision == "allow" and destination == "private-r2"

    def _prepare_default(self, outbox: PccOutbox, record: QueueRecord, owner: str) -> object:
        from .device import require_prepare_upload
        from .pcc_crypto import encrypt_and_prepare
        from .session_normalizer import NormalizationEvent

        require_prepare_upload()
        raw = record.metadata.get("normalization_event")
        if not isinstance(raw, Mapping) or not isinstance(raw.get("document"), Mapping):
            raise HarvestError("normalization-event-required")
        if self.profile is None or self.recipient_resolver is None:
            raise HarvestError("capture-authority-unavailable")
        event = NormalizationEvent(str(raw.get("kind")), dict(raw["document"]))
        destination = getattr(getattr(self.profile, "destination", None), "kind", None)
        child_owner = owner
        if event.document.get("event_id") != record.event_id:
            from .pcc_enqueue import enqueue_trigger

            child_id = event.document.get("event_id")
            if not isinstance(child_id, str):
                raise HarvestError("normalized-event-invalid")
            trigger = record.metadata.get("trigger")
            if trigger not in {"stop", "subagent-stop", "session-end"}:
                raise HarvestError("policy-denied")
            if destination != "private-r2":
                raise HarvestError("policy-denied")
            receipt = enqueue_trigger(
                outbox,
                event_id=child_id,
                session_id=record.session_id,
                metadata={"object_kind": event.kind, "policy_decision": "allow", "destination_class": destination, "trigger": trigger},
            )
            if receipt.event_id != child_id or receipt.state is not QueueState.QUEUED:
                raise HarvestError("child-lease-unavailable")
            child = outbox.claim_specific(child_id, owner)
            if child is None or child.session_id != record.session_id or child.checkpoint != record.checkpoint:
                raise HarvestError("child-lease-unavailable")
            outbox.transition(child_id, owner, QueueState.SOURCE_SNAPSHOTTED)
            child_owner = owner
        receipt = encrypt_and_prepare(
            event,
            outbox,
            child_owner,
            self.profile,
            self.recipient_resolver,
            require_device=True,
        )
        if receipt.event_id != record.event_id:
            outbox.expand(record.event_id, owner, child_event_ids=[receipt.event_id], checkpoint=record.checkpoint)
            outbox.release(receipt.event_id, owner)
        return {"event_id": receipt.event_id, "kind": receipt.kind, "ciphertext_size": receipt.ciphertext_size}

    def _publish_default(self, outbox: PccOutbox, record: QueueRecord, owner: str) -> object:
        if self.backend is None or not callable(getattr(self.backend, "publish_outbox_evidence", None)):
            raise HarvestError("provider-authority-unavailable")
        result = self.backend.publish_outbox_evidence(
            outbox,
            record.event_id,
            owner,
            index_ciphertext=self.index_ciphertext,
        )
        return {
            "committed": bool(getattr(result, "committed", False)),
            "object_key": getattr(getattr(result, "object", None), "key", None),
        }

    def plan(self, event_id: str | None = None) -> dict[str, object]:
        inspection = self.outbox.inspect(event_id)
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
            plan = HarvestPlan(record.event_id, record.session_id, state.value, action, record.checkpoint, record.metadata).to_dict()
            plan.update({
                "ready": action in {"prepare", "drain"},
                "reasons": [] if action in {"prepare", "drain"} else [f"state:{state.value}"],
                "limits": {"bounded": True},
                "destination": record.metadata.get("destination_class"),
                "policy_decision": record.metadata.get("policy_decision"),
            })
            plans.append(plan)
        return _envelope(
            ok=not bool(inspection.diagnostics),
            command="plan",
            content_free=True,
            source={"session_id": event_id, "checkpoints": len(plans)},
            estimated={"records": 0, "bytes": 0},
            plans=plans,
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )

    def status(self) -> dict[str, object]:
        inspection = self.outbox.inspect()
        counts = Counter(record.state.value for record in inspection.records)
        return _envelope(
            ok=not any(item.code == "storage-unavailable" for item in inspection.diagnostics) and not any(
                counts.get(state.value, 0) for state in (QueueState.CAPTURE_GAP, QueueState.QUARANTINED, QueueState.POLICY_DENIED)
            ),
            command="status",
            states={key: counts[key] for key in sorted(counts)},
            queued=sum(counts.get(state.value, 0) for state in (QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP)),
            prepared=sum(counts.get(state.value, 0) for state in (QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED)),
            committed=counts.get(QueueState.COMMITTED.value, 0),
            quarantined=counts.get(QueueState.QUARANTINED.value, 0),
            records=len(inspection.records),
            oldest_sequence=min((record.sequence for record in inspection.records), default=None),
            last_states=[record.state.value for record in inspection.records[-8:]],
            local_bytes=sum((record.ciphertext_size or 0) for record in inspection.records),
            scheduler="unknown",
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
    def _claim_prepared(self) -> tuple[QueueRecord | None, str]:
        owner = self.owner_factory()
        now = self.outbox.clock()
        for record in self.outbox.inspect().records:
            if record.resume_state not in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                continue
            if record.owner is not None and (record.lease_until is None or record.lease_until > now):
                continue
            claimed = self.outbox.claim_specific(record.event_id, owner, takeover=record.owner is not None)
            if claimed is not None:
                return claimed, owner
        return None, owner

    def _release(self, record: QueueRecord, owner: str) -> None:
        # #8 owns lease release; trigger expansion is already terminal and
        # intentionally has no ciphertext to release on the parent record.
        current = self.outbox.inspect_record(record.event_id)
        if current is None or current.owner != owner:
            return
        release = getattr(self.outbox, "release", None)
        if callable(release):
            release(record.event_id, owner)

    def run(self, *, limit: int = 1, offline: bool = False, max_seconds: float | None = None) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 1000:
            raise HarvestError("invalid-limit")
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise HarvestError("invalid-max-seconds")
        started = time.monotonic()
        prepared: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        for _ in range(limit):
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                failures.append({"event_id": None, "code": "cancelled"})
                break
            record, owner = self._claim_one()
            if record is None:
                break
            try:
                if record.resume_state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                    prepared.append(_record_public(record))
                    self._release(record, owner)
                    continue
                if self.prepare is None:
                    self.outbox.retry(record.event_id, owner, reason_code="prepare-unavailable")
                    failures.append({"event_id": record.event_id, "code": "prepare-unavailable"})
                    continue
                outcome = self.prepare(self.outbox, record, owner)
                current = self.outbox.inspect_record(record.event_id)
                prepared.append(_record_public(current or record))
                self._release(record, owner)
                if outcome is not None:
                    prepared[-1]["prepare"] = _public_mapping(outcome)
            except Exception as error:  # noqa: BLE001 - callback details map to stable codes
                code = _safe_code(error, "device-unavailable" if error.__class__.__name__ == "DeviceError" else "prepare-failed")
                code = getattr(code, "value", code)
                try:
                    if str(code) == "policy-denied":
                        self.outbox.transition(record.event_id, owner, QueueState.POLICY_DENIED, reason_code="policy-denied")
                    else:
                        self.outbox.retry(record.event_id, owner, reason_code=str(code))
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
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

    def drain(self, *, limit: int = 1, max_seconds: float | None = None) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 1000:
            raise HarvestError("invalid-limit")
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise HarvestError("invalid-max-seconds")
        started = time.monotonic()
        delivered: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        for _ in range(limit):
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                failures.append({"event_id": None, "code": "cancelled"})
                break
            record, owner = self._claim_prepared()
            if record is None:
                break
            if record.resume_state not in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                try:
                    self.outbox.retry(record.event_id, owner, reason_code="not-prepared")
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": "not-prepared"})
                continue
            if not self._publication_allowed(record):
                try:
                    self.outbox.transition(record.event_id, owner, QueueState.POLICY_DENIED, reason_code="policy-denied")
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": "policy-denied"})
                continue
            try:
                if self.publish is None:
                    raise HarvestError("publisher-unavailable")
                outcome = self.publish(self.outbox, record, owner)
                current = self.outbox.inspect_record(record.event_id)
                if current is None or current.state is not QueueState.COMMITTED:
                    try:
                        self.outbox.retry(record.event_id, owner, reason_code="publish-failed")
                    except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                        pass
                    raise HarvestError("publish-failed")
                item = _record_public(current)
                if outcome is not None:
                    item["publish"] = _public_mapping(outcome)
                delivered.append(item)
            except Exception as error:  # noqa: BLE001 - provider details map to stable codes
                code = _safe_code(error, "provider-unavailable" if error.__class__.__name__ in {"R2EvidenceError", "R2EvidencePublicationError"} else "publish-failed")
                code = getattr(code, "value", code)
                try:
                    self.outbox.retry(record.event_id, owner, reason_code=str(code))
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": str(code)})
        return _envelope(ok=not failures, command="drain", delivered=delivered, failures=failures, rescanned=False)

    def _transition(self, event_id: str, state: QueueState, reason: str) -> dict[str, object]:
        owner = self.owner_factory()
        record = self.outbox.inspect_record(event_id)
        if record is None:
            raise HarvestError("not-found")
        if record.owner is None:
            claimed = self.outbox.claim_specific(event_id, owner)
            if claimed is None or claimed.event_id != event_id:
                raise HarvestError("lease-conflict")
        elif record.lease_until is None or record.lease_until <= self.outbox.clock():
            try:
                self.outbox.takeover(event_id, owner)
            except OutboxError as error:
                raise HarvestError("lease-conflict") from error
        else:
            raise HarvestError("lease-conflict")
        updated = self.outbox.transition(event_id, owner, state, reason_code=reason)
        return _envelope(ok=True, command=state.value, record=_record_public(updated))

    def retry(self, event_id: str, reason: str = "operator-retry") -> dict[str, object]:
        record = self.outbox.inspect_record(event_id)
        if record is None or record.state not in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
            raise HarvestError("not-retryable")
        return self._transition(event_id, QueueState.RETRYABLE_FAILURE, reason)

    def quarantine(self, event_id: str, reason: str = "operator-quarantine") -> dict[str, object]:
        record = self.outbox.inspect_record(event_id)
        if record is None or record.state not in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP, QueueState.POLICY_DENIED}:
            raise HarvestError("not-quarantinable")
        return self._transition(event_id, QueueState.QUARANTINED, reason)

    def discard(self, event_id: str) -> dict[str, object]:
        return self.quarantine(event_id, "operator-discard") | {
            "discard": "quarantined",
            "discarded": False,
            "evidence_deleted": False,
        }
    def retry_all(self, reason: str = "operator-retry") -> dict[str, object]:
        results = []
        for record in self.outbox.inspect().records:
            if record.state in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                results.append(self.retry(record.event_id, reason))
        return _envelope(ok=True, command="retry-all", records=results)

    def reconcile(self, *, limit: int = 1000) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 10000:
            raise HarvestError("invalid-limit")
        inspection = self.outbox.inspect()
        repaired: list[dict[str, object]] = []
        for event_id in (inspection.orphan_prepared or [])[:limit]:
            try:
                record = self.outbox.reconcile_prepared(
                    event_id,
                    session_id=event_id,
                    checkpoint={
                        "source": event_id,
                        "representation": "unknown",
                        "start": 0,
                        "end": 0,
                        "prefix_sha256": "0" * 64,
                    },
                )
            except OutboxError as error:
                code = _safe_code(error, "storage-unavailable")
                repaired.append({"event_id": event_id, "state": "reconcile-failed", "code": code})
            else:
                repaired.append({"event_id": record.event_id, "state": record.state.value})
        ok = not any(item.get("state") == "reconcile-failed" for item in repaired)
        return _envelope(
            ok=ok,
            command="reconcile",
            bounded=True,
            repaired=repaired,
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )


def dump_result(result: Mapping[str, object]) -> str:
    return json.dumps(dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = ["SCHEMA", "SCHEMA_VERSION", "HarvestController", "HarvestError", "dump_result"]
