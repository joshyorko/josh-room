from __future__ import annotations

from types import SimpleNamespace

from josh_room import harvest_bridge as bridge_module
from josh_room.adapter_contract import Checkpoint, LogicalSourceName, PlanStatus
from josh_room.harvest import HarvestController
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
