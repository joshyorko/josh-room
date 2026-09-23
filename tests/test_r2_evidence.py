import hashlib
import io
import threading

import pytest
from botocore.exceptions import ClientError

from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.r2 import (
    EVIDENCE_CLAIM_PREFIX,
    EVIDENCE_INDEX_PREFIX,
    EVIDENCE_OBJECT_PREFIX,
    R2Backend,
    R2Config,
    R2EvidenceAbortFailure,
    R2EvidenceConflict,
    R2EvidenceError,
    R2EvidenceReadbackMismatch,
    evidence_claim_key,
    evidence_index_key,
    evidence_object_key,
    validate_evidence_claim_key,
    validate_evidence_object_key,
)


@pytest.fixture(autouse=True)
def _local_only_evidence(monkeypatch):
    """Direct fake-provider contracts do not represent production R2 authority."""
    monkeypatch.setattr("josh_room.device.require_prepare_upload", lambda: None)


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "Evidence")


class EvidenceS3:
    def __init__(self):
        self.objects = {}
        self.multipart = {}
        self.calls = []
        self.next_upload = 0
        self.put_errors = []
        self.part_errors = []
        self.complete_error = None
        self.abort_error = None
        self.list_pages = None
        self.lock = threading.Lock()

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        with self.lock:
            if self.put_errors:
                raise self.put_errors.pop(0)
            key = kwargs["Key"]
            if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
                raise error("412")
            body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
            self.objects[key] = {"body": body, "metadata": kwargs.get("Metadata", {})}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if kwargs["Key"] not in self.objects:
            raise error("404")
        body = self.objects[kwargs["Key"]]["body"]
        return {"ContentLength": len(body), "ETag": '"not-a-digest"'}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        body = self.objects[kwargs["Key"]]["body"]
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        self.next_upload += 1
        upload_id = f"upload-{self.next_upload}"
        self.multipart[upload_id] = {"key": kwargs["Key"], "parts": {}}
        return {"UploadId": upload_id}
    def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        if getattr(self, "crash_on_part", False):
            self.crash_on_part = False
            raise KeyboardInterrupt()
        if self.part_errors:
            raise self.part_errors.pop(0)
        body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
        self.multipart[kwargs["UploadId"]]["parts"][kwargs["PartNumber"]] = body
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        upload = self.multipart[kwargs["UploadId"]]
        body = b"".join(upload["parts"][part] for part in sorted(upload["parts"]))
        if kwargs.get("IfNoneMatch") == "*" and upload["key"] in self.objects:
            raise error("412")
        if self.complete_error == "ambiguous":
            self.objects[upload["key"]] = {"body": body}
            self.complete_error = None
            raise TimeoutError("ambiguous")
        if self.complete_error:
            error_value = self.complete_error
            self.complete_error = None
            raise error_value
        self.objects[upload["key"]] = {"body": body}
        del self.multipart[kwargs["UploadId"]]

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))
        if self.abort_error:
            raise self.abort_error
        self.multipart.pop(kwargs["UploadId"], None)

    def list_objects_v2(self, **kwargs):
        self.calls.append(("list_objects_v2", kwargs))
        if self.list_pages is not None:
            return self.list_pages.pop(0)
        keys = sorted(key for key in self.objects if key.startswith(kwargs["Prefix"]))
        start = int(kwargs.get("ContinuationToken", "0"))
        page = keys[start : start + kwargs["MaxKeys"]]
        end = start + kwargs["MaxKeys"]
        response = {"Contents": [{"Key": key, "Size": len(self.objects[key]["body"])} for key in page]}
        if end < len(keys):
            response.update({"IsTruncated": True, "NextContinuationToken": str(end)})
        else:
            response["IsTruncated"] = False
        return response
def backend(fake, threshold=8, chunk=4, receipt_dir=None):
    return R2Backend(
        R2Config("https://example.invalid", "synthetic", "test", multipart_threshold=threshold, multipart_chunk_size=chunk, max_attempts=3),
        client=fake,
        receipt_dir=receipt_dir,
    )


def test_dedicated_validators_are_opaque_and_do_not_change_workspace_keys():
    digest = hashlib.sha256(b"secret repo/project/session").hexdigest()
    object_key = evidence_object_key(digest)
    index_key = evidence_index_key(digest)
    assert object_key.startswith(EVIDENCE_OBJECT_PREFIX)
    assert index_key.startswith(EVIDENCE_INDEX_PREFIX)
    object_claim = evidence_claim_key(digest, object_key)
    index_claim = evidence_claim_key(digest, index_key)
    assert object_claim.startswith(EVIDENCE_CLAIM_PREFIX)
    assert index_claim.startswith(EVIDENCE_CLAIM_PREFIX)
    assert object_claim != index_claim
    assert validate_evidence_claim_key(object_claim) == object_claim.rsplit("/", 1)[1]
    assert validate_evidence_claim_key(index_claim) == index_claim.rsplit("/", 1)[1]
    for forbidden in ("repo", "project", "session", "source", "profile", "device"):
        assert forbidden not in object_key
        assert forbidden not in index_key
        assert forbidden not in object_claim
        assert forbidden not in index_claim
    with pytest.raises(ValueError):
        validate_evidence_object_key("objects/sha256/" + digest)


