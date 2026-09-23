from __future__ import annotations

from types import SimpleNamespace

import pytest

from josh_room import harvest_bridge as bridge_module
from josh_room.adapter_contract import Checkpoint, LogicalSourceName, PlanStatus
from josh_room.harvest import HarvestController, HarvestError
from josh_room.harvest_bridge import HostHarvestBridge, HostHarvestConfig, _Authority
from josh_room.pcc_crypto import RecipientSet
from josh_room.pcc_enqueue import enqueue_trigger
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.policy import CaptureProfile, Destination, Limits, PolicyConfig
from josh_room.session_evidence import canonical_digest, canonical_json
from josh_room.session_normalizer import NormalizationEvent


def test_bridge_prepares_evidence_and_index_and_drain_selects_event_path(tmp_path, monkeypatch):
    profile = CaptureProfile(
        name="synthetic",
        profile_id="profile-synthetic",
        workspace_id="workspace-synthetic",
        allowed_sources=frozenset({"codex.transcript"}),
        capture_mode="transcript-and-assets",
        triggers=frozenset({"stop"}),
        destination=Destination("private-r2", "binding-synthetic"),
        recipient_set_ref="recipients-synthetic",
        limits=Limits(100_000, 100_000, 1_000_000, 2_000_000, 30),
        allow_rules=(), deny_rules=(), downstream_memory=False,
        policy_version="1.0", provenance="synthetic",
    )
    policy = PolicyConfig({"synthetic": profile}, {})
    records = [{"record_type": "message", "role": "user", "text": "synthetic"}]
    document = {
        "schema_name": "codex-session-evidence", "schema_version": {"major": 1, "minor": 0},
        "kind": "session-segment", "event_id": "evidence-synthetic", "session_id": "session-synthetic",
        "source": {"surface": "unknown", "adapter": "codex-transcript", "adapter_version": "1"},
        "profile_id": profile.profile_id, "workspace_id": profile.workspace_id, "device_id": "device-synthetic",
        "checkpoint": {"source": "codex-hook", "representation": "unknown", "start": 0, "end": 0, "prefix_sha256": "0" * 64},
        "record_count": 1, "content_sha256": canonical_digest(records), "content_size": len(canonical_json(records)),
        "records": records,
        "asset_refs": [], "capture": {"status": "complete", "policy_decision": "allow", "sensitivity": "unknown", "counters": {}},
    }
    checkpoint = Checkpoint(LogicalSourceName.TRANSCRIPT, "session-synthetic", "codex-hook", "unknown", 0, 0, 0, "0" * 64)

    class FakeStream:
        result = SimpleNamespace(next_checkpoint=checkpoint)
        def __iter__(self):
            return iter((NormalizationEvent("session-segment", document),))

    class FakeAdapter:
        def __init__(self, _roots):
            pass
        def plan(self, *_args, **_kwargs):
            return SimpleNamespace(status=PlanStatus.READY)
        def open(self, _plan):
            return FakeStream()
        def checkpoint(self, _result):
            return checkpoint

    class FakeNormalizer:
        def __init__(self, *_args, **_kwargs):
            pass
        def normalize(self):
            return iter((NormalizationEvent("session-segment", document),))

    monkeypatch.setattr(bridge_module, "CodexTranscriptAdapter", FakeAdapter)
    monkeypatch.setattr(bridge_module, "SessionNormalizer", FakeNormalizer)
    monkeypatch.setattr(bridge_module, "decide", lambda *_args, **_kwargs: SimpleNamespace(kind="allow", destination_class="private-r2", profile_name="synthetic", reason_codes=()))
    recipients = RecipientSet(
        "recipients-synthetic",
        1,
        ("age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq",),
        ("age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag",),
        (),
    )
    monkeypatch.setattr(bridge_module, "_authority", lambda _name: _Authority(None, recipients, "device-synthetic", "synthetic"))
    monkeypatch.setattr(bridge_module.device, "require_prepare_upload", lambda: None)
    age = tmp_path / "age"
    age.write_text("#!/bin/sh\ncat\n", encoding="utf-8")
    age.chmod(0o700)
    bridge = HostHarvestBridge(
        HostHarvestConfig(
            roots=SimpleNamespace(),
            policy=policy,
            profile_name="synthetic",
            workspace_id=profile.workspace_id,
            workspace_path=None,
            repository=None,
            path_kind="unknown",
            age_executable=age,
        )
    )
    outbox = PccOutbox(tmp_path / "outbox")
    enqueue_trigger(outbox, event_id="parent-synthetic", session_id="session-synthetic", checkpoint=document["checkpoint"], metadata={"trigger": "stop", "policy_decision": "allow", "destination_class": "private-r2", "workspace_id": profile.workspace_id}, coalesce=False)
    outbox.claim("prepare-owner")
    result = bridge.prepare(outbox, outbox.inspect_record("parent-synthetic"), "prepare-owner")
    assert result["child_count"] == 2
    child_ids = result["child_event_ids"]
    evidence_id, index_id = child_ids
    assert outbox.inspect_record(evidence_id).state is QueueState.PREPARED_ENCRYPTED
    assert outbox.inspect_record(index_id).state is QueueState.PREPARED_ENCRYPTED
    assert outbox.prepared_path(index_id).is_file()

    seen = []
    class Backend:
        def publish_outbox_evidence(self, box, event_id, owner, *, index_ciphertext):
            seen.append(index_ciphertext)
            record = box.inspect_record(event_id)
            box.mark_uploaded(event_id, owner, object_key=f"evidence/objects/sha256/{record.ciphertext_sha256}", ciphertext_size=record.ciphertext_size)
            box.publish_index(event_id, owner, index_id="a" * 64)
            box.commit(event_id, owner)
            return SimpleNamespace(committed=True, object=None)

    controller = HarvestController(outbox, backend=Backend(), profile=profile, owner_factory=lambda: "drain-owner")
    drained = controller.drain()
    assert drained["ok"] is True
    assert seen == [outbox.prepared_path(index_id)]
    assert outbox.inspect_record(evidence_id).state is QueueState.COMMITTED


