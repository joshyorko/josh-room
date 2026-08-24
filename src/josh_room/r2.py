import hashlib
import io
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import truststore

truststore.inject_into_ssl()

from botocore.exceptions import ClientError

from .keyring import lookup
from .local_store import ObjectRef
from .progress import report_progress

OBJECT_KEY = re.compile(r"^objects/sha256/([0-9a-f]{64})$")


class R2Conflict(RuntimeError):
    pass


@dataclass(frozen=True)
class R2Config:
    endpoint: str
    bucket: str
    credential_profile: str
    region: str = "auto"
    catalog_key: str = "catalog.jroom.age"
    multipart_threshold: int = 64 * 1024 * 1024
    multipart_chunk_size: int = 16 * 1024 * 1024
    max_bytes: int = 8 * 1024 * 1024 * 1024
    timeout_seconds: int = 60
    max_attempts: int = 4

    @classmethod
    def from_private(cls, config: dict) -> "R2Config":
        values = config.get("r2") if config else None
        if not values:
            raise ValueError("private R2 configuration is unavailable")
        return cls(endpoint=values["endpoint"], bucket=values["bucket"], credential_profile=values["credential_profile"], region=values.get("region", "auto"), catalog_key=values.get("catalog_key", "catalog.jroom.age"))


class R2Backend:
    def __init__(self, config: R2Config, client=None, receipt_dir: Path | None = None):
        self.config = config
        self.client = client or self._client_from_keyring()
        self.receipt_dir = Path(receipt_dir) if receipt_dir else None
        if "/" in config.catalog_key or config.catalog_key.startswith("."):
            raise ValueError("catalog key must be fixed and opaque")

    def _client_from_keyring(self):
        import boto3
        from botocore.config import Config

        credentials = lookup(self.config.credential_profile)
        return boto3.client(
            "s3",
            endpoint_url=self.config.endpoint,
            region_name=self.config.region,
            aws_access_key_id=credentials["access-key-id"],
            aws_secret_access_key=credentials["secret-access-key"],
            aws_session_token=credentials.get("session-token"),
            config=Config(
                connect_timeout=self.config.timeout_seconds,
                read_timeout=self.config.timeout_seconds,
                retries={"max_attempts": self.config.max_attempts, "mode": "standard"},
            ),
        )

    def put_bytes(self, key: str, body: bytes) -> ObjectRef:
        digest = hashlib.sha256(body).hexdigest()
        self._validate_object_key(key, digest)
        if len(body) > self.config.max_bytes:
            raise ValueError("object exceeds maximum size")
        return self._put_stream(key, io.BytesIO(body), len(body), digest)

    def put_file(self, key: str, path: Path) -> ObjectRef:
        size = path.stat().st_size
        if size > self.config.max_bytes:
            raise ValueError("object exceeds maximum size")
        digest = _file_digest(path)
        self._validate_object_key(key, digest)
        with path.open("rb") as source:
            return self._put_stream(key, source, size, digest)

    def _put_stream(self, key: str, source, size: int, digest: str) -> ObjectRef:
        if size < self.config.multipart_threshold:
            try:
                self.client.put_object(Bucket=self.config.bucket, Key=key, Body=source, ContentLength=size, IfNoneMatch="*", Metadata={"sha256": digest})
            except ClientError as error:
                if not _is_precondition(error):
                    raise
                self._verify_remote(key, digest, size)
                return ObjectRef(key, digest, size)
        else:
            upload_id = self.client.create_multipart_upload(Bucket=self.config.bucket, Key=key, Metadata={"sha256": digest})["UploadId"]
            parts = []
            try:
                part_number = 1
                while True:
                    chunk = source.read(self.config.multipart_chunk_size)
                    if not chunk:
                        break
                    parts.append({"ETag": self.client.upload_part(Bucket=self.config.bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=chunk)["ETag"], "PartNumber": part_number})
                    uploaded = min(part_number * self.config.multipart_chunk_size, size)
                    report_progress("upload", f"Uploading encrypted Room • {_percent(uploaded, size)}%", current=uploaded, total=size)
                    part_number += 1
                try:
                    self.client.complete_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}, IfNoneMatch="*")
                except ClientError as error:
                    if not _is_precondition(error):
                        raise
                    self._verify_remote(key, digest, size)
                    return ObjectRef(key, digest, size)
            except BaseException:  # noqa: BLE001 - abort multipart on cancellation as well as SDK errors
                try:
                    self.client.abort_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id)
                finally:
                    raise
        self._verify_remote(key, digest, size)
        return ObjectRef(key, digest, size)

    def get_bytes(self, key: str, expected_digest: str | None = None, expected_size: int | None = None) -> bytes:
        self._validate_object_key(key, expected_digest)
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        size = int(response.get("ContentLength", 0))
        if size > self.config.max_bytes or expected_size is not None and size != expected_size:
            raise ValueError("remote object size mismatch")
        body = response["Body"].read(self.config.max_bytes + 1)
        digest = hashlib.sha256(body).hexdigest()
        if len(body) != size or expected_digest is not None and digest != expected_digest:
            raise ValueError("remote object digest mismatch")
        return body

    def download_file(self, key: str, destination: Path, expected_digest: str, expected_size: int) -> None:
        self._validate_object_key(key, expected_digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        if int(response.get("ContentLength", -1)) != expected_size:
            raise ValueError("remote object size mismatch")
        fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temp = Path(temp_name)
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = response["Body"].read(min(self.config.multipart_chunk_size, 1024 * 1024))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.config.max_bytes:
                        raise ValueError("remote object exceeds maximum size")
                    digest.update(chunk)
                    output.write(chunk)
                    if total == expected_size or total % (16 * 1024 * 1024) == 0:
                        report_progress("download", f"Downloading encrypted Room • {_percent(total, expected_size)}%", current=total, total=expected_size)
                output.flush()
                os.fsync(output.fileno())
            if total != expected_size or digest.hexdigest() != expected_digest:
                raise ValueError("remote object digest mismatch")
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def _verify_remote(self, key: str, digest: str, size: int) -> None:
        head = self.client.head_object(Bucket=self.config.bucket, Key=key)
        if int(head.get("ContentLength", -1)) != size:
            raise ValueError("remote object size mismatch")
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"]
        observed = hashlib.sha256()
        total = 0
        while True:
            chunk = body.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.max_bytes:
                raise ValueError("remote object exceeds maximum size")
            observed.update(chunk)
        if total != size or observed.hexdigest() != digest:
            raise ValueError("remote object digest mismatch")

    def read_catalog(self) -> tuple[bytes | None, str | None]:
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=self.config.catalog_key)
        except ClientError as error:
            if _not_found(error):
                return None, None
            raise
        response = self.client.get_object(Bucket=self.config.bucket, Key=self.config.catalog_key)
        body = response["Body"].read(self.config.max_bytes + 1)
        if len(body) != int(head.get("ContentLength", -1)) or len(body) > self.config.max_bytes:
            raise ValueError("catalog size mismatch")
        return body, head.get("ETag")

    def conditional_catalog_put(self, body: bytes, expected_etag: str | None) -> str:
        kwargs = {"Bucket": self.config.bucket, "Key": self.config.catalog_key, "Body": body, "ContentLength": len(body)}
        if expected_etag is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = expected_etag
        try:
            self.client.put_object(**kwargs)
        except ClientError as error:
            if _is_precondition(error):
                raise R2Conflict("stale catalog revision or existing catalog") from error
            raise
        head = self.client.head_object(Bucket=self.config.bucket, Key=self.config.catalog_key)
        verified, _etag = self.read_catalog()
        if verified != body:
            raise ValueError("catalog read-back mismatch")
        return head.get("ETag", "")

    def record_orphan(self, ref: ObjectRef) -> Path | None:
        if not self.receipt_dir:
            return None
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipt_dir / f"orphan-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({"status": "uploaded-unreferenced", "object_key": ref.key, "sha256": ref.sha256, "size": ref.size}, sort_keys=True))
        return path

    def delete_object(self, key: str) -> None:
        self._validate_object_key(key)
        self.client.delete_object(Bucket=self.config.bucket, Key=key)

    @staticmethod
    def _validate_object_key(key: str, digest: str | None = None) -> None:
        match = OBJECT_KEY.fullmatch(key)
        if not match or digest is not None and match.group(1) != digest:
            raise ValueError("invalid opaque object key")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percent(current: int, total: int) -> int:
    return 100 if total <= 0 else min(100, int(current * 100 / total))


def _is_precondition(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code")) in {"412", "PreconditionFailed", "ConditionalRequestConflict"}


def _not_found(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}
