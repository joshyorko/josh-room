import hashlib
import io

import pytest
from botocore.exceptions import ClientError

from josh_room.r2 import R2Backend, R2Config, R2Conflict


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutObject")


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.multipart = {}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise error("PreconditionFailed")
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != self.objects.get(key, {}).get("etag"):
            raise error("PreconditionFailed")
        body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
        self.objects[key] = {"body": body, "etag": '"etag"', "metadata": kwargs.get("Metadata", {})}
        return {"ETag": '"etag"'}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        if kwargs["Key"] not in self.objects:
            raise error("404")
        item = self.objects[kwargs["Key"]]
        return {"ContentLength": len(item["body"]), "ETag": item["etag"], "Metadata": item["metadata"]}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        item = self.objects[kwargs["Key"]]
        return {"ContentLength": len(item["body"]), "Body": io.BytesIO(item["body"])}

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        upload_id = "upload-1"
        self.multipart[upload_id] = {"key": kwargs["Key"], "parts": []}
        return {"UploadId": upload_id}

    def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
        self.multipart[kwargs["UploadId"]]["parts"].append(body)
        return {"ETag": f'"part-{kwargs["PartNumber"]}"'}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        upload = self.multipart[kwargs["UploadId"]]
        if kwargs.get("IfNoneMatch") == "*" and upload["key"] in self.objects:
            raise error("PreconditionFailed")
        body = b"".join(upload["parts"])
        self.objects[upload["key"]] = {"body": body, "etag": '"etag"', "metadata": {}}
        return {"ETag": '"etag"'}

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))
        self.multipart.pop(kwargs["UploadId"], None)

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)


class ChunkLimitedBody(io.BytesIO):
    def __init__(self, body, limit):
        super().__init__(body)
        self.limit = limit
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        if size < 0 or size > self.limit:
            raise AssertionError("verification attempted an unbounded read")
        return super().read(size)


def backend(fake, tmp_path, threshold=8):
    return R2Backend(R2Config("https://example.invalid", "synthetic", "test", multipart_threshold=threshold, multipart_chunk_size=4), client=fake, receipt_dir=tmp_path)


def test_small_object_is_conditional_and_read_back_verified(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path)
    body = b"small"
    ref = store.put_bytes("objects/sha256/" + hashlib.sha256(body).hexdigest(), body)
    assert ref.size == len(body)
    assert any(call[0] == "get_object" for call in fake.calls)
    assert dict(fake.calls[0][1])["IfNoneMatch"] == "*"


def test_existing_immutable_object_is_not_overwritten(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path)
    body = b"same"
    key = "objects/sha256/" + hashlib.sha256(body).hexdigest()
    store.put_bytes(key, body)
    assert store.put_bytes(key, body).sha256 == hashlib.sha256(body).hexdigest()
    assert len([call for call in fake.calls if call[0] == "put_object"]) == 2


def test_multipart_upload_completes_conditionally(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path, threshold=2)
    body = b"abcdefgh"
    key = "objects/sha256/" + hashlib.sha256(body).hexdigest()
    store.put_bytes(key, body)
    complete = next(kwargs for name, kwargs in fake.calls if name == "complete_multipart_upload")
    assert complete["IfNoneMatch"] == "*"
    assert len([call for call in fake.calls if call[0] == "upload_part"]) == 2


def test_multipart_failure_aborts_and_writes_no_object(tmp_path):
    fake = FakeS3()
    original = fake.upload_part

    def fail(**kwargs):
        if kwargs["PartNumber"] == 2:
            raise RuntimeError("synthetic part failure")
        return original(**kwargs)

    fake.upload_part = fail
    store = backend(fake, tmp_path, threshold=2)
    body = b"abcdefgh"
    key = "objects/sha256/" + hashlib.sha256(body).hexdigest()
    with pytest.raises(RuntimeError):
        store.put_bytes(key, body)
    assert any(name == "abort_multipart_upload" for name, _ in fake.calls)
    assert fake.objects == {}


def test_catalog_conflict_is_explicit(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path)
    with pytest.raises(R2Conflict):
        store.conditional_catalog_put(b"catalog", expected_etag='"wrong"')


def test_catalog_create_only_and_conditional_update_read_back(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path)
    etag = store.conditional_catalog_put(b"one", expected_etag=None)
    assert store.read_catalog()[0] == b"one"
    store.conditional_catalog_put(b"two", expected_etag=etag)
    assert store.read_catalog()[0] == b"two"


def test_remote_verification_hashes_in_bounded_chunks(tmp_path):
    fake = FakeS3()
    body = b"x" * (2 * 1024 * 1024)
    digest = hashlib.sha256(body).hexdigest()
    key = "objects/sha256/" + digest
    stream = ChunkLimitedBody(body, 1024 * 1024)
    fake.objects[key] = {"body": body, "etag": '"etag"', "metadata": {"sha256": digest}}
    fake.get_object = lambda **_kwargs: {"ContentLength": len(body), "Body": stream}
    backend(fake, tmp_path)._verify_remote(key, digest, len(body))
    assert len(stream.requests) >= 2


def test_delete_object_accepts_only_strict_content_addressed_keys(tmp_path):
    fake = FakeS3()
    store = backend(fake, tmp_path)
    body = b"delete-me"
    ref = store.put_bytes("objects/sha256/" + hashlib.sha256(body).hexdigest(), body)

    store.delete_object(ref.key)

    assert ref.key not in fake.objects
    with pytest.raises(ValueError):
        store.delete_object("catalog.jroom.age")
