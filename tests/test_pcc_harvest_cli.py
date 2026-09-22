from __future__ import annotations

from pathlib import Path

from josh_room.harvest import HarvestController
from josh_room.pcc_enqueue import enqueue_trigger
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.scheduler import install, remove, status


def _queued(root: Path) -> PccOutbox:
    outbox = PccOutbox(root)
    enqueue_trigger(
        outbox,
        event_id="event-1",
        session_id="session-1",
        checkpoint={"source": "codex-hook", "representation": "unknown", "start": 0, "end": 0, "prefix_sha256": "0" * 64},
    )
    return outbox


def test_plan_status_inspect_are_content_free(tmp_path):
    controller = HarvestController(_queued(tmp_path / "outbox"), owner_factory=lambda: "worker-1")
    assert controller.plan()["content_free"] is True
    assert controller.status()["queued"] == 1
    inspected = controller.inspect()
    assert inspected["metadata_only"] is True
    assert "path" not in str(inspected)


def test_scheduler_install_status_remove_is_idempotent(tmp_path):
    first = install(platform_name="linux", home=tmp_path, executable="/opt/josh-room")
    second = install(platform_name="linux", home=tmp_path, executable="/opt/josh-room")
    assert first["ok"] is True and first["changed"] is True
    assert second["ok"] is True and second["changed"] is False
    assert status(platform_name="linux", home=tmp_path)["installed"] is True
    assert remove(platform_name="linux", home=tmp_path)["changed"] is True
    assert remove(platform_name="linux", home=tmp_path)["changed"] is False


def test_prepare_and_drain_never_prepare_in_drain(tmp_path):
    outbox = _queued(tmp_path / "outbox")

    def prepare(box, record, owner):
        box.transition(record.event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        return box.prepare_encrypted(record.event_id, owner, b"cipher", metadata={"object_kind": "session-segment"})

    calls = []

    def publish(box, record, owner):
        calls.append(record.event_id)
        box.mark_uploaded(record.event_id, owner, object_key="evidence/objects/sha256/" + record.ciphertext_sha256, ciphertext_size=record.ciphertext_size)
        box.publish_index(record.event_id, owner, index_id="a" * 64)
        return box.commit(record.event_id, owner)

    controller = HarvestController(outbox, prepare=prepare, publish=publish, owner_factory=lambda: "worker-1")
    result = controller.run(offline=True)
    assert result["ok"] is True
    # The prepared lease remains held by #8 until its normal lease expiry.
    assert result["prepared"][0]["resume_state"] == QueueState.PREPARED_ENCRYPTED.value
    assert controller.status()["prepared"] == 1
    assert calls == []
