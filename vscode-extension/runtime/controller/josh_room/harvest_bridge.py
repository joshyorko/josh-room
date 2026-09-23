"""Host-owned Codex source bridge for bounded PCC harvest preparation.

This module is deliberately explicit: roots, policy context, device profile and
recipient authority are supplied by the CLI host.  Trigger metadata is never
used as policy or source authority, and normalized documents never enter the
public outbox metadata.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import device
from .adapter_contract import (
    Checkpoint,
    Decision as AdapterDecision,
    GateDecision,
    GateSet,
    LogicalSourceName,
    PlanStatus,
    SourceEvent,
)
from .codex_adapter import CodexRoots, CodexTranscriptAdapter
from .harvest import HarvestError
from .pcc_crypto import RecipientSet, encrypt_and_prepare
from .pcc_enqueue import enqueue_trigger
from .pcc_outbox import PccOutbox, QueueRecord, QueueState
from .policy import CaptureRequest, PolicyConfig, PolicyContext, decide
from .policy_config import load_policy_config
from .session_normalizer import (
    AssetReceipt,
    AssetSink,
    AssetWriter,
    NormalizationContext,
    NormalizationEvent,
    SessionNormalizer,
)

_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_MAX_CHILD_EVENTS = 64


class _PolicyGate:
    def __init__(self, decision: object) -> None:
        self._decision = decision

    def evaluate(self, _event: object, _declaration: object) -> GateDecision:
        value = getattr(self._decision, "kind", "local-only")
        mapped = {
            "allow": AdapterDecision.ALLOW,
            "deny": AdapterDecision.DENY,
            "local-only": AdapterDecision.LOCAL_ONLY,
            "quarantine": AdapterDecision.QUARANTINE,
        }.get(value, AdapterDecision.LOCAL_ONLY)
        reasons = tuple(getattr(self._decision, "reason_codes", ()) or ("policy-unavailable",))
        return GateDecision(mapped, reasons, "host-policy")


class _MaterialGate(_PolicyGate):
    def evaluate(self, _event: object, _declaration: object) -> GateDecision:
        value = getattr(self._decision, "kind", "local-only")
        mapped = {
            "allow": AdapterDecision.ALLOW,
            "deny": AdapterDecision.DENY,
            "local-only": AdapterDecision.LOCAL_ONLY,
            "quarantine": AdapterDecision.QUARANTINE,
        }.get(value, AdapterDecision.LOCAL_ONLY)
        reasons = tuple(getattr(self._decision, "reason_codes", ()) or ("policy-unavailable",))
        return GateDecision(mapped, reasons, "host-policy")


class _MemoryAssetSink:
    def __init__(self, writer: "_MemoryAssetWriter", asset_id: str, content_type: str, media_category: str) -> None:
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
    except Exception as error:  # noqa: BLE001 - stable public boundary
        raise HarvestError("policy-config-unavailable") from error


def load_host_policy(*, policy_config: Path | None, config_home: Path | None) -> PolicyConfig:
    if policy_config is not None:
        return _safe_policy_file(policy_config)
    try:
        return load_policy_config(config_home=config_home)
    except Exception as error:  # noqa: BLE001 - policy boundary is public-safe
        raise HarvestError("policy-config-unavailable") from error


def _checkpoint(record: QueueRecord) -> Checkpoint | None:
    raw = record.checkpoint
    try:
        representation = raw["representation"]
        source = raw["source"]
        start = raw["start"]
        end = raw["end"]
        prefix = raw["prefix_sha256"]
        if representation not in {"active-jsonl", "archived-jsonl", "compressed-jsonl-zst"}:
            return None
        if not all(isinstance(item, str) for item in (source, representation, prefix)):
            return None
        if type(start) is not int or type(end) is not int or start != end or start < 0:
            return None
        if len(prefix) != 64:
            return None
        return Checkpoint(
            LogicalSourceName.TRANSCRIPT,
            record.session_id,
            source,
            representation,
            0,
            end,
            end,
            prefix,
        )
    except Exception:  # noqa: BLE001 - malformed trigger checkpoint is not authority
        return None


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
    except Exception as error:  # noqa: BLE001 - native authority is fail-closed
        raise HarvestError("device-unavailable") from error


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
            trigger="session-end" if record.is_final else "stop",
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
    ) -> str:
        event_id = event.document.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise HarvestError("normalized-event-invalid")
        metadata = {
            "object_kind": event.kind,
            "policy_decision": "allow",
            "destination_class": self.profile.destination.kind,
            "workspace_id": self.profile.workspace_id,
            "source_adapter": "codex-transcript",
            "source_adapter_version": "1",
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
            encrypt_and_prepare(
                event,
                outbox,
                owner,
                self.profile,
                self._resolver,
                asset_payload=payload,
                age_executable=self.config.age_executable,
                require_device=True,
            )
            outbox.release(event_id, owner)
        except Exception:
            try:
                current = outbox.inspect_record(event_id)
                if current is not None and current.owner == owner and current.state in {QueueState.CLAIMED, QueueState.SOURCE_SNAPSHOTTED}:
                    outbox.retry(event_id, owner, reason_code="prepare-failed")
            except Exception:
                pass
            raise
        return event_id

    def prepare(self, outbox: PccOutbox, record: QueueRecord, owner: str) -> object:
        if self.profile.destination.kind != "private-r2":
            raise HarvestError("policy-denied")
        decision = self._decision(record)
        if decision.kind != "allow" or decision.destination_class != "private-r2":
            raise HarvestError("policy-denied")
        prior = _checkpoint(record)
        event = SourceEvent(record.event_id, LogicalSourceName.TRANSCRIPT, record.session_id, "codex-sessions")
        gates = GateSet(_PolicyGate(decision), _MaterialGate(decision))
        try:
            plan = self.adapter.plan(event, gates, prior_checkpoint=prior)
            if plan.status is not PlanStatus.READY:
                raise HarvestError("policy-denied")
            stream = self.adapter.open(plan)
        except HarvestError:
            raise
        except Exception as error:  # noqa: BLE001 - adapter details stay private
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
        normalizer = SessionNormalizer(stream, context, prior_checkpoint=prior, finalize=record.is_final, asset_writer=writer)
        child_ids: list[str] = []
        try:
            for normalized in normalizer.normalize():
                if len(child_ids) >= _MAX_CHILD_EVENTS:
                    raise HarvestError("child-limit")
                child_ids.append(self._prepare_event(outbox, record, owner, normalized, _event_checkpoint(normalized, stream.result.next_checkpoint), writer))
        except HarvestError:
            raise
        except Exception as error:  # noqa: BLE001 - normalizer/crypto details stay private
            raise HarvestError("prepare-failed") from error
        latest = self.adapter.checkpoint(stream.result)
        try:
            expanded = outbox.expand(record.event_id, owner, child_event_ids=child_ids, checkpoint={
                "source": latest.source_id,
                "representation": latest.representation,
                "start": latest.next_byte_offset,
                "end": latest.next_byte_offset,
                "prefix_sha256": latest.prefix_digest,
            })
        except Exception as error:  # noqa: BLE001 - durable terminal receipt is required
            raise HarvestError("prepare-failed") from error
        return {"expanded": True, "child_count": len(child_ids), "child_event_ids": child_ids, "state": expanded.state.value}


__all__ = ["HostHarvestConfig", "HostHarvestBridge", "load_host_policy"]