def test_publish_rejects_unlinked_index_file_before_backend_upload(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    checkpoint = {
        "source": "synthetic",
        "representation": "active-jsonl",
        "start": 0,
        "end": 1,
        "prefix_sha256": "a" * 64,
    }
    outbox.enqueue(
        event_id="event-unlinked-index",
        session_id="session-unlinked-index",
        checkpoint=checkpoint,
        metadata={"object_kind": "session-segment"},
    )
    outbox.claim("worker-one")
    outbox.transition("event-unlinked-index", "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(
        "event-unlinked-index",
        "worker-one",
        b"evidence ciphertext",
        metadata={"object_kind": "session-segment"},
    )

    calls = []

    class Backend:
        def publish_outbox_evidence(self, *args, **kwargs):
            calls.append((args, kwargs))

    controller = HarvestController(
        outbox,
        backend=Backend(),
        index_ciphertext=b"unrelated encrypted index",
    )
    record = outbox.inspect_record("event-unlinked-index")
    assert record is not None
    with pytest.raises(HarvestError, match="index-builder-unavailable"):
        controller._publish_default(outbox, record, "worker-one")
    assert calls == []
    assert outbox.inspect_record("event-unlinked-index").state is QueueState.PREPARED_ENCRYPTED


def test_drain_skips_foreign_profile_records_without_upload(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    checkpoint = {
        "source": "synthetic",
        "representation": "active-jsonl",
        "start": 0,
        "end": 1,
        "prefix_sha256": "a" * 64,
    }

    def stage(event_id, workspace_id, binding_id):
        index_id = f"{event_id}-index"
        evidence_metadata = {
            "object_kind": "session-segment",
            "policy_decision": "allow",
            "destination_class": "private-r2",
            "destination_binding_id": binding_id,
            "workspace_id": workspace_id,
            "index_event_id": index_id,
        }
        outbox.enqueue(
            event_id=event_id,
            session_id=f"session-{event_id}",
            checkpoint=checkpoint,
            metadata=evidence_metadata,
            coalesce=False,
        )
        owner = f"prepare-{event_id}"
        outbox.claim_specific(event_id, owner)
        outbox.transition(event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        outbox.prepare_encrypted(event_id, owner, f"ciphertext-{event_id}".encode(), metadata={"object_kind": "session-segment"})
        outbox.release(event_id, owner)
        evidence = outbox.inspect_record(event_id)
        assert evidence is not None
        index_metadata = {
            "object_kind": "index-event",
            "policy_decision": "allow",
            "destination_class": "private-r2",
            "destination_binding_id": binding_id,
            "workspace_id": workspace_id,
            "evidence_kind": "session-segment",
            "evidence_event_id": event_id,
            "ciphertext_sha256": evidence.ciphertext_sha256,
            "ciphertext_size": evidence.ciphertext_size,
        }
        outbox.enqueue(
            event_id=index_id,
            session_id=f"session-{event_id}",
            checkpoint=checkpoint,
            metadata=index_metadata,
            coalesce=False,
        )
        outbox.claim_specific(index_id, owner)
        outbox.transition(index_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        outbox.prepare_encrypted(index_id, owner, f"index-{event_id}".encode(), metadata={"object_kind": "index-event"})
        outbox.release(index_id, owner)
        return index_id

    work_index = stage("event-work", "workspace-work", "binding-work")
    personal_index = stage("event-personal", "workspace-personal", "binding-personal")
    profile = SimpleNamespace(
        workspace_id="workspace-personal",
        destination=Destination("private-r2", "binding-personal"),
    )
    uploads = []

    def publish(box, record, owner):
        uploads.append(record.event_id)
        box.mark_uploaded(record.event_id, owner, object_key=f"evidence/objects/sha256/{record.ciphertext_sha256}", ciphertext_size=record.ciphertext_size)
        box.publish_index(record.event_id, owner, index_id="a" * 64)
        box.commit(record.event_id, owner)
        return SimpleNamespace(committed=True, object=None)

    drained = HarvestController(
        outbox,
        publish=publish,
        profile=profile,
        owner_factory=lambda: "drain-owner",
    ).drain(limit=1)
    assert drained["failures"] == []
    assert uploads == ["event-personal"]
    assert outbox.inspect_record("event-personal").state is QueueState.COMMITTED
    assert outbox.inspect_record("event-work").state is QueueState.PREPARED_ENCRYPTED
    assert outbox.inspect_record(personal_index).state is QueueState.PREPARED_ENCRYPTED
    assert outbox.inspect_record(work_index).state is QueueState.PREPARED_ENCRYPTED


def test_omitted_profile_drain_never_publishes_work_record(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    outbox.enqueue(
        event_id="event-work",
        session_id="session-work",
        checkpoint={"source": "synthetic", "representation": "active-jsonl", "start": 0, "end": 1, "prefix_sha256": "a" * 64},
        metadata={
            "object_kind": "session-segment",
            "policy_decision": "allow",
            "destination_class": "private-r2",
            "destination_binding_id": "binding-work",
            "workspace_id": "workspace-work",
        },
        coalesce=False,
    )
    owner = "prepare-work"
    outbox.claim_specific("event-work", owner)
    outbox.transition("event-work", owner, QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted("event-work", owner, b"work-ciphertext", metadata={"object_kind": "session-segment"})
    outbox.release("event-work", owner)

    uploads = []

    def publish(box, record, claim_owner):
        uploads.append(record.event_id)
        box.mark_uploaded(record.event_id, claim_owner, object_key="work", ciphertext_size=record.ciphertext_size)
        box.commit(record.event_id, claim_owner)

    drained = HarvestController(
        outbox,
        backend=object(),
        publish=publish,
        owner_factory=lambda: "drain-owner",
    ).drain()

    assert drained["delivered"] == []
    assert drained["failures"] == []
    assert uploads == []
    assert outbox.inspect_record("event-work").state is QueueState.PREPARED_ENCRYPTED
