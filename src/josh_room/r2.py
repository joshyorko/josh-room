import hashlib
import io
import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from botocore.exceptions import BotoCoreError, ClientError

from .config import DimensionConfig, resolve_dimension
from .encryption_domain import (
    CONTROL_OBJECT_MAX_BYTES,
    is_control_key,
    validate_minio_transport,
)
from .keyring import lookup
from .local_store import ObjectRef
from .object_store import ObjectStore
from .progress import report_progress
from .s3 import BucketAccessDenied, BucketListForbidden
from .s3 import check_bucket_access as _check_bucket_access
from .s3 import create_bucket as _create_bucket
from .s3 import list_buckets as _list_buckets


OBJECT_KEY = re.compile(r"^objects/sha256/([0-9a-f]{64})$")


class R2PublicationError(RuntimeError):
    def __init__(self, message: str, *, published: bool):
        self.published = published
        super().__init__(message)
EVIDENCE_OBJECT_PREFIX = "evidence/objects/sha256/"
EVIDENCE_INDEX_PREFIX = "evidence/index/v1/"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_OBJECT_KEY = re.compile(r"^evidence/objects/sha256/([0-9a-f]{64})$")
_EVIDENCE_INDEX_KEY = re.compile(r"^evidence/index/v1/([0-9a-f]{2})/([0-9a-f]{64})\.age$")
_MAX_EVIDENCE_INDEX_PAGE = 1000


class R2EvidenceError(R2PublicationError):
    """Public-safe failure for the dedicated encrypted evidence namespace."""

    def __init__(
        self,
        code: str,
        *,
        published: bool = False,
        source_preserved: bool = True,
        retries: int = 0,
    ):
        self.code = code
        self.source_preserved = source_preserved
        self.retries = retries
        super().__init__(f"evidence operation failed: {code}", published=published)


class R2EvidenceConflict(R2EvidenceError):
    def __init__(self, *, retries: int = 0):
        super().__init__("immutable-conflict", retries=retries)


class R2EvidenceReadbackMismatch(R2EvidenceError):
    def __init__(self, *, published: bool = True, retries: int = 0):
        super().__init__("readback-mismatch", published=published, retries=retries)


class R2EvidenceRetryable(R2EvidenceError):
    def __init__(self, code: str = "retry-exhausted", *, published: bool = False, retries: int = 0):
        super().__init__(code, published=published, retries=retries)


class R2EvidenceAbortFailure(R2EvidenceError):
    def __init__(self, *, retries: int = 0):
        super().__init__("multipart-abort-failed", retries=retries)


class R2EvidenceMultipartConditionalUnsupported(R2EvidenceError):
    def __init__(self, *, retries: int = 0):
        super().__init__("multipart-conditional-create-unsupported", retries=retries)


class R2EvidenceOutboxPrecondition(R2EvidenceError):
    def __init__(self):
        super().__init__("outbox-precondition")


@dataclass(frozen=True, slots=True)
class R2EvidenceMetrics:
    bytes: int = 0
    parts: int = 0
    retries: int = 0
    duplicate: bool = False
    readback_verified: bool = False
    orphaned: bool = False
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class R2EvidenceReceipt:
    key: str
    ciphertext_sha256: str
    ciphertext_size: int
    metrics: R2EvidenceMetrics
    recovery: str = "none"


@dataclass(frozen=True, slots=True)
class R2EvidenceIndexRef:
    key: str
    ciphertext_sha256: str
    ciphertext_size: int


@dataclass(frozen=True, slots=True)
class R2EvidencePublication:
    evidence: R2EvidenceReceipt
    index: R2EvidenceReceipt | None
    committed: bool


