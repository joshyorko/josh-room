import hashlib
import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import josh_room.pcc_outbox as outbox_module
from josh_room.pcc_outbox import (
    CaptureGap,
    LeaseConflict,
    PccOutbox,
    PreparedFileRecord,
    QueueState,
)


def _checkpoint(number: int = 1) -> dict[str, object]:
    return {
        "source": "codex.transcript",
        "representation": "active-jsonl",
        "start": number - 1,
        "end": number,
        "prefix_sha256": f"{number:064x}",
    }


def _enqueue(outbox: PccOutbox, event_id: str, number: int = 1, *, final: bool = False):
    return outbox.enqueue(
        event_id=event_id,
        session_id="session-synthetic",
        checkpoint=_checkpoint(number),
        is_final=final,
        metadata={"workspace_id": "workspace-synthetic", "source_surface": "cli"},
    )


def test_enqueue_is_local_metadata_only_and_coalesces_checkpoint_finalization(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")

    first = _enqueue(outbox, "event-one")
    duplicate = _enqueue(outbox, "event-two", final=True)
    distinct = _enqueue(outbox, "event-three", number=2)

    assert first.state is QueueState.QUEUED
    assert duplicate.coalesced is True
    assert distinct.coalesced is False
    records = outbox.inspect().records
    assert len(records) == 2
    record = outbox.inspect_record(first.event_id)
    assert record is not None
    assert record.is_final is True
    assert record.final_event_id == "event-two"
    assert record.sequence < outbox.inspect_record(distinct.event_id).sequence
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "network").exists()


def test_state_machine_claims_renews_and_rejects_live_theft(tmp_path):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")

    claimed = outbox.claim("worker-one")
    assert claimed is not None
    assert claimed.state is QueueState.CLAIMED
    assert claimed.owner == "worker-one"
    with pytest.raises(LeaseConflict):
        outbox.takeover(receipt.event_id, "worker-two")
    renewed = outbox.renew(receipt.event_id, "worker-one")
    assert renewed.lease_until == 110.0

    now[0] = 121.0
    recovered = outbox.takeover(receipt.event_id, "worker-two")
    assert recovered.owner == "worker-two"
    with pytest.raises(LeaseConflict):
        outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)


