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
        if isinstance(item, Mapping):
            if len(result) < 32:
                result[key] = _public_mapping(item)
        elif isinstance(item, (list, tuple)):
            if len(result) < 32:
                result[key] = [
                    _public_mapping(part) if isinstance(part, Mapping) else part
                    for part in list(item)[:32]
                    if item is None or isinstance(part, (str, int, float, bool, Mapping))
                ]
        elif isinstance(item, str):
            if len(item) > 256 or any(marker in item.lower() for marker in ("secret", "token", "credential", "/")):
                continue
            result[key] = item
        elif item is None or isinstance(item, (bool, int, float)):
            result[key] = item
    return result


_READINESS_COMPONENTS = ("policy", "source", "hook", "keyring", "device", "scheduler")
_UNKNOWN_READINESS = {
    name: {"state": "unknown", "reason": "host-authority-unavailable"}
    for name in _READINESS_COMPONENTS
}


def _host_snapshot(provider: object | None) -> dict[str, object]:
    if provider is None:
        return {"readiness": {key: dict(value) for key, value in _UNKNOWN_READINESS.items()}}
    try:
        value = provider() if callable(provider) else provider
    except Exception:  # noqa: BLE001 - host diagnostics fail closed
        return {
            "readiness": {
                key: {"state": "error", "reason": "host-authority-error"}
                for key in _READINESS_COMPONENTS
            }
        }
    snapshot = _public_mapping(value)
    readiness = snapshot.get("readiness")
    if not isinstance(readiness, Mapping):
        readiness = {}
    normalized = {}
    for key in _READINESS_COMPONENTS:
        item = readiness.get(key, snapshot.get(key))
        normalized[key] = dict(item) if isinstance(item, Mapping) else dict(_UNKNOWN_READINESS[key])
    snapshot["readiness"] = normalized
    return snapshot


def _readiness_ready(readiness: Mapping[str, object]) -> bool:
    return all(
        isinstance(readiness.get(key), Mapping)
        and readiness[key].get("state") in {"ready", "healthy", "available"}
        for key in _READINESS_COMPONENTS
    )