class R2Conflict(R2PublicationError):
    def __init__(self, message: str):
        super().__init__(message, published=False)


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
    temporary_credentials: bool = True
    dimension_id: str | None = None

    @classmethod
    def from_dimension(cls, dimension: DimensionConfig) -> "R2Config":
        if dimension.provider != "r2":
            raise ValueError("selected Dimension is not an R2 Dimension")
        return cls(endpoint=dimension.endpoint, bucket=dimension.bucket, credential_profile=dimension.credential_profile, region=dimension.region, catalog_key=dimension.catalog_key, multipart_threshold=dimension.option("multipart_threshold", cls.multipart_threshold), multipart_chunk_size=dimension.option("multipart_chunk_size", cls.multipart_chunk_size), max_bytes=dimension.option("max_bytes", cls.max_bytes), timeout_seconds=dimension.option("timeout_seconds", cls.timeout_seconds), max_attempts=dimension.option("max_attempts", cls.max_attempts), temporary_credentials=dimension.option("temporary_credentials", True), dimension_id=dimension.dimension_id)

    @classmethod
    def from_private(cls, config: dict | DimensionConfig, dimension_id: str | None = None) -> "R2Config":
        if isinstance(config, DimensionConfig):
            return cls.from_dimension(config)
        if dimension_id:
            return cls.from_dimension(resolve_dimension(config, dimension_id))
        values = config.get("r2") if config else None
        if not values:
            raise ValueError("private R2 configuration is unavailable")
        return cls(endpoint=values["endpoint"], bucket=values["bucket"], credential_profile=values["credential_profile"], region=values.get("region", "auto"), catalog_key=values.get("catalog_key", "catalog.jroom.age"), temporary_credentials=values.get("temporary_credentials", True), dimension_id="r2")

def client_for_config(config: R2Config):
    import boto3
    from botocore.config import Config

    credentials = lookup(config.credential_profile, allow_runtime=True)
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint,
        region_name=config.region,
        aws_access_key_id=credentials["access-key-id"],
        aws_secret_access_key=credentials["secret-access-key"],
        aws_session_token=credentials.get("session-token"),
        config=Config(
            connect_timeout=config.timeout_seconds,
            read_timeout=config.timeout_seconds,
            retries={"max_attempts": config.max_attempts, "mode": "standard"},
        ),
    )


def list_buckets(config: R2Config, client=None) -> list[str]:
    return _list_buckets(client or client_for_config(config), "Cloudflare R2", error_type=BucketListForbidden, context=config.dimension_id)


def create_bucket(config: R2Config, bucket: str, client=None) -> str:
    return _create_bucket(client or client_for_config(config), bucket, region=config.region)


def check_bucket_access(config: R2Config, bucket: str, client=None) -> str:
    return _check_bucket_access(client or client_for_config(config), bucket, "Cloudflare R2", error_type=BucketAccessDenied, context=config.dimension_id)


def evidence_object_key(ciphertext_sha256: str) -> str:
    if not isinstance(ciphertext_sha256, str) or not _DIGEST.fullmatch(ciphertext_sha256):
        raise ValueError("invalid evidence ciphertext digest")
    return f"{EVIDENCE_OBJECT_PREFIX}{ciphertext_sha256}"


def evidence_index_key(ciphertext_sha256: str) -> str:
    if not isinstance(ciphertext_sha256, str) or not _DIGEST.fullmatch(ciphertext_sha256):
        raise ValueError("invalid evidence index digest")
    return f"{EVIDENCE_INDEX_PREFIX}{ciphertext_sha256[:2]}/{ciphertext_sha256}.age"


def validate_evidence_object_key(key: str) -> str:
    match = _EVIDENCE_OBJECT_KEY.fullmatch(key)
    if not match:
        raise ValueError("invalid evidence object key")
    return match.group(1)


def validate_evidence_index_key(key: str) -> str:
    match = _EVIDENCE_INDEX_KEY.fullmatch(key)
    if not match or match.group(1) != match.group(2)[:2]:
        raise ValueError("invalid evidence index key")
    return match.group(2)


