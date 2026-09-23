"""Host-owned Codex source bridge for bounded PCC harvest preparation.

This module is deliberately explicit: roots, policy context, device profile and
recipient authority are supplied by the CLI host.  Trigger metadata is never
used as policy or source authority, and normalized documents never enter the
public outbox metadata.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import device
from .adapter_contract import (
    Checkpoint,
    Decision,
    GateDecision,
    GateSet,
    LogicalSourceName,
    PlanStatus,
    SourceEvent,
)
from .codex_adapter import CodexRoots, CodexTranscriptAdapter
from .harvest import HarvestError
from .pcc_crypto import PreparedReceipt, RecipientSet, encrypt_and_prepare
from .pcc_enqueue import enqueue_trigger
from .pcc_outbox import PccOutbox, QueueRecord, QueueState
from .policy import CaptureRequest, PolicyConfig, PolicyContext, decide
from .policy_config import load_policy_config
from .session_evidence import CONTENT_TYPES, ValidationDisposition, validate_document
from .session_normalizer import (
    AssetReceipt,
    AssetSink,
    NormalizationContext,
    NormalizationEvent,
    NormalizationLimits,
    SessionNormalizer,
)

_MAX_CHILD_EVENTS = 64


class _PolicyGate:
    def __init__(self, decision: object) -> None:
        self._decision = decision

    def evaluate(self, _event: object, _declaration: object) -> GateDecision:
        value = getattr(self._decision, "kind", "local-only")
        mapped = {
            "allow": Decision.ALLOW,
            "deny": Decision.DENY,
            "local-only": Decision.LOCAL_ONLY,
            "quarantine": Decision.QUARANTINE,
        }.get(value, Decision.LOCAL_ONLY)
        reasons = tuple(getattr(self._decision, "reason_codes", ()) or ("policy-unavailable",))
        return GateDecision(mapped, reasons, "host-policy")


class _MaterialGate(_PolicyGate):
    def evaluate(self, _event: object, _declaration: object) -> GateDecision:
        value = getattr(self._decision, "kind", "local-only")
        mapped = {
            "allow": Decision.ALLOW,
            "deny": Decision.DENY,
            "local-only": Decision.LOCAL_ONLY,
            "quarantine": Decision.QUARANTINE,
        }.get(value, Decision.LOCAL_ONLY)
        reasons = tuple(getattr(self._decision, "reason_codes", ()) or ("policy-unavailable",))
        return GateDecision(mapped, reasons, "host-policy")


class _MemoryAssetSink:
    def __init__(self, writer: _MemoryAssetWriter, asset_id: str, content_type: str, media_category: str) -> None:
        self._writer = writer
        self._asset_id = asset_id
        self._content_type = content_type
        self._media_category = media_category
        self._body = bytearray()
        self._closed = False

    def write(self, chunk: bytes) -> None:
        if self._closed or not isinstance(chunk, bytes):
            raise ValueError("asset sink is closed")
        if len(self._body) + len(chunk) > self._writer.max_bytes:
            raise ValueError("asset exceeds ephemeral bound")
        self._body.extend(chunk)

    def commit(self) -> AssetReceipt:
        if self._closed:
            raise ValueError("asset sink is closed")
        self._closed = True
        body = bytes(self._body)
        digest = hashlib.sha256(body).hexdigest()
        self._writer._assets[self._asset_id] = (body, self._content_type, self._media_category)
        return AssetReceipt(self._asset_id, digest, len(body), self._media_category, self._content_type)

    def abort(self) -> None:
        self._closed = True
        self._body.clear()


class _MemoryAssetWriter:
    """Ephemeral asset sink; no plaintext asset is written to the outbox."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self._assets: dict[str, tuple[bytes, str, str]] = {}

    def open(self, asset_id: str, content_type: str, media_category: str) -> AssetSink:
        return _MemoryAssetSink(self, asset_id, content_type, media_category)

    def payload(self, receipt: AssetReceipt) -> bytes | None:
        value = self._assets.get(receipt.asset_id)
        return None if value is None else value[0]