def _estimate_value(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > 1_000_000_000_000:
        return None
    return value
_SAFE_CODES = frozenset({
    "device-unavailable", "capture-authority-unavailable", "normalization-event-required",
    "normalized-event-invalid", "child-lease-unavailable", "provider-authority-unavailable",
    "provider-unavailable", "prepare-failed", "publish-failed", "not-prepared", "policy-denied",
    "recipient-authority-unavailable", "child-limit", "empty-capture", "index-builder-unavailable",
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
        "expanded_event_ids": list(getattr(record, "expanded_event_ids", ())),
        "expanded_checkpoint": dict(getattr(record, "expanded_checkpoint", None)) if isinstance(getattr(record, "expanded_checkpoint", None), Mapping) else None,
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
        host_status: object | None = None,
        readiness: object | None = None,
        estimate: Callable[[QueueRecord], Mapping[str, object]] | None = None,
    ) -> None:
        if not isinstance(outbox, PccOutbox):
            raise TypeError("outbox is required")
        self.outbox = outbox
        self.profile = profile
        self.recipient_resolver = recipient_resolver
        self.backend = backend
        self.index_ciphertext = index_ciphertext
        self.policy_check = policy_check
        self.host_status = host_status if host_status is not None else readiness
        self.estimate = estimate
        self._last_operation: dict[str, object] = {
            "state": "unknown",
            "reason": "operation-not-observed",
        }
        self.prepare = prepare or self._prepare_default
        self.publish = publish or self._publish_default
        self.owner_factory = owner_factory

    def _record_scope_matches(self, record: QueueRecord) -> bool:
        """Require queued delivery metadata to match the selected host profile."""
        if self.profile is None:
            return True
        destination = getattr(self.profile, "destination", None)
        expected_kind = getattr(destination, "kind", None)
        expected_binding = getattr(destination, "binding_id", None)
        if record.metadata.get("workspace_id") != getattr(self.profile, "workspace_id", None):
            return False
        if record.metadata.get("destination_class") != expected_kind:
            return False
        actual_binding = record.metadata.get("destination_binding_id")
        if expected_kind == "private-r2":
            return actual_binding == expected_binding
        return actual_binding is None

    def _publication_scope_matches(self, outbox: PccOutbox, record: QueueRecord) -> bool:
        if not self._record_scope_matches(record):
            return False
        index_event_id = record.metadata.get("index_event_id")
        if isinstance(index_event_id, str):
            index_record = outbox.inspect_record(index_event_id)
            if index_record is not None and not self._record_scope_matches(index_record):
                return False
        return True

    def _publication_allowed(self, record: QueueRecord) -> bool:
        if self.profile is not None and not self._record_scope_matches(record):
            return False
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
        index_event_id = record.metadata.get("index_event_id")
        if not isinstance(index_event_id, str) or not index_event_id:
            raise HarvestError("index-builder-unavailable")
        index_record = outbox.inspect_record(index_event_id)
        if index_record is None:
            raise HarvestError("index-builder-unavailable")
        index_metadata = index_record.metadata
        if (
            index_metadata.get("object_kind") != "index-event"
            or index_metadata.get("evidence_event_id") != record.event_id
            or index_metadata.get("evidence_kind") != record.metadata.get("object_kind")
            or index_metadata.get("ciphertext_sha256") != record.ciphertext_sha256
            or index_metadata.get("ciphertext_size") != record.ciphertext_size
        ):
            raise HarvestError("index-builder-unavailable")
        try:
            index_ciphertext = outbox.prepared_path(index_event_id)
        except Exception as error:
            raise HarvestError("index-builder-unavailable") from error
        result = self.backend.publish_outbox_evidence(
            outbox,
            record.event_id,
            owner,
            index_ciphertext=index_ciphertext,
        )
        return {
            "committed": bool(getattr(result, "committed", False)),
            "object_key": getattr(getattr(result, "object", None), "key", None),
            "index_event_id": index_event_id,
        }
    def _host(self) -> dict[str, object]:
        return _host_snapshot(self.host_status)

    def _record_estimate(self, record: QueueRecord) -> dict[str, object]:
        value: Mapping[str, object] | None = None
        if self.estimate is not None:
            try:
                candidate = self.estimate(record)
                if isinstance(candidate, Mapping):
                    value = candidate
            except Exception:  # noqa: BLE001 - estimates fail closed
                return {"records": None, "bytes": None, "state": "error", "reason": "estimate-unavailable"}
        if value is None:
            value = record.metadata
        estimated_records = _estimate_value(value.get("estimated_records", value.get("records")))
        estimated_bytes = _estimate_value(value.get("estimated_bytes", value.get("bytes")))
        if estimated_records is None and record.metadata.get("object_kind") not in {"trigger", None}:
            estimated_records = 1
        if estimated_bytes is None:
            estimated_bytes = _estimate_value(value.get("content_size"))
        state = "ready" if estimated_records is not None and estimated_bytes is not None else "unknown"
        return {
            "records": estimated_records,
            "bytes": estimated_bytes,
            "state": state,
            **({"reason": "source-estimate-unavailable"} if state != "ready" else {}),
        }

    def _profile_status(self) -> dict[str, object]:
        destination = getattr(self.profile, "destination", None)
        profile_id = getattr(self.profile, "profile_id", None)
        workspace_id = getattr(self.profile, "workspace_id", None)
        result = {
            "profile": profile_id if isinstance(profile_id, str) else None,
            "workspace": workspace_id if isinstance(workspace_id, str) else None,
            "destination": getattr(destination, "kind", None),
        }
        return {key: value for key, value in result.items() if value is not None}

    def _last_operation_status(self, visible_records: list[QueueRecord]) -> dict[str, object]:
        if self._last_operation.get("state") != "unknown":
            return dict(self._last_operation)
        if visible_records:
            latest = max(visible_records, key=lambda item: item.sequence)
            return {
                "state": "failed" if latest.failure_code else latest.state.value,
                "operation": "queue",
                **({"diagnostic": latest.failure_code} if latest.failure_code else {}),
            }
        return dict(self._last_operation)

    def _remember_operation(self, command: str, result: Mapping[str, object]) -> None:
        self._last_operation = {
            "state": "succeeded" if result.get("ok") is True else "failed",
            "operation": command,
            **({"diagnostic": result.get("error")} if result.get("error") else {}),
        }

    def plan(self, event_id: str | None = None) -> dict[str, object]:
        inspection = self.outbox.inspect(event_id)
        host = self._host()
        readiness = host["readiness"]
        plans = []
        aggregate_records = 0
        aggregate_bytes = 0
        aggregate_known = True
        for record in inspection.records:
            if record.metadata.get("object_kind") == "index-event":
                continue
            state = record.state
            if state in {QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                action = "prepare"
            elif state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                action = "drain"
            elif state is QueueState.COMMITTED:
                action = "none"
            else:
                action = "inspect"
            estimate = self._record_estimate(record)
            if estimate["records"] is None or estimate["bytes"] is None:
                aggregate_known = False
            else:
                aggregate_records += int(estimate["records"])
                aggregate_bytes += int(estimate["bytes"])
            reasons = [] if action in {"prepare", "drain"} else [f"state:{state.value}"]
            operation_ready = action == "none" or (
                action in {"prepare", "drain"} and _readiness_ready(readiness)
            )
            if action in {"prepare", "drain"} and not operation_ready:
                reasons.extend(
                    f"{key}:{value.get('state', 'unknown')}"
                    for key, value in readiness.items()
                    if isinstance(value, Mapping) and value.get("state") not in {"ready", "healthy", "available"}
                )
            plan = HarvestPlan(record.event_id, record.session_id, state.value, action, record.checkpoint, record.metadata).to_dict()
            plan.update({
                "ready": operation_ready,
                "reasons": reasons,
                "limits": {"bounded": True},
                "destination": record.metadata.get("destination_class"),
                "policy_decision": record.metadata.get("policy_decision"),
                "estimated": estimate,
                "readiness": readiness,
            })
            plans.append(plan)
        estimated = {
            "records": aggregate_records if aggregate_known else None,
            "bytes": aggregate_bytes if aggregate_known else None,
            "state": "ready" if aggregate_known else "unknown",
        }
        overall_ready = all(item["ready"] for item in plans) if plans else _readiness_ready(readiness)
        return _envelope(
            ok=not bool(inspection.diagnostics) and overall_ready,
            command="plan",
            content_free=True,
            source={"session_id": event_id, "checkpoints": len(plans)},
            profile=self._profile_status(),
            policy=readiness["policy"],
            source_readiness=readiness["source"],
            hook=readiness["hook"],
            keyring=readiness["keyring"],
            device=readiness["device"],
            scheduler=readiness["scheduler"],
            readiness=readiness,
            ready=overall_ready,
            estimated=estimated,
            plans=plans,
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )

    def status(self) -> dict[str, object]:
        inspection = self.outbox.inspect()
        visible_records = [record for record in inspection.records if record.metadata.get("object_kind") != "index-event"]
        counts = Counter(record.state.value for record in visible_records)
        host = self._host()
        readiness = host["readiness"]
        estimate_items = [self._record_estimate(record) for record in visible_records]
        estimate_known = all(item["records"] is not None and item["bytes"] is not None for item in estimate_items)
        source_adapter = next(
            (
                {
                    "state": readiness["source"].get("state", "unknown"),
                    "adapter": record.metadata.get("source_adapter"),
                    "version": record.metadata.get("source_adapter_version"),
                    "surface": record.metadata.get("source_surface"),
                }
                for record in reversed(visible_records)
                if record.metadata.get("source_adapter")
            ),
            readiness["source"],
        )
        queue_unhealthy = any(
            counts.get(state.value, 0)
            for state in (QueueState.CAPTURE_GAP, QueueState.QUARANTINED, QueueState.POLICY_DENIED, QueueState.RETRYABLE_FAILURE)
        )
        result = _envelope(
            ok=(
                not any(item.code == "storage-unavailable" for item in inspection.diagnostics)
                and not queue_unhealthy
                and _readiness_ready(readiness)
            ),
            command="status",
            states={key: counts[key] for key in sorted(counts)},
            queued=sum(counts.get(state.value, 0) for state in (QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP)),
            prepared=sum(counts.get(state.value, 0) for state in (QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED)),
            committed=counts.get(QueueState.COMMITTED.value, 0),
            quarantined=counts.get(QueueState.QUARANTINED.value, 0),
            records=len(visible_records),
            oldest_sequence=min((record.sequence for record in visible_records), default=None),
            last_states=[record.state.value for record in visible_records[-8:]],
            local_bytes=sum((record.ciphertext_size or 0) for record in inspection.records),
            estimated={
                "records": sum(int(item["records"]) for item in estimate_items) if estimate_known else None,
                "bytes": sum(int(item["bytes"]) for item in estimate_items) if estimate_known else None,
                "state": "ready" if estimate_known else "unknown",
            },
            profile=self._profile_status(),
            policy=host.get("policy", readiness["policy"]),
            source=source_adapter,
            adapter=source_adapter,
            hook=host.get("hook", readiness["hook"]),
            keyring=host.get("keyring", readiness["keyring"]),
            device=host.get("device", readiness["device"]),
            enrollment=host.get("enrollment", readiness["device"]),
            scheduler=host.get("scheduler", readiness["scheduler"]),
            readiness=readiness,
            ready=_readiness_ready(readiness) and not queue_unhealthy and not bool(inspection.diagnostics),
            last_operation=host.get("last_operation", self._last_operation_status(visible_records)),
            partial=inspection.partial_count,
            orphan_prepared=list(inspection.orphan_prepared or []),
            diagnostics=[item.to_dict() for item in inspection.diagnostics],
        )
        if not _readiness_ready(readiness) and not inspection.diagnostics and not queue_unhealthy:
            result["error"] = "host-readiness-unavailable"
        if inspection.diagnostics:
            result["error"] = inspection.diagnostics[0].code
        return result
    def _claim_one(self) -> tuple[QueueRecord | None, str]:
        owner = self.owner_factory()
        return self.outbox.claim(owner), owner

    def _claim_prepare(self) -> tuple[QueueRecord | None, str]:
        owner = self.owner_factory()
        for record in self.outbox.inspect().records:
            if record.metadata.get("object_kind") == "index-event":
                continue
            if record.state not in {QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                continue
            claimed = self.outbox.claim_specific(record.event_id, owner, takeover=record.owner is not None)
            if claimed is not None:
                return claimed, owner
        return None, owner

    def _claim_prepared(self, excluded: set[str] | None = None) -> tuple[QueueRecord | None, str]:
        excluded = excluded or set()
        owner = self.owner_factory()
        now = self.outbox.clock()
        for record in self.outbox.inspect().records:
            if record.event_id in excluded or record.metadata.get("object_kind") == "index-event":
                continue
            if record.resume_state not in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                continue
            if self.profile is not None and not self._publication_scope_matches(self.outbox, record):
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
            record, owner = self._claim_prepare()
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
                if isinstance(outcome, Mapping) and outcome.get("expanded") is False:
                    failures.append({"event_id": record.event_id, "code": "empty-capture"})
                elif outcome is not None:
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
        result = _envelope(
            ok=not failures,
            command="run",
            offline=bool(offline),
            prepared=prepared,
            failures=failures,
            remaining=self.status()["queued"],
        )
        self._remember_operation("run", result)
        return result

    def drain(self, *, limit: int = 1, max_seconds: float | None = None) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 1000:
            raise HarvestError("invalid-limit")
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise HarvestError("invalid-max-seconds")
        started = time.monotonic()
        delivered: list[dict[str, object]] = []
        failures: list[dict[str, object]] = []
        attempted: set[str] = set()
        # Scope filtering happens while selecting each bounded item, so foreign records remain unchanged.
        for _ in range(limit):
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                failures.append({"event_id": None, "code": "cancelled"})
                break
            record, owner = self._claim_prepared(attempted)
            if record is None:
                break
            attempted.add(record.event_id)
            if record.resume_state not in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                try:
                    self.outbox.retry(record.event_id, owner, reason_code="not-prepared")
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": "not-prepared"})
                continue
            if not self._publication_scope_matches(self.outbox, record):
                try:
                    self.outbox.transition(record.event_id, owner, QueueState.POLICY_DENIED, reason_code="scope-binding-mismatch")
                except Exception:  # noqa: BLE001, S110 - preserve original lifecycle failure
                    pass
                failures.append({"event_id": record.event_id, "code": "scope-binding-mismatch"})
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
        result = _envelope(ok=not failures, command="drain", delivered=delivered, failures=failures, rescanned=False)
        self._remember_operation("drain", result)
        return result

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

        record = self.outbox.inspect_record(event_id)
        if record is None or record.state not in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP, QueueState.POLICY_DENIED}:
            raise HarvestError("not-quarantinable")
        if record.state is QueueState.POLICY_DENIED:
            updated = self.outbox.quarantine_operator(event_id, reason_code=reason)
            return _envelope(ok=True, command="quarantine", record=_record_public(updated))
        return self._transition(event_id, QueueState.QUARANTINED, reason)

    def quarantine_list(self) -> dict[str, object]:
        inspection = self.outbox.inspect()
        diagnostics = [item.to_dict() for item in inspection.diagnostics]
        result = _envelope(
            ok=not diagnostics,
            command="quarantine-list",
            records=[_record_public(record) for record in inspection.records if record.state is QueueState.QUARANTINED],
            diagnostics=diagnostics,
        )
        if diagnostics:
            result["error"] = inspection.diagnostics[0].code
        return result
    def quarantine_inspect(self, event_id: str) -> dict[str, object]:
        record = self.outbox.inspect_record(event_id)
        if record is None:
            raise HarvestError("not-found")
        if record.state is not QueueState.QUARANTINED:
            raise HarvestError("not-quarantined")
        return _envelope(ok=True, command="quarantine-inspect", records=[_record_public(record)])
    def discard(self, event_id: str) -> dict[str, object]:
        record = self.outbox.inspect_record(event_id)
        if record is None or record.state is not QueueState.QUARANTINED:
            raise HarvestError("not-discardable")
        receipt = self.outbox.discard_receipt(event_id)
        return _envelope(ok=True, command="discard", record=_record_public(record), **receipt)
    def retry_all(self, reason: str = "operator-retry") -> dict[str, object]:
        results = []
        for record in self.outbox.inspect().records:
            if record.state in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                results.append(self.retry(record.event_id, reason))
        return _envelope(ok=True, command="retry-all", records=results)

    def reconcile(self, *, limit: int = 1000, max_seconds: float | None = None) -> dict[str, object]:
        if type(limit) is not int or not 0 < limit <= 10000:
            raise HarvestError("invalid-limit")
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise HarvestError("invalid-max-seconds")
        started = time.monotonic()
        inspection = self.outbox.inspect()
        repaired: list[dict[str, object]] = []
        for event_id in (inspection.orphan_prepared or [])[:limit]:
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                repaired.append({"event_id": event_id, "state": "cancelled", "code": "cancelled"})
                break
            repaired.append({"event_id": event_id, "state": "reconcile-failed", "code": "repair-authority-unavailable"})
        diagnostics = [item.to_dict() for item in inspection.diagnostics]
        ok = not any(item.get("state") == "reconcile-failed" for item in repaired) and not diagnostics
        return _envelope(
            ok=ok,
            command="reconcile",
            bounded=True,
            repaired=repaired,
            diagnostics=diagnostics,
        )


def dump_result(result: Mapping[str, object]) -> str:
    return json.dumps(dict(result), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = ["SCHEMA", "SCHEMA_VERSION", "HarvestController", "HarvestError", "dump_result"]