def test_single_put_duplicate_and_full_readback_ignore_etag(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    source = tmp_path / "ciphertext.age"
    source.write_bytes(b"encrypted evidence")
    source.chmod(0o600)
    first = store.put_evidence_file(source)
    second = store.put_evidence_file(source)
    assert first.key == second.key
    assert second.metrics.duplicate
    assert store.get_evidence_bytes(first.key) == source.read_bytes()
    assert fake.objects[first.key]["body"] == source.read_bytes()


def test_multipart_stream_uses_digest_claim_and_unconditional_completion(tmp_path):
    fake = EvidenceS3()
    store = backend(fake, threshold=2, chunk=4)
    payload = b"0123456789"
    source = tmp_path / "ciphertext.age"
    source.write_bytes(payload)
    source.chmod(0o600)
    receipt = store.put_evidence_file(source)
    complete = next(kwargs for name, kwargs in fake.calls if name == "complete_multipart_upload")
    assert "IfNoneMatch" not in complete
    claim = next(kwargs for name, kwargs in fake.calls if name == "put_object" and kwargs["Key"].startswith(EVIDENCE_CLAIM_PREFIX))
    assert claim["IfNoneMatch"] == "*"
    assert validate_evidence_claim_key(claim["Key"]) == claim["Key"].rsplit("/", 1)[1]
    assert b"repo" not in claim["Body"] and b"session" not in claim["Body"]
    assert receipt.metrics.parts == 3
    assert store.get_evidence_bytes(receipt.key) == payload


def test_multipart_ambiguous_completion_is_verified_without_losing_source(tmp_path):
    fake = EvidenceS3()
    fake.complete_error = "ambiguous"
    store = backend(fake, threshold=2, chunk=4)
    source = tmp_path / "ciphertext.age"
    source.write_bytes(b"0123456789")
    source.chmod(0o600)
    receipt = store.put_evidence_file(source)
    assert receipt.recovery == "ambiguous-complete-verified"
    assert source.read_bytes() == b"0123456789"


def test_conflicting_object_is_not_accepted_after_precondition(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    body = b"correct"
    source = tmp_path / "ciphertext.age"
    source.write_bytes(body)
    source.chmod(0o600)
    key = evidence_object_key(hashlib.sha256(body).hexdigest())
    fake.objects[key] = {"body": b"wrong"}
    with pytest.raises(R2EvidenceConflict):
        store.put_evidence_file(source)


def test_multipart_claim_conflict_is_typed_and_does_not_complete(tmp_path):
    fake = EvidenceS3()
    store = backend(fake, threshold=2, chunk=4)
    payload = b"conflicting payload"
    digest = hashlib.sha256(payload).hexdigest()
    claim_key = evidence_claim_key(digest)
    fake.objects[claim_key] = {
        "body": b'{"fence":"00000000000000000000000000000000","sha256":"' + digest.encode() + b'","size":19,"version":1}'
    }
    source = tmp_path / "ciphertext.age"
    source.write_bytes(payload)
    source.chmod(0o600)
    with pytest.raises(R2EvidenceConflict, match="claim-conflict"):
        store.put_evidence_file(source)
    assert not any(name == "complete_multipart_upload" for name, _ in fake.calls)


def test_multipart_parts_resume_from_bounded_state_after_crash(tmp_path):
    fake = EvidenceS3()
    store = backend(fake, threshold=2, chunk=4, receipt_dir=tmp_path)
    payload = b"0123456789"
    source = tmp_path / "ciphertext.age"
    source.write_bytes(payload)
    source.chmod(0o600)
    fake.crash_on_part = True
    with pytest.raises(KeyboardInterrupt):
        store.put_evidence_file(source)
    assert list((tmp_path / "evidence-multipart").glob("*.json"))
    receipt = store.put_evidence_file(source)
    assert receipt.ciphertext_size == len(payload)
    assert store.get_evidence_bytes(receipt.key) == payload


def test_concurrent_claim_fence_allows_distinct_digests_and_one_same_key(tmp_path):
    fake = EvidenceS3()
    barrier = threading.Barrier(2)
    payload = b"concurrent payload"
    results = []
    failures = []

    def publish(index, body):
        source = tmp_path / f"source-{index}.age"
        source.write_bytes(body)
        source.chmod(0o600)
        store = backend(fake, threshold=2, chunk=4, receipt_dir=tmp_path / f"state-{index}")
        barrier.wait()
        try:
            results.append(store.put_evidence_file(source))
        except R2EvidenceConflict as error:
            failures.append(error)

    threads = [threading.Thread(target=publish, args=(index, payload)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) + len(failures) == 2
    assert results
    assert all(item.key == results[0].key for item in results)
    assert len([call for call in fake.calls if call[0] == "complete_multipart_upload"]) == 1


def test_multipart_state_symlink_is_rejected(tmp_path):
    fake = EvidenceS3()
    store = backend(fake, threshold=2, chunk=4, receipt_dir=tmp_path / "state")
    payload = b"state symlink"
    source = tmp_path / "ciphertext.age"
    source.write_bytes(payload)
    source.chmod(0o600)
    digest = hashlib.sha256(payload).hexdigest()
    key = evidence_object_key(digest)
    state_path = store._evidence_state_path(digest, key)
    state_path.parent.mkdir(mode=0o700, parents=True)
    target = state_path.with_suffix(".target")
    target.write_text("{}")
    state_path.symlink_to(target)
    with pytest.raises(R2EvidenceError):
        store.put_evidence_file(source)

def test_abort_failure_is_typed_and_source_remains_durable(tmp_path):
    fake = EvidenceS3()
    fake.part_errors = [error("500"), error("500"), error("500")]
    fake.abort_error = RuntimeError("abort unavailable")
    store = backend(fake, threshold=2, chunk=4)
    source = tmp_path / "ciphertext.age"
    source.write_bytes(b"0123456789")
    source.chmod(0o600)
    with pytest.raises(R2EvidenceAbortFailure):
        store.put_evidence_file(source)
    assert source.exists()


def test_paginated_duplicate_safe_index_discovery_has_fixed_prefix_and_no_order_dependency():
    fake = EvidenceS3()
    store = backend(fake)
    first = store.put_evidence_index_bytes(b"index-one")
    second = store.put_evidence_index_bytes(b"index-two")
    fake.list_pages = [
        {"Contents": [{"Key": first.key, "Size": first.ciphertext_size}], "IsTruncated": True, "NextContinuationToken": "next"},
        {"Contents": [{"Key": first.key, "Size": first.ciphertext_size}, {"Key": second.key, "Size": second.ciphertext_size}], "IsTruncated": False},
    ]
    discovered = store.discover_evidence_indexes(page_size=1)
    assert {item.key for item in discovered} == {first.key, second.key}
    assert all(item.key.startswith(EVIDENCE_INDEX_PREFIX) for item in discovered)
    assert all(call[1]["Prefix"] == EVIDENCE_INDEX_PREFIX for call in fake.calls if call[0] == "list_objects_v2")


def test_index_discovery_reports_page_cap_after_skipped_keys():
    fake = EvidenceS3()
    fake.list_pages = [
        {
            "Contents": [{"Key": "not-an-index", "Size": 1}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
    ]

    with pytest.raises(R2EvidenceError) as failure:
        backend(fake).discover_evidence_indexes(max_events=2, page_size=1, max_pages=1)

    assert failure.value.code == "index-discovery-incomplete"

def test_index_discovery_reports_malformed_valid_index_size_as_incomplete():
    fake = EvidenceS3()
    store = backend(fake)
    digest = hashlib.sha256(b"synthetic index").hexdigest()
    fake.list_pages = [
        {
            "Contents": [{
                "Key": evidence_index_key(digest),
                "Size": store.config.max_bytes + 1,
            }],
            "IsTruncated": False,
        },
    ]

    with pytest.raises(R2EvidenceError) as failure:
        store.discover_evidence_indexes(max_events=2, page_size=1, max_pages=1)

    assert failure.value.code == "index-discovery-incomplete"

def test_outbox_uploaded_indexed_committed_and_unindexed_recovery(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    outbox = PccOutbox(tmp_path / "outbox")
    event_id = "event-one"
    checkpoint = {"source": "synthetic", "representation": "active-jsonl", "start": 0, "end": 1, "prefix_sha256": "a" * 64}
    outbox.enqueue(event_id=event_id, session_id="session-one", checkpoint=checkpoint, metadata={"object_kind": "session-segment"})
    outbox.claim("worker-one")
    outbox.transition(event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(event_id, "worker-one", b"durable ciphertext", metadata={"object_kind": "session-segment"})
    uploaded = store.publish_outbox_evidence(outbox, event_id, "worker-one", index_ciphertext=None)
    assert not uploaded.committed
    assert uploaded.evidence.metrics.orphaned
    assert uploaded.evidence.recovery == "uploaded-unindexed"
    assert outbox.inspect_record(event_id).state is QueueState.OBJECT_UPLOADED
    committed = store.publish_outbox_evidence(outbox, event_id, "worker-one", index_ciphertext=b"independently encrypted index")
    assert committed.committed
    assert outbox.inspect_record(event_id).state is QueueState.COMMITTED
def test_index_published_recovery_republishes_missing_supplied_index(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    outbox = PccOutbox(tmp_path / "outbox")
    event_id = "event-index-recovery"
    checkpoint = {"source": "synthetic", "representation": "active-jsonl", "start": 0, "end": 1, "prefix_sha256": "c" * 64}
    outbox.enqueue(event_id=event_id, session_id="session-index", checkpoint=checkpoint, metadata={"object_kind": "session-segment"})
    outbox.claim("worker-one")
    outbox.transition(event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(event_id, "worker-one", b"durable ciphertext", metadata={"object_kind": "session-segment"})
    store.publish_outbox_evidence(outbox, event_id, "worker-one", index_ciphertext=None)
    index_ciphertext = b"recovery index"
    index_id = hashlib.sha256(index_ciphertext).hexdigest()
    outbox.publish_index(event_id, "worker-one", index_id=index_id)
    recovered = store.publish_outbox_evidence(outbox, event_id, "worker-one", index_ciphertext=index_ciphertext)
    assert recovered.committed
    assert outbox.inspect_record(event_id).state is QueueState.COMMITTED

def test_stream_digest_mismatch_does_not_poison_final_key():
    fake = EvidenceS3()
    store = backend(fake)
    with pytest.raises(R2EvidenceReadbackMismatch) as failure:
        store.put_evidence_stream(io.BytesIO(b"actual"), 6, "0" * 64)
    assert not failure.value.published
    assert not fake.objects



def test_readback_mismatch_is_typed_and_not_hidden_by_etag(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    source = tmp_path / "ciphertext.age"
    source.write_bytes(b"expected")
    source.chmod(0o600)
    key = evidence_object_key(hashlib.sha256(source.read_bytes()).hexdigest())
    fake.objects[key] = {"body": b"tampered"}
    with pytest.raises(R2EvidenceReadbackMismatch):
        store.get_evidence_bytes(key, expected_size=len(source.read_bytes()))
@pytest.mark.parametrize("index", [False, True])
def test_evidence_readback_bounds_body_read_by_declared_size_plus_one(index):
    fake = EvidenceS3()
    store = backend(fake)
    payload = b"bounded encrypted evidence"
    if index:
        receipt = store.put_evidence_index_bytes(payload)
    else:
        receipt = store.put_evidence_stream(
            io.BytesIO(payload),
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )

    read_sizes = []

    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            read_sizes.append(size)
            return super().read(size)

    get_object = fake.get_object

    def tracked_get(**kwargs):
        response = get_object(**kwargs)
        response["Body"] = BoundedBody(response["Body"].read())
        return response

    fake.get_object = tracked_get
    if index:
        body = store.get_evidence_index_bytes(receipt.key, expected_size=receipt.ciphertext_size)
    else:
        body = store.get_evidence_bytes(receipt.key, expected_size=receipt.ciphertext_size)

    assert body == payload
    assert read_sizes == [len(payload) + 1]



def test_outbox_retry_from_uploaded_stage_does_not_rewind_or_repeat_mark_uploaded(tmp_path):
    fake = EvidenceS3()
    store = backend(fake)
    outbox = PccOutbox(tmp_path / "outbox")
    event_id = "event-retry"
    checkpoint = {"source": "synthetic", "representation": "active-jsonl", "start": 0, "end": 1, "prefix_sha256": "b" * 64}
    outbox.enqueue(event_id=event_id, session_id="session-retry", checkpoint=checkpoint, metadata={"object_kind": "session-segment"})
    outbox.claim("worker-one")
    outbox.transition(event_id, "worker-one", QueueState.SOURCE_SNAPSHOTTED)
    outbox.prepare_encrypted(event_id, "worker-one", b"durable retry ciphertext", metadata={"object_kind": "session-segment"})
    store.publish_outbox_evidence(outbox, event_id, "worker-one", index_ciphertext=None)
    outbox.retry(event_id, "worker-one", reason_code="index-timeout")
    outbox.claim("worker-two")
    result = store.publish_outbox_evidence(outbox, event_id, "worker-two", index_ciphertext=b"retry index ciphertext")
    assert result.committed
    assert outbox.inspect_record(event_id).state is QueueState.COMMITTED