class R2Backend(ObjectStore):
    def __init__(self, config: R2Config, client=None, receipt_dir: Path | None = None):
        if hasattr(config, "verify_tls"):
            validate_minio_transport(
                config.endpoint,
                verify_tls=config.verify_tls,
                ca_bundle=getattr(config, "ca_bundle", None),
            )
        self.config = config
        self.client = client or self._client_from_keyring()
        self.receipt_dir = Path(receipt_dir) if receipt_dir else None
        if "/" in config.catalog_key or config.catalog_key.startswith("."):
            raise ValueError("catalog key must be fixed and opaque")

    def _client_from_keyring(self):
        return client_for_config(self.config)

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
                report_progress("verify", "Encrypted Room already exists; verifying R2 object")
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
                    report_progress("verify", "Encrypted Room already exists; verifying R2 object")
                    self._verify_remote(key, digest, size)
                    return ObjectRef(key, digest, size)
            except BaseException:  # noqa: BLE001 - abort multipart on cancellation as well as SDK errors
                try:
                    self.client.abort_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id)
                finally:
                    raise
        report_progress("upload", "Encrypted Room uploaded • 100%", current=size, total=size)
        report_progress("verify", "Verifying encrypted R2 object")
        self._verify_remote(key, digest, size)
        return ObjectRef(key, digest, size)
    def put_evidence_file(self, path: Path) -> R2EvidenceReceipt:
        """Stream a durable ciphertext file without reopening a path after validation."""
        source, size, digest = self._snapshot_evidence_file(path)
        def factory():
            source.seek(0)
            return source
        factory.owns_source = False
        try:
            return self._put_evidence(evidence_object_key(digest), factory, size, digest)
        finally:
            source.close()

    def _snapshot_evidence_file(self, path: Path):
        source = None
        descriptor = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(os.fspath(Path(path)), flags)
            source = os.fdopen(descriptor, "rb")
            descriptor = -1
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077:
                raise ValueError("source-private")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(source.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size:
                raise ValueError("source-changed")
            source.seek(0)
            return source, size, digest.hexdigest()
        except (OSError, ValueError) as error:
            if source is not None:
                source.close()
            elif descriptor >= 0:
                os.close(descriptor)
            raise R2EvidenceError("source-unavailable") from error

    def put_evidence_stream(self, source, size: int, ciphertext_sha256: str) -> R2EvidenceReceipt:
        """Stream a ciphertext reader; the supplied digest is independently read back."""
        if type(size) is not int or size < 0:
            raise ValueError("evidence size is invalid")
        if not _DIGEST.fullmatch(ciphertext_sha256):
            raise ValueError("invalid evidence ciphertext digest")
        if not hasattr(source, "read"):
            raise TypeError("evidence source is not readable")

        def factory():
            try:
                source.seek(0)
            except (AttributeError, OSError, ValueError):
                if getattr(factory, "used", False):
                    raise R2EvidenceRetryable("source-not-rewindable")
            factory.used = True
            return source

        factory.used = False
        factory.owns_source = False
        return self._put_evidence(evidence_object_key(ciphertext_sha256), factory, size, ciphertext_sha256)
    def get_evidence_bytes(self, key: str, expected_size: int | None = None) -> bytes:
        digest = validate_evidence_object_key(key)
        try:
            self._verify_evidence_remote(key, digest, expected_size)
        except ValueError as error:
            raise R2EvidenceReadbackMismatch() from error
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"].read(self.config.max_bytes + 1)
        if len(body) != int(response.get("ContentLength", -1)) or len(body) > self.config.max_bytes:
            raise R2EvidenceReadbackMismatch()
        if hashlib.sha256(body).hexdigest() != digest:
            raise R2EvidenceReadbackMismatch()
        return body
    def download_evidence_file(self, key: str, destination: Path, expected_size: int) -> None:
        digest = validate_evidence_object_key(key)
        try:
            self._verify_evidence_remote(key, digest, expected_size)
        except ValueError as error:
            raise R2EvidenceReadbackMismatch() from error
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        temporary = Path(temporary_name)
        observed = hashlib.sha256()
        total = 0
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = response["Body"].read(min(self.config.multipart_chunk_size, 1024 * 1024))
                    if not chunk:
                        break
                    total += len(chunk)
                    observed.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total != expected_size or observed.hexdigest() != digest:
                raise R2EvidenceReadbackMismatch()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    def put_evidence_index_file(self, path: Path) -> R2EvidenceReceipt:
        source, size, digest = self._snapshot_evidence_file(path)
        def factory():
            source.seek(0)
            return source
        factory.owns_source = False
        try:
            return self._put_evidence(evidence_index_key(digest), factory, size, digest)
        finally:
            source.close()

    def put_evidence_index_bytes(self, ciphertext: bytes) -> R2EvidenceReceipt:
        if not isinstance(ciphertext, bytes):
            raise TypeError("evidence index ciphertext must be bytes")
        digest = hashlib.sha256(ciphertext).hexdigest()
        return self._put_evidence(evidence_index_key(digest), lambda: io.BytesIO(ciphertext), len(ciphertext), digest)

    publish_index_event = put_evidence_index_bytes

    def get_evidence_index_bytes(self, key: str) -> bytes:
        digest = validate_evidence_index_key(key)
        try:
            self._verify_evidence_remote(key, digest, None)
        except ValueError as error:
            raise R2EvidenceReadbackMismatch() from error
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"].read(self.config.max_bytes + 1)
        if hashlib.sha256(body).hexdigest() != digest:
            raise R2EvidenceReadbackMismatch()
        return body

    def discover_evidence_indexes(self, *, max_events: int = 1000, page_size: int = 100) -> list[R2EvidenceIndexRef]:
        """Discover only encrypted index keys under the fixed, opaque prefix."""
        if type(max_events) is not int or not 0 < max_events <= 100_000:
            raise ValueError("evidence discovery bound is invalid")
        if type(page_size) is not int or not 0 < page_size <= _MAX_EVIDENCE_INDEX_PAGE:
            raise ValueError("evidence page size is invalid")
        result: dict[str, R2EvidenceIndexRef] = {}
        token = None
        seen_tokens: set[str] = set()
        pages = 0
        while pages < max_events:
            kwargs = {
                "Bucket": self.config.bucket,
                "Prefix": EVIDENCE_INDEX_PREFIX,
                "MaxKeys": min(page_size, max_events - len(result)),
            }
            if token is not None:
                kwargs["ContinuationToken"] = token
            try:
                response = self.client.list_objects_v2(**kwargs)
            except (BotoCoreError, ClientError, TimeoutError) as error:
                raise self._map_evidence_error(error, published=False) from error
            pages += 1
            for item in response.get("Contents", ()) or ():
                key = item.get("Key")
                if not isinstance(key, str):
                    continue
                try:
                    digest = validate_evidence_index_key(key)
                except ValueError:
                    continue
                size = item.get("Size")
                if type(size) is not int or size < 0 or size > self.config.max_bytes:
                    continue
                result.setdefault(key, R2EvidenceIndexRef(key, digest, size))
                if len(result) >= max_events:
                    break
            if len(result) >= max_events or not response.get("IsTruncated"):
                break
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            token = next_token
        return sorted(result.values(), key=lambda item: item.key)

    put_evidence = put_evidence_file
    get_evidence = get_evidence_bytes
    discover_index_events = discover_evidence_indexes

    def publish_outbox_evidence(
        self,
        outbox,
        event_id: str,
        owner: str,
        *,
        index_ciphertext: bytes | Path | None,
    ) -> R2EvidencePublication:
        """Drive #8's uploaded -> indexed -> committed seam without catalog writes."""
        try:
            queued = outbox.inspect_record(event_id)
        except Exception as error:  # noqa: BLE001 - no untrusted outbox detail crosses the boundary
            raise R2EvidenceOutboxPrecondition() from error
        if queued is None:
            raise R2EvidenceOutboxPrecondition()
        state = getattr(queued.state, "value", queued.state)
        resume = getattr(queued.resume_state, "value", queued.resume_state)
        if queued.object_key is not None:
            try:
                if validate_evidence_object_key(queued.object_key) != queued.ciphertext_sha256:
                    raise ValueError("object identity")
            except ValueError as error:
                raise R2EvidenceOutboxPrecondition() from error
        if queued.ciphertext_sha256 is None or queued.ciphertext_size is None:
            raise R2EvidenceOutboxPrecondition()
        evidence_key = evidence_object_key(queued.ciphertext_sha256)
        if state == "committed":
            if queued.index_id is None:
                raise R2EvidenceOutboxPrecondition()
            try:
                self._verify_evidence_remote(evidence_key, queued.ciphertext_sha256, queued.ciphertext_size)
                index_key = evidence_index_key(queued.index_id)
                index_size = self._verify_evidence_remote(index_key, queued.index_id, None)
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=False) from error
            return R2EvidencePublication(
                R2EvidenceReceipt(evidence_key, queued.ciphertext_sha256, queued.ciphertext_size, R2EvidenceMetrics(queued.ciphertext_size, 0, 0, True, True)),
                R2EvidenceReceipt(index_key, queued.index_id, index_size, R2EvidenceMetrics(index_size, 1, 0, True, True)),
                True,
            )
        if queued.owner != owner:
            raise R2EvidenceOutboxPrecondition()
        prepared = outbox.prepared.inspect_record(event_id)
        if prepared is None:
            raise R2EvidenceOutboxPrecondition()
        if hasattr(prepared, "ciphertext_file"):
            source = outbox.prepared.directory / prepared.ciphertext_file
            evidence = self.put_evidence_file(source)
        else:
            evidence = self.put_evidence_stream(io.BytesIO(prepared.ciphertext), prepared.ciphertext_size, prepared.ciphertext_sha256)
        if evidence.key != evidence_key:
            raise R2EvidenceOutboxPrecondition()
        stage = resume if state in {"claimed", "retryable-failure", "capture-gap"} else state
        if stage not in {"object-uploaded", "index-published"}:
            outbox.mark_uploaded(event_id, owner, object_key=evidence.key, ciphertext_size=evidence.ciphertext_size)
            stage = "object-uploaded"
        index = None
        if stage == "index-published":
            if queued.index_id is None:
                raise R2EvidenceOutboxPrecondition()
            index_key = evidence_index_key(queued.index_id)
            try:
                index_size = self._verify_evidence_remote(index_key, queued.index_id, None)
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=False) from error
            index = R2EvidenceReceipt(index_key, queued.index_id, index_size, R2EvidenceMetrics(index_size, 1, 0, True, True))
        else:
            if index_ciphertext is None:
                return R2EvidencePublication(evidence, None, False)
            if isinstance(index_ciphertext, Path):
                index = self.put_evidence_index_file(index_ciphertext)
            else:
                index = self.put_evidence_index_bytes(index_ciphertext)
            outbox.publish_index(event_id, owner, index_id=index.ciphertext_sha256)
        outbox.commit(event_id, owner)
        return R2EvidencePublication(evidence, index, True)

    def _put_evidence(
        self,
        key: str,
        source_factory: Callable[[], object],
        size: int,
        digest: str,
    ) -> R2EvidenceReceipt:
        if type(size) is not int or size < 0 or size > self.config.max_bytes:
            raise ValueError("evidence object exceeds maximum size")
        if key.startswith(EVIDENCE_OBJECT_PREFIX):
            if validate_evidence_object_key(key) != digest:
                raise ValueError("invalid evidence object key")
        else:
            if validate_evidence_index_key(key) != digest:
                raise ValueError("invalid evidence index key")
        started = time.monotonic()
        if size < self.config.multipart_threshold:
            return self._put_evidence_single(key, source_factory, size, digest, started)
        return self._put_evidence_multipart(key, source_factory, size, digest, started)

    def _duplicate_readback_failure(self, error, retries: int):
        if _is_retryable(error):
            return R2EvidenceRetryable("duplicate-readback-retry", published=True, retries=retries)
        if isinstance(error, ValueError):
            return R2EvidenceConflict(retries=retries)
        return R2EvidenceReadbackMismatch(published=True, retries=retries)
    def _put_evidence_single(self, key, source_factory, size, digest, started):
        retries = 0
        for attempt in range(max(1, self.config.max_attempts)):
            source = None
            try:
                source = source_factory()
                self.client.put_object(
                    Bucket=self.config.bucket,
                    Key=key,
                    Body=source,
                    ContentLength=size,
                    IfNoneMatch="*",
                    Metadata={"sha256": digest},
                )
                self._verify_evidence_remote(key, digest, size)
                return R2EvidenceReceipt(
                    key,
                    digest,
                    size,
                    R2EvidenceMetrics(size, 1, retries, False, True, False, _latency_ms(started)),
                )
            except ClientError as error:
                if _is_precondition(error):
                    try:
                        self._verify_evidence_remote(key, digest, size)
                    except (ValueError, ClientError, BotoCoreError, TimeoutError) as mismatch:
                        raise self._duplicate_readback_failure(mismatch, retries) from mismatch
                    return R2EvidenceReceipt(
                        key,
                        digest,
                        size,
                        R2EvidenceMetrics(size, 1, retries, True, True, False, _latency_ms(started)),
                    )
                if not _is_retryable(error) or attempt + 1 >= max(1, self.config.max_attempts):
                    raise self._map_evidence_error(error, retries=retries) from error
                retries += 1
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=True, retries=retries) from error
            except (BotoCoreError, TimeoutError) as error:
                if attempt + 1 >= max(1, self.config.max_attempts):
                    try:
                        self._verify_evidence_remote(key, digest, size)
                    except (ValueError, ClientError, BotoCoreError, TimeoutError) as mismatch:
                        raise self._duplicate_readback_failure(mismatch, retries) from mismatch
                    return R2EvidenceReceipt(
                        key,
                        digest,
                        size,
                        R2EvidenceMetrics(size, 1, retries, True, True, False, _latency_ms(started)),
                    )
                retries += 1
            finally:
                if source is not None and getattr(source_factory, "owns_source", True):
                    try:
                        source.close()
                    except (AttributeError, OSError):
                        pass
            if retries:
                time.sleep(min(0.01 * (2 ** min(retries - 1, 4)), 0.1))
        raise R2EvidenceRetryable(retries=retries)

    def _put_evidence_multipart(self, key, source_factory, size, digest, started):
        retries = 0
        max_attempts = max(1, self.config.max_attempts)
        for _upload_attempt in range(max_attempts):
            source = None
            upload_id = None
            parts = []
            committed = False
            try:
                try:
                    upload_id = self.client.create_multipart_upload(
                        Bucket=self.config.bucket,
                        Key=key,
                        Metadata={"sha256": digest},
                    )["UploadId"]
                except (ClientError, BotoCoreError, TimeoutError) as error:
                    if not _is_retryable(error) or retries + 1 >= max_attempts:
                        raise self._map_evidence_error(error, retries=retries, published=False) from error
                    retries += 1
                    time.sleep(min(0.01 * (2 ** min(retries - 1, 4)), 0.1))
                    continue
                source = source_factory()
                part_number = 1
                while True:
                    chunk = source.read(self.config.multipart_chunk_size)
                    if not chunk:
                        break
                    if part_number > 10_000:
                        raise R2EvidenceError("multipart-part-limit")
                    part_retries = 0
                    while True:
                        try:
                            result = self.client.upload_part(
                                Bucket=self.config.bucket,
                                Key=key,
                                UploadId=upload_id,
                                PartNumber=part_number,
                                Body=chunk,
                            )
                            break
                        except (ClientError, BotoCoreError, TimeoutError) as error:
                            if not _is_retryable(error) or retries + 1 >= max_attempts:
                                raise self._map_evidence_error(error, retries=retries, published=False) from error
                            retries += 1
                            part_retries += 1
                            time.sleep(min(0.01 * (2 ** min(part_retries - 1, 4)), 0.1))
                    parts.append({"ETag": result["ETag"], "PartNumber": part_number})
                    part_number += 1
                try:
                    self.client.complete_multipart_upload(
                        Bucket=self.config.bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                        IfNoneMatch="*",
                    )
                except (ClientError, BotoCoreError, TimeoutError) as error:
                    if isinstance(error, ClientError) and _is_precondition(error):
                        try:
                            self._verify_evidence_remote(key, digest, size)
                        except (ValueError, ClientError, BotoCoreError, TimeoutError) as mismatch:
                            raise self._duplicate_readback_failure(mismatch, retries) from mismatch
                        self._abort_evidence_upload(key, upload_id, retries)
                        upload_id = None
                        committed = True
                        return R2EvidenceReceipt(
                            key,
                            digest,
                            size,
                            R2EvidenceMetrics(size, len(parts), retries, True, True, False, _latency_ms(started)),
                            "duplicate-conflict-verified",
                        )
                    if error.__class__.__name__ == "ParamValidationError":
                        raise R2EvidenceMultipartConditionalUnsupported(retries=retries) from error
                    if isinstance(error, ClientError) and str(error.response.get("Error", {}).get("Code")) in {
                        "NotImplemented",
                        "InvalidRequest",
                        "UnsupportedHeader",
                        "InvalidArgument",
                    }:
                        raise R2EvidenceMultipartConditionalUnsupported(retries=retries) from error
                    if not _is_retryable(error):
                        raise self._map_evidence_error(error, retries=retries) from error
                    try:
                        self._verify_evidence_remote(key, digest, size)
                    except (ValueError, ClientError, BotoCoreError, TimeoutError) as mismatch:
                        self._abort_evidence_upload(key, upload_id, retries)
                        upload_id = None
                        if retries + 1 >= max_attempts:
                            raise self._duplicate_readback_failure(mismatch, retries) from mismatch
                        retries += 1
                        continue
                    committed = True
                    return R2EvidenceReceipt(
                        key,
                        digest,
                        size,
                        R2EvidenceMetrics(size, len(parts), retries, True, True, False, _latency_ms(started)),
                        "ambiguous-complete-verified",
                    )
                self._verify_evidence_remote(key, digest, size)
                committed = True
                return R2EvidenceReceipt(
                    key,
                    digest,
                    size,
                    R2EvidenceMetrics(size, len(parts), retries, False, True, False, _latency_ms(started)),
                )
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=True, retries=retries) from error
            except (ClientError, BotoCoreError, TimeoutError) as error:
                if error.__class__.__name__ == "ParamValidationError":
                    raise R2EvidenceMultipartConditionalUnsupported(retries=retries) from error
                raise self._map_evidence_error(error, retries=retries) from error
            finally:
                if source is not None and getattr(source_factory, "owns_source", True):
                    try:
                        source.close()
                    except (AttributeError, OSError):
                        pass
                if upload_id is not None and not committed:
                    self._abort_evidence_upload(key, upload_id, retries)
        raise R2EvidenceRetryable(retries=retries)

    def _abort_evidence_upload(self, key: str, upload_id: str, retries: int) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id)
        except Exception as error:  # noqa: BLE001 - provider-specific abort failures are typed
            raise R2EvidenceAbortFailure(retries=retries) from error

    def _verify_evidence_remote(self, key: str, digest: str, size: int | None) -> int:
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if _not_found(error):
                raise ValueError("evidence object is unavailable") from error
            raise
        observed_size = int(head.get("ContentLength", -1))
        if observed_size > self.config.max_bytes or size is not None and observed_size != size:
            raise ValueError("evidence object size mismatch")
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        body = response["Body"]
        observed = hashlib.sha256()
        total = 0
        while True:
            chunk = body.read(min(self.config.multipart_chunk_size, 1024 * 1024))
            if not chunk:
                break
            total += len(chunk)
            if total > self.config.max_bytes:
                raise ValueError("evidence object exceeds maximum size")
            observed.update(chunk)
        if total != observed_size or observed.hexdigest() != digest:
            raise ValueError("evidence object digest mismatch")

        return observed_size
    def _map_evidence_error(self, error, *, retries: int = 0, published: bool = True):
        code = str(error.response.get("Error", {}).get("Code")) if isinstance(error, ClientError) else ""
        if code in {"ExpiredToken", "InvalidToken", "TokenRefreshRequired"}:
            return R2EvidenceRetryable("credentials-expired", published=published, retries=retries)
        if _is_retryable(error):
            return R2EvidenceRetryable("retry-exhausted", published=published, retries=retries)
        return R2EvidenceError("publication-unknown", published=published, retries=retries)
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

    def verify_object(self, key: str, expected_digest: str, expected_size: int) -> ObjectRef:
        self._validate_object_key(key, expected_digest)
        self._verify_remote(key, expected_digest, expected_size)
        return ObjectRef(key, expected_digest, expected_size)

    def read_catalog(self) -> tuple[bytes | None, str | None]:
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=self.config.catalog_key)
        except ClientError as error:
            if _not_found(error):
                return None, None
            raise
        kwargs = {"Bucket": self.config.bucket, "Key": self.config.catalog_key}
        if head.get("ETag"):
            kwargs["IfMatch"] = head["ETag"]
        response = self.client.get_object(**kwargs)
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
            raise R2PublicationError("catalog publication outcome is unknown", published=True) from error
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=self.config.catalog_key)
            verified, _etag = self.read_catalog()
            if verified != body:
                raise ValueError("catalog read-back mismatch")
            return head.get("ETag", "")
        except BaseException as error:
            raise R2PublicationError("catalog publication verification failed", published=True) from error

    def read_control(self, key: str, max_bytes: int):
        _validate_control_key(key)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("control object size bound is invalid")
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if _not_found(error):
                return None, None
            raise
        size = int(head.get("ContentLength", -1))
        max_bytes = min(max_bytes, CONTROL_OBJECT_MAX_BYTES)
        if size < 0 or size > max_bytes:
            raise ValueError("control object exceeds maximum size")
        kwargs = {"Bucket": self.config.bucket, "Key": key}
        if head.get("ETag"):
            kwargs["IfMatch"] = head["ETag"]
        response = self.client.get_object(**kwargs)
        body = response["Body"].read(max_bytes + 1)
        if len(body) != size or len(body) > max_bytes:
            raise ValueError("control object exceeds maximum size")
        return body, head.get("ETag")

    def create_control(self, key: str, body: bytes) -> str:
        return self._publish_control(key, body, expected_etag=None)

    def replace_control(self, key: str, body: bytes, expected_etag: str) -> str:
        if not isinstance(expected_etag, str) or not expected_etag:
            raise ValueError("control object ETag is required")
        return self._publish_control(key, body, expected_etag=expected_etag)

    def _publish_control(self, key: str, body: bytes, expected_etag: str | None) -> str:
        _validate_control_key(key)
        if not isinstance(body, bytes):
            raise TypeError("control object body must be bytes")
        if len(body) > CONTROL_OBJECT_MAX_BYTES:
            raise ValueError("control object exceeds maximum size")
        kwargs = {"Bucket": self.config.bucket, "Key": key, "Body": body, "ContentLength": len(body)}
        if expected_etag is None:
            kwargs["IfNoneMatch"] = "*"
        else:
            kwargs["IfMatch"] = expected_etag
        try:
            self.client.put_object(**kwargs)
        except (BotoCoreError, ClientError) as error:
            if isinstance(error, ClientError) and _is_precondition(error):
                raise R2Conflict("control object conditional conflict") from error
            raise R2PublicationError("control object publication outcome is unknown", published=True) from error
        try:
            verified, etag = self.read_control(key, CONTROL_OBJECT_MAX_BYTES)
            if verified != body:
                raise ValueError("control object read-back mismatch")
            return etag or ""
        except Exception as error:
            raise R2PublicationError("control object publication verification failed", published=True) from error

    def record_orphan(self, ref: ObjectRef) -> Path | None:
        if not self.receipt_dir:
            return None
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipt_dir / f"orphan-{uuid.uuid4().hex}.json"
        provider = "minio" if self.__class__.__module__.endswith("minio") else "r2"
        destination = {"provider": provider}
        for name in ("dimension_id", "endpoint", "bucket"):
            value = getattr(self.config, name, None)
            if isinstance(value, str) and value:
                destination[name] = value
        path.write_text(json.dumps({"status": "uploaded-unreferenced", "destination": destination, "object_key": ref.key, "sha256": ref.sha256, "size": ref.size}, sort_keys=True))
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
    return str(error.response.get("Error", {}).get("Code")) in {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}


def _not_found(error: ClientError) -> bool:
    return str(error.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}



def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code"))
        return code in {
            "408",
            "425",
            "429",
            "500",
            "502",
            "503",
            "504",
            "SlowDown",
            "RequestTimeout",
            "InternalError",
            "ServiceUnavailable",
            "Throttling",
        }
    return isinstance(error, BotoCoreError)


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))

def _validate_control_key(key: str) -> None:
    if not is_control_key(key):
        raise ValueError("control key is not allowlisted")