@dataclass(frozen=True, slots=True)
class HostHarvestConfig:
    roots: CodexRoots
    policy: PolicyConfig
    profile_name: str
    workspace_id: str | None
    workspace_path: str | None
    repository: str | None
    path_kind: str
    age_executable: Path | None = None


@dataclass(frozen=True, slots=True)
class _Authority:
    profile: object
    recipient_set: RecipientSet
    device_id: str
    age_profile: str


def _safe_policy_file(path: Path) -> PolicyConfig:
    """Load one explicit host-owned policy file without accepting symlinks."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise HarvestError("policy-config-unavailable")
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise HarvestError("policy-config-unavailable")
        if os.name == "posix" and status.st_uid != os.getuid():
            raise HarvestError("policy-config-unavailable")
        if stat.S_IMODE(status.st_mode) & 0o077:
            raise HarvestError("policy-config-unavailable")
        body = json.loads(path.read_text(encoding="utf-8"), parse_float=lambda value: (_ for _ in ()).throw(ValueError(value)))
        return PolicyConfig.from_dict(body)
    except HarvestError:
        raise
    except Exception as error:
        raise HarvestError("policy-config-unavailable") from error


def load_host_policy(*, policy_config: Path | None, config_home: Path | None) -> PolicyConfig:
    if policy_config is not None:
        return _safe_policy_file(policy_config)
    try:
        return load_policy_config(config_home=config_home)
    except Exception as error:
        raise HarvestError("policy-config-unavailable") from error


def _checkpoint(record: QueueRecord) -> Checkpoint | None:
    raw = record.expanded_checkpoint if isinstance(record.expanded_checkpoint, Mapping) else record.checkpoint
    expanded = raw is record.expanded_checkpoint
    try:
        representation = raw["representation"]
        source = raw["source"]
        prefix = raw["prefix_sha256"]
        if representation not in {"active-jsonl", "archived-jsonl", "compressed-jsonl-zst"}:
            return None
        if not all(isinstance(item, str) for item in (source, representation, prefix)):
            return None
        if len(prefix) != 64:
            return None
        if expanded:
            next_record_index = raw["next_record_index"]
            next_byte_offset = raw["next_byte_offset"]
            observed_size = raw["observed_size"]
            if any(type(item) is not int or item < 0 for item in (next_record_index, next_byte_offset, observed_size)):
                return None
            if next_byte_offset != raw["end"] or observed_size < next_byte_offset:
                return None
        else:
            start = raw["start"]
            end = raw["end"]
            if type(start) is not int or type(end) is not int or start != end or start < 0:
                return None
            next_record_index, next_byte_offset, observed_size = 0, end, end
        return Checkpoint(
            LogicalSourceName.TRANSCRIPT,
            record.session_id,
            source,
            representation,
            next_record_index,
            next_byte_offset,
            observed_size,
            prefix,
        )
    except Exception:  # noqa: BLE001 - malformed trigger checkpoint is not authority
        return None


def _resume_state(outbox: PccOutbox, record: QueueRecord) -> tuple[Checkpoint | None, str | None]:
    """Return the newest expanded source cursor before this trigger.

    Hook triggers intentionally carry only a zero cursor.  The bridge instead
    resumes from the prior trigger's durable expansion, preserving both the
    adapter cursor and the evidence chain without replaying the prefix.
    """
    current = _checkpoint(record)
    current_expanded = record.expanded_checkpoint if isinstance(record.expanded_checkpoint, Mapping) else None
    if current_expanded is not None:
        chain = current_expanded.get("chain_head_sha256")
        return current, chain if isinstance(chain, str) else None
    candidates = [
        item for item in outbox.inspect().records
        if item.session_id == record.session_id
        and item.sequence < record.sequence
        and isinstance(item.expanded_checkpoint, Mapping)
        and _checkpoint(item) is not None
    ]
    if not candidates:
        return current, None
    predecessor = max(candidates, key=lambda item: item.sequence)
    expanded = predecessor.expanded_checkpoint
    chain = expanded.get("chain_head_sha256") if isinstance(expanded, Mapping) else None
    return _checkpoint(predecessor), chain if isinstance(chain, str) else None


def _event_checkpoint(event: NormalizationEvent, stream_checkpoint: Checkpoint) -> dict[str, object]:
    raw = event.document.get("checkpoint") if isinstance(event.document, Mapping) else None
    if isinstance(raw, Mapping) and {"source", "representation", "start", "end", "prefix_sha256"} <= set(raw):
        value = dict(raw)
        if all(isinstance(value.get(key), str) for key in ("source", "representation", "prefix_sha256")) and all(type(value.get(key)) is int for key in ("start", "end")):
            return value
    return {
        "source": stream_checkpoint.source_id,
        "representation": stream_checkpoint.representation,
        "start": stream_checkpoint.next_byte_offset,
        "end": stream_checkpoint.next_byte_offset,
        "prefix_sha256": stream_checkpoint.prefix_digest,
    }


def _authority(profile_name: str) -> _Authority:
    try:
        report = device.inspect(profile=profile_name)
        device_body = report.get("device") if isinstance(report, Mapping) else None
        device_id = device_body.get("device_id") if isinstance(device_body, Mapping) else None
        if not isinstance(device_id, str) or not device_id:
            raise HarvestError("device-unavailable")
        recipients = device.active_recipients(profile=profile_name)
        age_profile = device.active_age_profile(profile=profile_name)
        if not recipients or not isinstance(age_profile, str) or not age_profile:
            raise HarvestError("device-unavailable")
        sets = device.read_recipient_sets(profile=profile_name)
        if not sets:
            raise HarvestError("device-unavailable")
        current = sets[-1]
        version = current.get("version") if isinstance(current, Mapping) else None
        if type(version) is not int or version < 1:
            raise HarvestError("device-unavailable")
        daily = tuple(recipients[:1])
        recovery = tuple(recipients[1:2])
        additional = tuple(recipients[2:])
        return _Authority(
            profile=None,
            recipient_set=RecipientSet("", version, daily, recovery, additional),
            device_id=device_id,
            age_profile=age_profile,
        )
    except HarvestError:
        raise
    except Exception as error:
        raise HarvestError("device-unavailable") from error


@dataclass(frozen=True)
class PreparedChild:
    event_id: str
    receipt: PreparedReceipt
    ciphertext_path: Path

_EVENT_CONTENT_TYPES = {
    "session-segment": "application/vnd.josh.codex-session-segment+json",
    "session-asset": "application/vnd.josh.codex-session-asset",
    "session-final": "application/vnd.josh.codex-session-final+json",
}


def _index_event_id(evidence_event_id: str) -> str:
    candidate = f"{evidence_event_id}.index"
    if len(candidate) <= 128:
        return candidate
    return "idx-" + hashlib.sha256(evidence_event_id.encode("utf-8")).hexdigest()


def _observed_at(document: Mapping[str, object]) -> str:
    value = document.get("observed_at")
    if isinstance(value, str) and value:
        return value
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _index_document(event: NormalizationEvent, prepared: PreparedChild) -> tuple[str, dict[str, object]]:
    evidence_kind = event.document.get("kind")
    content_type = _EVENT_CONTENT_TYPES.get(evidence_kind)
    content_sha256 = event.document.get("content_sha256")
    if not isinstance(content_sha256, str):
        content_sha256 = hashlib.sha256(json.dumps(dict(event.document), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if (
        not isinstance(evidence_kind, str)
        or content_type not in CONTENT_TYPES
        or not isinstance(content_sha256, str)
        or len(content_sha256) != 64
    ):
        raise HarvestError("normalized-event-invalid")
    document: dict[str, object] = {
        "schema_name": "codex-session-evidence",
        "schema_version": {"major": 1, "minor": 0},
        "kind": "index-event",
        "event_id": _index_event_id(prepared.event_id),
        "evidence_kind": evidence_kind,
        "evidence_event_id": prepared.event_id,
        "content_sha256": content_sha256,
        "ciphertext_sha256": prepared.receipt.ciphertext_sha256,
        "ciphertext_size": prepared.receipt.ciphertext_size,
        "content_type": content_type,
        "discovery": {"state": "discovered", "observed_at": _observed_at(event.document)},
    }
    if validate_document(document).disposition is not ValidationDisposition.ACCEPTED:
        raise HarvestError("normalized-event-invalid")
    return str(document["event_id"]), document


class HostHarvestBridge:
    """Prepare one trigger from explicit host roots and policy authority."""

    def __init__(self, config: HostHarvestConfig) -> None:
        profile = config.policy.profiles.get(config.profile_name)
        if profile is None:
            raise HarvestError("profile-unavailable")
        self.config = config
        self.profile = profile
        authority = _authority(config.profile_name)
        authority = _Authority(
            profile,
            RecipientSet(
                profile.recipient_set_ref,
                authority.recipient_set.version,
                authority.recipient_set.daily_use,
                authority.recipient_set.recovery,
                authority.recipient_set.additional,
            ),
            authority.device_id,
            authority.age_profile,
        )
        self.authority = authority
        self.adapter = CodexTranscriptAdapter(config.roots)

    def _resolver(self, reference: str) -> RecipientSet:
        if reference != self.authority.recipient_set.reference:
            raise HarvestError("recipient-authority-unavailable")
        return self.authority.recipient_set

    def _decision(self, record: QueueRecord):
        trigger = record.metadata.get("trigger")
        if trigger not in {"stop", "subagent-stop", "session-end"}:
            raise HarvestError("policy-denied")
        context = PolicyContext.from_values(
            workspace_id=self.config.workspace_id or self.profile.workspace_id,
            remote=self.config.repository,
            workspace_path=self.config.workspace_path,
            path_kind=self.config.path_kind,
            context_source="host-observed",
        )
        request = CaptureRequest(
            context=context,
            logical_sources=(LogicalSourceName.TRANSCRIPT.value,),
            trigger=trigger,
        )
        decision = decide(self.config.policy, request)
        if self.config.profile_name and decision.profile_name not in {None, self.config.profile_name}:
            raise HarvestError("policy-denied")
        return decision

    def _prepare_event(
        self,
        outbox: PccOutbox,
        parent: QueueRecord,
        owner: str,
        event: NormalizationEvent,
        checkpoint: dict[str, object],
        writer: _MemoryAssetWriter,
    ) -> PreparedChild:
        event_id = event.document.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise HarvestError("normalized-event-invalid")
        source = event.document.get("source")
        capture = event.document.get("capture")
        trigger = parent.metadata.get("trigger")
        if trigger not in {"stop", "subagent-stop", "session-end"}:
            raise HarvestError("policy-denied")
        binding_id = getattr(getattr(self.profile, "destination", None), "binding_id", None)
        if self.profile.destination.kind == "private-r2" and not isinstance(binding_id, str):
            raise HarvestError("policy-denied")
        metadata = {
            "object_kind": event.kind,
            "policy_decision": "allow",
            "destination_class": self.profile.destination.kind,
            **({"destination_binding_id": binding_id} if binding_id is not None else {}),
            "workspace_id": self.profile.workspace_id,
            "source_adapter": "codex-transcript",
            "source_adapter_version": "1",
            "source_surface": source.get("surface", "unknown") if isinstance(source, Mapping) else "unknown",
            "capture_status": capture.get("status", "complete") if isinstance(capture, Mapping) else "complete",
            "sensitivity": event.document.get("sensitivity", "unknown"),
            "trigger": trigger,
            "index_event_id": _index_event_id(event_id),
        }
        receipt = enqueue_trigger(
            outbox,
            event_id=event_id,
            session_id=parent.session_id,
            checkpoint=checkpoint,
            metadata=metadata,
            policy_decision="allow",
            coalesce=False,
        )
        if receipt.event_id != event_id or receipt.state is QueueState.CAPTURE_GAP:
            raise HarvestError("child-lease-unavailable")
        child = outbox.claim_specific(event_id, owner)
        if child is None or child.event_id != event_id or child.session_id != parent.session_id or child.checkpoint != checkpoint:
            raise HarvestError("child-lease-unavailable")
        if child.resume_state is QueueState.QUEUED:
            outbox.transition(event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        payload = writer.payload(event.asset_receipt) if event.asset_receipt is not None else None
        if event.kind == "session-asset" and payload is None:
            raise HarvestError("asset-payload-unavailable")
        try:
            prepared = encrypt_and_prepare(
                event,
                outbox,
                owner,
                self.profile,
                self._resolver,
                asset_payload=payload,
                age_executable=self.config.age_executable,
                require_device=True,
            )
            outbox.prepared_path(event_id)
            if prepared.ciphertext_size <= 0:
                raise HarvestError("prepared-ciphertext-unavailable")
            outbox.release(event_id, owner)
        except Exception:
            try:
                current = outbox.inspect_record(event_id)
                if current is not None and current.owner == owner and current.state in {QueueState.CLAIMED, QueueState.SOURCE_SNAPSHOTTED}:
                    outbox.retry(event_id, owner, reason_code="prepare-failed")
            except Exception:  # noqa: BLE001, S110
                pass
            raise
        return PreparedChild(event_id, prepared, outbox.prepared_path(event_id))

    def _prepare_index_event(
        self,
        outbox: PccOutbox,
        parent: QueueRecord,
        owner: str,
        evidence_event: NormalizationEvent,
        evidence: PreparedChild,
    ) -> PreparedChild:
        event_id, document = _index_document(evidence_event, evidence)
        binding_id = getattr(getattr(self.profile, "destination", None), "binding_id", None)
        metadata = {
            "object_kind": "index-event",
            "policy_decision": "allow",
            "destination_class": self.profile.destination.kind,
            **({"destination_binding_id": binding_id} if binding_id is not None else {}),
            "workspace_id": self.profile.workspace_id,
            "content_type": "application/vnd.josh.codex-index-event+json",
            "capture_status": "complete",
            "sensitivity": "unknown",
            "evidence_kind": document["evidence_kind"],
            "evidence_event_id": document["evidence_event_id"],
            "content_sha256": document["content_sha256"],
            "ciphertext_sha256": document["ciphertext_sha256"],
            "ciphertext_size": document["ciphertext_size"],
        }
        receipt = enqueue_trigger(
            outbox,
            event_id=event_id,
            session_id=parent.session_id,
            checkpoint=parent.checkpoint,
            metadata=metadata,
            policy_decision="allow",
            coalesce=False,
        )
        if receipt.event_id != event_id or receipt.state is QueueState.CAPTURE_GAP:
            raise HarvestError("child-lease-unavailable")
        child = outbox.claim_specific(event_id, owner)
        if child is None or child.session_id != parent.session_id or child.checkpoint != parent.checkpoint:
            raise HarvestError("child-lease-unavailable")
        if child.resume_state is QueueState.QUEUED:
            outbox.transition(event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        try:
            prepared = encrypt_and_prepare(
                NormalizationEvent("index-event", document),
                outbox,
                owner,
                self.profile,
                self._resolver,
                age_executable=self.config.age_executable,
                require_device=True,
            )
            path = outbox.prepared_path(event_id)
            if prepared.ciphertext_size <= 0:
                raise HarvestError("prepared-ciphertext-unavailable")
            outbox.release(event_id, owner)
        except Exception:
            try:
                current = outbox.inspect_record(event_id)
                if current is not None and current.owner == owner and current.state in {QueueState.CLAIMED, QueueState.SOURCE_SNAPSHOTTED}:
                    outbox.retry(event_id, owner, reason_code="prepare-failed")
            except Exception:  # noqa: BLE001, S110
                pass
            raise
        return PreparedChild(event_id, prepared, path)

    def prepare(self, outbox: PccOutbox, record: QueueRecord, owner: str) -> object:
        if self.profile.destination.kind != "private-r2":
            raise HarvestError("policy-denied")
        decision = self._decision(record)
        if decision.kind != "allow" or decision.destination_class != "private-r2":
            raise HarvestError("policy-denied")
        prior, previous_segment_sha256 = _resume_state(outbox, record)
        event = SourceEvent(record.event_id, LogicalSourceName.TRANSCRIPT, record.session_id, "codex-sessions")
        gates = GateSet(_PolicyGate(decision), _MaterialGate(decision))
        try:
            plan = self.adapter.plan(event, gates, prior_checkpoint=prior)
            if plan.status is not PlanStatus.READY:
                raise HarvestError("policy-denied")
            stream = self.adapter.open(plan)
        except HarvestError:
            raise
        except Exception as error:
            raise HarvestError("source-unavailable") from error
        current = outbox.inspect_record(record.event_id)
        if current is None or current.owner != owner:
            raise HarvestError("child-lease-unavailable")
        if current.resume_state is QueueState.QUEUED:
            outbox.transition(record.event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        writer = _MemoryAssetWriter(max(1, self.profile.limits.per_asset_bytes))
        context = NormalizationContext(
            session_id=record.session_id,
            profile_id=self.profile.profile_id,
            workspace_id=self.profile.workspace_id,
            device_id=self.authority.device_id,
            source={"surface": "unknown", "adapter": "codex-transcript", "adapter_version": "1"},
            policy_decision="allow",
            sensitivity="unknown",
        )
        capture_limits = NormalizationLimits(
            max_source_bytes=self.profile.limits.per_session_bytes,
            max_record_bytes=self.profile.limits.per_record_bytes,
            max_asset_bytes=self.profile.limits.per_asset_bytes,
            max_session_bytes=self.profile.limits.per_session_bytes,
            max_working_bytes=min(self.profile.limits.per_record_bytes, 2 * 1024 * 1024),
        )
        normalizer = SessionNormalizer(
            stream,
            context,
            prior_checkpoint=prior,
            previous_segment_sha256=previous_segment_sha256,
            finalize=record.is_final,
            limits=capture_limits,
            asset_writer=writer if self.profile.capture_mode == "transcript-and-assets" else None,
        )
        child_ids: list[str] = []
        prepared_children: list[PreparedChild] = []
        try:
            for normalized in normalizer.normalize():
                if len(child_ids) + 2 > _MAX_CHILD_EVENTS:
                    raise HarvestError("child-limit")
                checkpoint = _event_checkpoint(normalized, stream.result.next_checkpoint)
                child = self._prepare_event(outbox, record, owner, normalized, checkpoint, writer)
                index_child = self._prepare_index_event(outbox, record, owner, normalized, child)
                prepared_children.extend((child, index_child))
                child_ids.extend((child.event_id, index_child.event_id))
        except HarvestError:
            raise
        except Exception as error:
            raise HarvestError("prepare-failed") from error
        latest = self.adapter.checkpoint(stream.result)
        receipt = normalizer.receipt
        if receipt is None:
            raise HarvestError("prepare-failed")
        if not child_ids:
            try:
                released = outbox.retry(record.event_id, owner, reason_code="empty-capture")
            except Exception as error:
                raise HarvestError("prepare-failed") from error
            return {"expanded": False, "child_count": 0, "state": released.state.value}
        expanded_checkpoint = {
            "source": latest.source_id,
            "representation": latest.representation,
            "start": latest.next_byte_offset,
            "end": latest.next_byte_offset,
            "prefix_sha256": latest.prefix_digest,
            "next_record_index": latest.next_record_index,
            "next_byte_offset": latest.next_byte_offset,
            "observed_size": latest.observed_size,
            "chain_head_sha256": receipt.last_segment_sha256,
        }
        try:
            expanded = outbox.expand(record.event_id, owner, child_event_ids=child_ids, checkpoint=expanded_checkpoint)
        except Exception as error:
            raise HarvestError("prepare-failed") from error
        return {"expanded": True, "child_count": len(child_ids), "child_event_ids": child_ids, "state": expanded.state.value}


__all__ = ["HostHarvestBridge", "HostHarvestConfig", "load_host_policy"]