def test_reused_event_id_cannot_overwrite_a_different_checkpoint(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")

    first = _enqueue(outbox, "event-reused", number=1)
    conflict = _enqueue(outbox, "event-reused", number=2)

    assert first.state is QueueState.QUEUED
    assert conflict.state is QueueState.CAPTURE_GAP
    assert conflict.diagnostic is not None
    assert conflict.diagnostic.reason_code == "event-id-conflict"
    records = outbox.inspect().records
    assert len(records) == 1
    assert records[0].checkpoint["end"] == 1


def test_full_durable_transition_flow_keeps_prepared_and_delivery_stages_distinct(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: 100.0)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(
        receipt.event_id,
        "worker-one",
        b"opaque-ciphertext",
        metadata={"content_type": "application/vnd.josh.codex-session-segment+json"},
    )
    prepared = outbox.prepared.inspect_record(receipt.event_id)
    assert prepared is not None
    assert prepared.ciphertext == b"opaque-ciphertext"
    assert prepared.metadata["content_type"].startswith("application/")
    outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/fc98614b5258ee5f59a8f85178b36da8591a622104ee9c9f193f490c5ca5d042")
    assert outbox.inspect_record(receipt.event_id).state is QueueState.OBJECT_UPLOADED
    assert outbox.inspect_record(receipt.event_id).index_id is None
    outbox.publish_index(receipt.event_id, "worker-one", index_id="index-one")
    committed = outbox.commit(receipt.event_id, "worker-one")
    assert committed.state is QueueState.COMMITTED
    assert committed.index_id == "index-one"
    inspected = outbox.inspect()
    assert inspected.quarantined_count == 0
    assert inspected.records[0].state is QueueState.COMMITTED


def _prepared_source(tmp_path, name="ciphertext.age", body=b"synthetic-ciphertext"):
    source = tmp_path / name
    with source.open("wb") as handle:
        handle.write(body)
        handle.flush()
        __import__("os").fsync(handle.fileno())
    source.chmod(0o600)
    return source, body


def test_file_backed_preparation_publishes_ciphertext_only_state_without_loading_bytes(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-file")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    source, body = _prepared_source(tmp_path, body=b"SYNTHETIC-CIPHERTEXT-MARKER")

    prepared_queue = outbox.prepare_encrypted_file(
        receipt.event_id,
        "worker-one",
        source,
        metadata={"content_type": "application/vnd.josh.codex-session-segment+json"},
    )
    prepared = outbox.prepared.inspect_record(receipt.event_id)
    assert isinstance(prepared, PreparedFileRecord)
    assert prepared.ciphertext_file == "event-file.age"
    ciphertext_path = outbox.prepared.directory / prepared.ciphertext_file
    assert ciphertext_path.read_bytes() == body
    assert ciphertext_path.stat().st_mode & 0o777 == 0o600
    assert prepared_queue.state is QueueState.PREPARED_ENCRYPTED
    assert prepared_queue.ciphertext_sha256 == hashlib.sha256(body).hexdigest()
    assert prepared_queue.ciphertext_size == len(body)
    assert not source.exists()

    state = (outbox.prepared.directory / "event-file.json").read_text()
    assert "ciphertext_b64" not in state
    assert "SYNTHETIC-CIPHERTEXT-MARKER" not in state
    assert str(source) not in state
    assert "content_sha256" not in state
    assert "content_size" not in state
    assert "event-file.age" in state


def test_file_backed_preparation_rejects_plaintext_identity_metadata(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-file")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    source, _body = _prepared_source(tmp_path)
    with pytest.raises(ValueError):
        outbox.prepare_encrypted_file(
            receipt.event_id,
            "worker-one",
            source,
            metadata={"content_sha256": "a" * 64, "content_size": 10},
        )
    assert source.exists()
    assert not list(outbox.prepared.directory.glob("*.age"))


def test_file_backed_prepared_state_reconciles_after_queue_publication_crash(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    source, body = _prepared_source(tmp_path, body=b"recoverable-ciphertext")
    metadata = {"content_type": "application/vnd.josh.codex-session-segment+json"}
    prepared = PreparedFileRecord(
        "event-recover",
        "event-recover.age",
        metadata,
        hashlib.sha256(body).hexdigest(),
        len(body),
    )
    outbox.prepared.publish_file(prepared, source)
    recovered = outbox.reconcile_prepared(
        "event-recover",
        session_id="session-synthetic",
        checkpoint=_checkpoint(),
        metadata=metadata,
    )
    assert recovered.state is QueueState.PREPARED_ENCRYPTED
    assert outbox.inspect_record("event-recover").ciphertext_sha256 == prepared.ciphertext_sha256
    assert (outbox.prepared.directory / prepared.ciphertext_file).read_bytes() == body


def test_file_backed_publication_failure_removes_unpublished_ciphertext(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-file")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    source, _body = _prepared_source(tmp_path)

    def fail_publish(_path, _body):
        raise outbox_module.OutboxStorageError("publication-failed", pending_preserved=False)

    outbox.prepared._publisher.publish = fail_publish
    with pytest.raises(outbox_module.OutboxStorageError):
        outbox.prepare_encrypted_file(receipt.event_id, "worker-one", source)
    assert source.exists() or not list(outbox.prepared.directory.glob("*.age"))
    assert not (outbox.prepared.directory / "event-file.json").exists()


def test_file_backed_metadata_fsync_failure_cleans_both_halves_for_retry(tmp_path, monkeypatch):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    receipt = _enqueue(outbox, "event-file")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    source, body = _prepared_source(tmp_path, body=b"recoverable-ciphertext")
    original_sync = outbox_module._sync_directory
    calls = [0]

    def fail_metadata_sync(directory, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("synthetic metadata directory fsync failure")
        return original_sync(directory, **kwargs)

    monkeypatch.setattr(outbox_module, "_sync_directory", fail_metadata_sync)
    with pytest.raises(outbox_module.OutboxStorageError):
        outbox.prepare_encrypted_file(receipt.event_id, "worker-one", source)

    assert not (outbox.prepared.directory / "event-file.age").exists()
    assert not (outbox.prepared.directory / "event-file.json").exists()
    assert outbox.inspect_record(receipt.event_id).state is QueueState.SOURCE_SNAPSHOTTED

    monkeypatch.setattr(outbox_module, "_sync_directory", original_sync)
    retry_source = tmp_path / "retry.age"
    retry_source.write_bytes(body)
    retry_source.chmod(0o600)
    prepared = outbox.prepare_encrypted_file(receipt.event_id, "worker-one", retry_source)
    assert prepared.state is QueueState.PREPARED_ENCRYPTED


def test_uploaded_before_index_is_recoverable_and_retry_is_idempotent(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: 100.0)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
    outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979")
    outbox.retry(receipt.event_id, "worker-one", reason_code="index-unavailable")

    assert outbox.inspect_record(receipt.event_id).state is QueueState.RETRYABLE_FAILURE
    outbox.claim("worker-two")
    outbox.publish_index(receipt.event_id, "worker-two", index_id="index-recovered")
    outbox.publish_index(receipt.event_id, "worker-two", index_id="index-recovered")
    assert outbox.commit(receipt.event_id, "worker-two").state is QueueState.COMMITTED
    assert outbox.prepared.inspect_record(receipt.event_id).ciphertext == b"ciphertext"


def test_uploaded_object_digest_must_match_prepared_ciphertext(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    ciphertext = b"ciphertext"
    outbox.prepare_encrypted(receipt.event_id, "worker-one", ciphertext)

    with pytest.raises(outbox_module.InvalidTransition):
        outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/" + "f" * 64)

    digest = hashlib.sha256(ciphertext).hexdigest()
    uploaded = outbox.mark_uploaded(
        receipt.event_id,
        "worker-one",
        object_key="objects/sha256/" + digest,
    )
    assert uploaded.state is QueueState.OBJECT_UPLOADED


def test_uploaded_size_must_match_prepared_ciphertext(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    ciphertext = b"ciphertext"
    outbox.prepare_encrypted(receipt.event_id, "worker-one", ciphertext)
    digest = hashlib.sha256(ciphertext).hexdigest()

    with pytest.raises(outbox_module.InvalidTransition):
        outbox.mark_uploaded(
            receipt.event_id,
            "worker-one",
            object_key="objects/sha256/" + digest,
            ciphertext_size=999,
        )

    uploaded = outbox.mark_uploaded(
        receipt.event_id,
        "worker-one",
        object_key="objects/sha256/" + digest,
        ciphertext_size=len(ciphertext),
    )
    assert uploaded.ciphertext_size == len(ciphertext)


def test_policy_denial_and_capture_gap_are_explicit_and_bounded(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", max_events=1)
    denied = outbox.enqueue(
        event_id="event-denied",
        session_id="session-synthetic",
        checkpoint=_checkpoint(),
        policy_decision="deny",
        diagnostic_detail="/private/source/secret.json",
    )
    assert denied.state is QueueState.POLICY_DENIED
    assert "/private" not in json.dumps(denied.to_dict())

    full = _enqueue(outbox, "event-full", number=2)
    assert full.state is QueueState.CAPTURE_GAP
    assert isinstance(full.diagnostic, CaptureGap)
    assert full.diagnostic.reason_code == "queue-full"
    assert outbox.inspect_record("event-full") is None
    assert outbox.inspect_record(denied.event_id).state is QueueState.POLICY_DENIED


def test_corrupt_record_is_quarantined_without_publicing_raw_bytes(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    _enqueue(outbox, "event-one")
    record_path = next((root / "queue").glob("*.json"))
    record_path.write_bytes(b'{"session_id":"/private/leak","state":')

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"
    assert "/private" not in json.dumps(inspection.to_dict())
    assert list((root / "quarantine").glob("*.json"))


def test_non_object_corrupt_record_is_quarantined_without_raw_error(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    _enqueue(outbox, "event-one")
    next((root / "queue").glob("*.json")).write_text("[]")

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"


def test_inspect_quarantines_corrupt_prepared_records(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    prepared_path = root / "prepared" / "event-one.json"
    prepared_path.parent.mkdir(parents=True)
    prepared_path.write_text("[]")

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "prepared-record-corrupt"
    assert list((root / "quarantine").glob("prepared-corrupt-*.json"))


def test_symlinked_record_is_quarantined_without_reading_target(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    target = tmp_path / "sensitive.json"
    target.write_text('{"session_id":"/private/sensitive"}')
    queue = root / "queue"
    queue.mkdir(parents=True)
    (queue / "event-one.json").symlink_to(target)

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert "/private" not in json.dumps(inspection.to_dict())
    assert target.exists()


def test_symlinked_queue_directory_fails_closed_without_writing_outside_root(tmp_path):
    root = tmp_path / "outbox"
    external = tmp_path / "external"
    external.mkdir()
    root.mkdir()
    try:
        (root / "queue").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    outbox = PccOutbox(root)

    result = _enqueue(outbox, "event-one")

    assert result.state is QueueState.CAPTURE_GAP
    assert result.diagnostic is not None
    assert result.diagnostic.reason_code == "storage-unavailable"
    assert list(external.iterdir()) == []


@pytest.mark.parametrize(
    "stage",
    [
        QueueState.CLAIMED,
        QueueState.SOURCE_SNAPSHOTTED,
        QueueState.PREPARED_ENCRYPTED,
        QueueState.OBJECT_UPLOADED,
        QueueState.INDEX_PUBLISHED,
    ],
)
def test_stale_owner_takeover_covers_every_owned_progress_stage(tmp_path, stage):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    if stage in {QueueState.SOURCE_SNAPSHOTTED, QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
        outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    if stage in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
        outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
    if stage in {QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
        outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979")
    if stage is QueueState.INDEX_PUBLISHED:
        outbox.publish_index(receipt.event_id, "worker-one", index_id="index-one")

    now[0] = 200.0
    taken = outbox.takeover(receipt.event_id, "worker-two")

    assert taken.state is stage
    assert taken.owner == "worker-two"
    with pytest.raises(LeaseConflict):
        outbox.commit(receipt.event_id, "worker-one")


def test_live_owner_cannot_be_taken_over_at_any_progress_stage(tmp_path):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)

    with pytest.raises(LeaseConflict):
        outbox.takeover(receipt.event_id, "worker-two")


def test_commit_clears_ownership_and_lease(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
    outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979")
    outbox.publish_index(receipt.event_id, "worker-one", index_id="index-one")

    committed = outbox.commit(receipt.event_id, "worker-one")

    assert committed.owner is None
    assert committed.lease_until is None


def test_owner_release_preserves_prepared_state_and_allows_immediate_drain(tmp_path):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("run-owner")
    outbox.transition(receipt.event_id, "run-owner", QueueState.SOURCE_SNAPSHOTTED)
    prepared = outbox.prepare_encrypted(receipt.event_id, "run-owner", b"ciphertext")

    released = outbox.release(receipt.event_id, "run-owner")
    assert released.state is QueueState.PREPARED_ENCRYPTED
    assert released.resume_state is QueueState.PREPARED_ENCRYPTED
    assert released.owner is None
    assert released.lease_until is None
    assert released.ciphertext_sha256 == prepared.ciphertext_sha256
    assert released.ciphertext_size == prepared.ciphertext_size
    assert outbox.release(receipt.event_id, "run-owner") == released

    drained = outbox.claim("drain-owner")
    assert drained is not None
    assert drained.state is QueueState.CLAIMED
    assert drained.resume_state is QueueState.PREPARED_ENCRYPTED


def test_release_publication_failure_keeps_owner_for_crash_recovery(tmp_path, monkeypatch):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("run-owner")
    outbox.transition(receipt.event_id, "run-owner", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "run-owner", b"ciphertext")

    def fail_queue_publication(_record):
        raise outbox_module.OutboxStorageError("publication-failed", pending_preserved=True)

    original_publish = outbox._publish_record_unlocked
    monkeypatch.setattr(outbox, "_publish_record_unlocked", fail_queue_publication)
    with pytest.raises(outbox_module.OutboxStorageError):
        outbox.release(receipt.event_id, "run-owner")

    pending = outbox.inspect_record(receipt.event_id)
    assert pending is not None
    assert pending.state is QueueState.PREPARED_ENCRYPTED
    assert pending.resume_state is QueueState.PREPARED_ENCRYPTED
    assert pending.owner == "run-owner"
    monkeypatch.setattr(outbox, "_publish_record_unlocked", original_publish)
    now[0] = 200.0
    recovered = outbox.takeover(receipt.event_id, "drain-owner")
    assert recovered.owner == "drain-owner"
    assert recovered.resume_state is QueueState.PREPARED_ENCRYPTED


def test_release_rejects_live_owner_conflict_without_clearing_lease(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("run-owner")
    outbox.transition(receipt.event_id, "run-owner", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "run-owner", b"ciphertext")

    with pytest.raises(LeaseConflict):
        outbox.release(receipt.event_id, "drain-owner")

    pending = outbox.inspect_record(receipt.event_id)
    assert pending is not None
    assert pending.owner == "run-owner"
    assert pending.lease_until is not None


def test_release_fails_closed_when_prepared_publication_is_missing(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("run-owner")
    outbox.transition(receipt.event_id, "run-owner", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "run-owner", b"ciphertext")
    outbox.prepared._path(receipt.event_id).unlink()

    with pytest.raises(outbox_module.OutboxStorageError):
        outbox.release(receipt.event_id, "run-owner")

    pending = outbox.inspect_record(receipt.event_id)
    assert pending is not None
    assert pending.owner == "run-owner"
    assert pending.resume_state is QueueState.PREPARED_ENCRYPTED


def test_orphan_prepared_record_can_be_reconciled_without_plaintext(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    outbox.prepared.publish(
        outbox_module.PreparedRecord(
            "event-orphan",
            b"opaque-ciphertext",
            {"content_type": "application/vnd.josh.codex-session-segment+json"},
            __import__("hashlib").sha256(b"opaque-ciphertext").hexdigest(),
            len(b"opaque-ciphertext"),
        )
    )

    recovered = outbox.reconcile_prepared(
        "event-orphan",
        session_id="session-synthetic",
        checkpoint=_checkpoint(),
    )

    assert recovered.state is QueueState.PREPARED_ENCRYPTED
    assert outbox.inspect().orphan_prepared == []
    assert outbox.prepared.inspect_record("event-orphan").ciphertext == b"opaque-ciphertext"


def test_parseable_but_inconsistent_state_is_quarantined(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    _enqueue(outbox, "event-one")
    record_path = next((root / "queue").glob("*.json"))
    body = json.loads(record_path.read_text())
    body["owner"] = "attacker"
    body["lease_until"] = 100.0
    record_path.write_text(json.dumps(body))

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"


def test_prepared_record_contains_ciphertext_and_public_safe_metadata_only(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    with pytest.raises(ValueError):
        outbox.prepare_encrypted(
            receipt.event_id,
            "worker-one",
            b"opaque",
            metadata={"source_path": "/private/secret/session.jsonl"},
        )
    outbox.prepare_encrypted(
        receipt.event_id,
        "worker-one",
        b"opaque-ciphertext",
        metadata={"content_sha256": "c" * 64, "content_size": 15},
    )
    body = (tmp_path / "outbox" / "prepared" / f"{receipt.event_id}.json").read_text()
    assert "opaque-ciphertext" not in body
    assert "/private" not in body
    assert "content_sha256" in body


def test_fifty_concurrent_enqueues_are_durable_and_distinct(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")

    def add(number: int):
        return _enqueue(outbox, f"event-{number}", number=number)

    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = list(pool.map(add, range(1, 65)))

    assert all(receipt.state is QueueState.QUEUED for receipt in receipts)
    records = outbox.inspect().records
    assert len(records) == 64
    assert len({record.sequence for record in records}) == 64
    assert [record.sequence for record in records] == sorted(record.sequence for record in records)


def test_fifty_concurrent_enqueue_processes_are_durable(tmp_path):
    root = tmp_path / "outbox"
    script = """
import json
import sys
from pathlib import Path
from josh_room.pcc_outbox import PccOutbox

root, event_id, number = sys.argv[1:]
PccOutbox(Path(root)).enqueue(
    event_id=event_id,
    session_id="session-process",
    checkpoint={
        "source": "codex.transcript",
        "representation": "active-jsonl",
        "start": int(number) - 1,
        "end": int(number),
        "prefix_sha256": "0" * 64,
    },
)
"""

    def add(number: int):
        return subprocess.run(
            [sys.executable, "-c", script, str(root), f"process-{number}", str(number)],
            check=True,
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(add, range(1, 65)))

    records = PccOutbox(root).inspect().records
    assert len(records) == 64
    assert len({record.event_id for record in records}) == 64


def test_atomic_publication_failure_preserves_previous_record_and_returns_gap(tmp_path, monkeypatch):
    outbox = PccOutbox(tmp_path / "outbox")
    _enqueue(outbox, "event-one")
    original_replace = outbox_module.os.replace
    failures = [True]

    def fail_once(source, target):
        if failures:
            failures.pop()
            raise OSError("synthetic disk full at /private/path")
        return original_replace(source, target)

    monkeypatch.setattr(outbox_module.os, "replace", fail_once)
    result = _enqueue(outbox, "event-two", number=2)

    assert result.state is QueueState.CAPTURE_GAP
    assert result.diagnostic.reason_code == "publication-failed"
    assert "private" not in json.dumps(result.to_dict())
    assert outbox.inspect_record("event-one") is not None
    assert outbox.inspect_record("event-two") is None


def test_read_only_publication_failure_does_not_delete_pending_work(tmp_path, monkeypatch):
    outbox = PccOutbox(tmp_path / "outbox")
    _enqueue(outbox, "event-one")
    original_fsync = outbox_module.os.fsync

    def fail_sync(_descriptor):
        raise OSError("synthetic read-only filesystem")

    monkeypatch.setattr(outbox_module.os, "fsync", fail_sync)
    result = _enqueue(outbox, "event-two", number=2)
    monkeypatch.setattr(outbox_module.os, "fsync", original_fsync)

    assert result.state is QueueState.CAPTURE_GAP
    assert outbox.inspect_record("event-one") is not None


def test_windows_lock_contract_is_explicit(tmp_path, monkeypatch):
    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self):
            self.calls = []

        def locking(self, descriptor, mode, size):
            self.calls.append((descriptor, mode, size))

    windows = FakeMsvcrt()
    monkeypatch.setattr(outbox_module, "_fcntl", None)
    monkeypatch.setattr(outbox_module, "_msvcrt", windows)
    lock_path = tmp_path / "queue.lock"

    with outbox_module._exclusive_file_lock(lock_path):
        assert windows.calls[0][1:] == (windows.LK_LOCK, 1)

    assert windows.calls[-1][1:] == (windows.LK_UNLCK, 1)
    assert lock_path.read_bytes() == b"\0"


def test_windows_publication_contract_does_not_use_posix_directory_fsync(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(outbox_module.os, "fsync", lambda descriptor: called.append(descriptor))

    outbox_module._sync_directory(tmp_path, platform_name="nt")

    assert called == []


def test_publication_falls_back_when_fchmod_is_unavailable(tmp_path, monkeypatch):
    if not hasattr(outbox_module.os, "fchmod"):
        pytest.skip("fchmod is already unavailable")
    monkeypatch.delattr(outbox_module.os, "fchmod")

    result = _enqueue(PccOutbox(tmp_path / "outbox"), "event-one")

    assert result.state is QueueState.QUEUED


def test_reordered_final_signals_use_checkpoint_identity_not_timestamp(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    _enqueue(outbox, "segment-later", number=2)
    _enqueue(outbox, "final-earlier", number=1, final=True)

    records = outbox.inspect().records
    assert [record.checkpoint["end"] for record in records] == [2, 1]
    assert records[1].is_final is True
    assert records[1].final_event_id == "final-earlier"


def test_invalid_metadata_fails_closed_without_record_or_sensitive_error(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    with pytest.raises(ValueError) as error:
        outbox.enqueue(
            event_id="event-one",
            session_id="session-synthetic",
            checkpoint=_checkpoint(),
            metadata={"tool_output": "Authorization: Bearer synthetic-secret"},
        )
    assert "synthetic-secret" not in str(error.value)
    assert outbox.inspect().records == []


def test_inspect_reports_partial_temp_files_without_treating_them_as_records(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    (root / "queue").mkdir(parents=True)
    (root / "queue" / ".event.partial").write_bytes(b"partial")

    inspection = outbox.inspect()

    assert inspection.records == []
    assert inspection.partial_count == 1


def test_claim_contention_allows_only_one_live_owner(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(outbox.claim, [f"worker-{number}" for number in range(8)]))

    successful = [claim for claim in claims if claim is not None]
    assert len(successful) == 1
    assert successful[0].event_id == receipt.event_id


def test_retry_and_quarantine_are_idempotent_after_publication_uncertainty(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")

    first_retry = outbox.retry(receipt.event_id, "worker-one", reason_code="temporary")
    second_retry = outbox.retry(receipt.event_id, "worker-one", reason_code="temporary")
    assert second_retry == first_retry

    outbox.claim("worker-two")
    first_quarantine = outbox.quarantine(receipt.event_id, "worker-two", reason_code="unsafe-input")
    second_quarantine = outbox.quarantine(receipt.event_id, "worker-two", reason_code="unsafe-input")
    assert second_quarantine == first_quarantine


def test_failure_at_each_queue_transition_keeps_last_durable_state(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")

    for state, expected in [
        (QueueState.SOURCE_SNAPSHOTTED, QueueState.CLAIMED),
        (QueueState.PREPARED_ENCRYPTED, QueueState.SOURCE_SNAPSHOTTED),
        (QueueState.OBJECT_UPLOADED, QueueState.PREPARED_ENCRYPTED),
        (QueueState.INDEX_PUBLISHED, QueueState.OBJECT_UPLOADED),
        (QueueState.COMMITTED, QueueState.INDEX_PUBLISHED),
    ]:
        original_publish = outbox._publisher.publish
        failed = [True]

        def fail_once(path, body, _failed=failed, _original_publish=original_publish):
            if _failed:
                _failed.pop()
                raise outbox_module.OutboxStorageError("publication-failed")
            return _original_publish(path, body)

        outbox._publisher.publish = fail_once
        with pytest.raises(outbox_module.OutboxStorageError):
            if state is QueueState.PREPARED_ENCRYPTED:
                outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
            elif state is QueueState.OBJECT_UPLOADED:
                outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979")
            elif state is QueueState.INDEX_PUBLISHED:
                outbox.publish_index(receipt.event_id, "worker-one", index_id="index-one")
            elif state is QueueState.COMMITTED:
                outbox.commit(receipt.event_id, "worker-one")
            else:
                outbox.transition(receipt.event_id, "worker-one", state)
        outbox._publisher.publish = original_publish
        assert outbox.inspect_record(receipt.event_id).state is expected
        if expected is QueueState.CLAIMED:
            outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
        elif expected is QueueState.SOURCE_SNAPSHOTTED:
            outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
        elif expected is QueueState.PREPARED_ENCRYPTED:
            outbox.mark_uploaded(receipt.event_id, "worker-one", object_key="objects/sha256/305531dcc50ebca31cf1d5b31e9fc76ed51f66b3b6dd5a030c6539ae6532f979")
        elif expected is QueueState.OBJECT_UPLOADED:
            outbox.publish_index(receipt.event_id, "worker-one", index_id="index-one")


def test_corrupt_prepared_record_is_explicitly_quarantined(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    prepared_path = root / "prepared" / "event-one.json"
    prepared_path.parent.mkdir(parents=True)
    prepared_path.write_bytes(b'{"event_id":"event-one","ciphertext_b64":"not-base64"}')

    with pytest.raises(outbox_module.OutboxStorageError) as error:
        outbox.prepared.inspect_record("event-one")

    assert error.value.code == "prepared-record-corrupt"
    assert list((root / "quarantine").glob("*.json"))


def test_quota_by_bytes_preserves_existing_pending_records(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", max_bytes=1)
    result = _enqueue(outbox, "event-one")

    assert result.state is QueueState.CAPTURE_GAP
    assert result.diagnostic.pending_preserved is False
    assert outbox.inspect().records == []


def test_final_marker_is_persisted_when_event_id_quota_is_full(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox")
    for number in range(64):
        _enqueue(outbox, f"event-{number}")

    result = _enqueue(outbox, "event-final", final=True)

    record = outbox.inspect_record("event-0")
    assert result.state is QueueState.CAPTURE_GAP
    assert result.diagnostic.reason_code == "event-id-quota"
    assert result.diagnostic.pending_preserved is True
    assert record is not None
    assert record.is_final is True
    assert record.final_event_id == "event-final"
    assert record.event_id in record.event_ids
    assert "event-final" in record.event_ids
    assert len(record.event_ids) == 64


@pytest.mark.parametrize("clock_value", [math.nan, math.inf, -math.inf, -100.0])
def test_non_finite_clock_values_fail_closed(tmp_path, clock_value):
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: clock_value)
    _enqueue(outbox, "event-one")

    with pytest.raises(outbox_module.OutboxStorageError) as error:
        outbox.claim("worker-one")

    assert error.value.code == "clock-invalid"


def test_extreme_persisted_lease_is_quarantined_without_float_overflow(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    record_path = root / "queue" / f"{receipt.event_id}.json"
    body = json.loads(record_path.read_text())
    body["lease_until"] = 10**1000
    record_path.write_text(json.dumps(body))

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"


def test_non_finite_persisted_lease_is_quarantined(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    record_path = root / "queue" / f"{receipt.event_id}.json"
    body = json.loads(record_path.read_text())
    body["lease_until"] = math.nan
    record_path.write_text(json.dumps(body))

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"


def test_reconcile_prepared_recovers_after_stale_owner_crash(tmp_path):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")
    now[0] = 200.0

    recovered = outbox.reconcile_prepared(
        receipt.event_id,
        session_id="session-synthetic",
        checkpoint=_checkpoint(),
    )

    assert recovered.state is QueueState.PREPARED_ENCRYPTED
    assert recovered.owner is None
    assert recovered.lease_until is None


def test_live_owner_blocks_prepared_reconciliation(tmp_path):
    now = [100.0]
    outbox = PccOutbox(tmp_path / "outbox", clock=lambda: now[0], lease_seconds=10)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")

    with pytest.raises(LeaseConflict):
        outbox.reconcile_prepared(
            receipt.event_id,
            session_id="session-synthetic",
            checkpoint=_checkpoint(),
        )


def test_prepared_bytes_count_toward_quota_without_deleting_pending_work(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", max_bytes=10_000)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    queue_path = tmp_path / "outbox" / "queue" / "event-one.json"
    outbox.max_bytes = queue_path.stat().st_size + 1

    result = outbox.prepare_encrypted(receipt.event_id, "worker-one", b"ciphertext")

    assert result.state is QueueState.CAPTURE_GAP
    assert result.failure_code == "prepared-byte-quota"
    assert outbox.prepared.inspect_record(receipt.event_id) is None
    pending = outbox.inspect_record(receipt.event_id)
    assert pending is not None
    assert pending.state is QueueState.CAPTURE_GAP
    assert pending.resume_state is QueueState.SOURCE_SNAPSHOTTED


def test_existing_prepared_ciphertext_counts_toward_quota(tmp_path):
    outbox = PccOutbox(tmp_path / "outbox", max_bytes=100_000)
    first = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(first.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(first.event_id, "worker-one", b"existing-ciphertext")

    second = _enqueue(outbox, "event-two", number=2)
    outbox.claim("worker-two")
    outbox.transition(second.event_id, "worker-two", QueueState.SOURCE_SNAPSHOTTED)
    second_record = outbox.inspect_record(second.event_id)
    assert second_record is not None
    new_ciphertext = b"new-ciphertext"
    prepared = outbox_module.PreparedRecord(
        second.event_id,
        new_ciphertext,
        {},
        hashlib.sha256(new_ciphertext).hexdigest(),
        len(new_ciphertext),
    )
    updated = outbox_module.QueueRecord(
        **{
            **second_record.__dict__,
            "state": QueueState.PREPARED_ENCRYPTED,
            "resume_state": QueueState.PREPARED_ENCRYPTED,
            "failure_code": None,
        }
    )
    encoded_prepared = json.dumps(prepared.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    encoded_updated = json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    queue_one = (tmp_path / "outbox" / "queue" / "event-one.json").stat().st_size
    prepared_one = (tmp_path / "outbox" / "prepared" / "event-one.json").stat().st_size
    outbox.max_bytes = queue_one + len(encoded_updated) + len(encoded_prepared) + prepared_one - 1

    result = outbox.prepare_encrypted(second.event_id, "worker-two", new_ciphertext)

    assert result.state is QueueState.CAPTURE_GAP
    assert result.failure_code == "prepared-byte-quota"
    assert outbox.prepared.inspect_record(second.event_id) is None


def test_prepared_temp_files_count_toward_quota(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root, max_bytes=100_000)
    receipt = _enqueue(outbox, "event-one")
    outbox.claim("worker-one")
    outbox.transition(receipt.event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    partial = root / "prepared" / ".crashed.partial"
    partial.write_bytes(b"x" * 200)
    ciphertext = b"new-ciphertext"
    prepared = outbox_module.PreparedRecord(
        receipt.event_id,
        ciphertext,
        {},
        hashlib.sha256(ciphertext).hexdigest(),
        len(ciphertext),
    )
    encoded_prepared = json.dumps(prepared.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    queue_path = root / "queue" / f"{receipt.event_id}.json"
    outbox.max_bytes = queue_path.stat().st_size + len(encoded_prepared) + partial.stat().st_size - 1

    result = outbox.prepare_encrypted(receipt.event_id, "worker-one", ciphertext)

    assert result.state is QueueState.CAPTURE_GAP
    assert result.failure_code == "prepared-byte-quota"
    assert partial.exists()


def test_post_replace_directory_sync_failure_reports_visible_record(tmp_path, monkeypatch):
    outbox = PccOutbox(tmp_path / "outbox")
    _enqueue(outbox, "event-prime")

    def fail_directory_sync(_directory):
        raise OSError("synthetic directory sync failure")

    monkeypatch.setattr(outbox_module, "_sync_directory", fail_directory_sync)
    result = _enqueue(outbox, "event-one", number=2)

    assert result.state is QueueState.CAPTURE_GAP
    assert result.diagnostic.reason_code == "publication-failed"
    assert result.diagnostic.pending_preserved is True
    assert outbox.inspect_record("event-one") is not None


def test_inspect_reports_orphan_publisher_temp_files(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    (root / "queue").mkdir(parents=True)
    (root / "queue" / ".event-one.json.deadbeef").write_bytes(b"partial")

    inspection = outbox.inspect()

    assert inspection.records == []
    assert inspection.partial_count == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(event_ids=["event-other"]),
        lambda body: body.update(is_final=True, final_event_id=None),
    ],
)
def test_inconsistent_event_identity_state_is_quarantined(tmp_path, mutate):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    _enqueue(outbox, "event-one")
    record_path = root / "queue" / "event-one.json"
    body = json.loads(record_path.read_text())
    mutate(body)
    record_path.write_text(json.dumps(body))

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 1
    assert inspection.diagnostics[0].code == "corrupt-record"


def test_malformed_prepared_digest_is_quarantined_without_type_error(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    prepared_path = root / "prepared" / "event-one.json"
    prepared_path.parent.mkdir(parents=True)
    prepared_path.write_text(
        json.dumps(
            {
                "event_id": "event-one",
                "ciphertext_b64": "Y2lwaGVydGV4dA==",
                "ciphertext_sha256": None,
                "ciphertext_size": 10,
                "metadata": {},
            }
        )
    )

    with pytest.raises(outbox_module.OutboxStorageError) as error:
        outbox.prepared.inspect_record("event-one")

    assert error.value.code == "prepared-record-corrupt"
    assert list((root / "quarantine").glob("prepared-corrupt-*.json"))


def test_dangling_queue_and_prepared_symlinks_are_quarantined(tmp_path):
    root = tmp_path / "outbox"
    outbox = PccOutbox(root)
    (root / "queue").mkdir(parents=True)
    (root / "prepared").mkdir(parents=True)
    (root / "queue" / "event-one.json").symlink_to(tmp_path / "missing-queue.json")
    (root / "prepared" / "event-two.json").symlink_to(tmp_path / "missing-prepared.json")

    inspection = outbox.inspect()

    assert inspection.quarantined_count == 2
    assert not (root / "queue" / "event-one.json").exists()
    assert not (root / "prepared" / "event-two.json").exists()
    assert len(list((root / "quarantine").glob("*.json"))) == 2


def test_posix_directory_sync_is_explicit(tmp_path, monkeypatch):
    calls = []
    original_open = outbox_module.os.open

    def track_open(path, flags):
        calls.append((path, flags))
        return original_open(path, flags)

    monkeypatch.setattr(outbox_module.os, "open", track_open)
    monkeypatch.setattr(outbox_module.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor)))
    monkeypatch.setattr(outbox_module.os, "close", lambda descriptor: calls.append(("close", descriptor)))

    outbox_module._sync_directory(tmp_path, platform_name="posix")

    assert calls[0][0] == tmp_path
    assert calls[1][0] == "fsync"
    assert calls[2][0] == "close"
