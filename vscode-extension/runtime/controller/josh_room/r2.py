import hashlib
import io
import json
import os
import re
import secrets
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

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

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows runtime
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX runtime
    _msvcrt = None


OBJECT_KEY = re.compile(r"^objects/sha256/([0-9a-f]{64})$")


class R2PublicationError(RuntimeError):
    def __init__(self, message: str, *, published: bool):
        self.published = published
        super().__init__(message)
EVIDENCE_OBJECT_PREFIX = "evidence/objects/sha256/"
EVIDENCE_INDEX_PREFIX = "evidence/index/v1/"
EVIDENCE_CLAIM_PREFIX = "evidence/claims/v1/"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_OBJECT_KEY = re.compile(r"^evidence/objects/sha256/([0-9a-f]{64})$")
_EVIDENCE_INDEX_KEY = re.compile(r"^evidence/index/v1/([0-9a-f]{2})/([0-9a-f]{64})\.age$")
_EVIDENCE_CLAIM_KEY = re.compile(r"^evidence/claims/v1/([0-9a-f]{64})$")
_MAX_EVIDENCE_INDEX_PAGE = 1000
_MAX_EVIDENCE_CLAIM_BYTES = 4096
_MAX_EVIDENCE_STATE_BYTES = 1024 * 1024
_MAX_EVIDENCE_PARTS = 10_000


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
    def __init__(self, code: str = "immutable-conflict", *, retries: int = 0):
        super().__init__(code, retries=retries)


class R2EvidenceTimeout(R2EvidenceError):
    def __init__(self, *, published: bool = False, retries: int = 0):
        super().__init__("timeout", published=published, retries=retries)


class R2EvidenceRateLimited(R2EvidenceError):
    def __init__(self, *, published: bool = False, retries: int = 0):
        super().__init__("rate-limited", published=published, retries=retries)


class R2EvidenceCredentialFailure(R2EvidenceError):
    def __init__(self, *, published: bool = False, retries: int = 0):
        super().__init__("credentials", published=published, retries=retries)


class R2EvidenceAmbiguous(R2EvidenceError):
    def __init__(self, *, retries: int = 0):
        super().__init__("ambiguous-complete", published=True, retries=retries)


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

def client_for_config(config: R2Config, credential_profile: str | None = None):
    import boto3
    from botocore.config import Config

    selected_profile = credential_profile or config.credential_profile
    if selected_profile in {None, "oauth-runtime"}:
        from .device import active_credential_profile
        selected_profile = active_credential_profile() or selected_profile
    credential_profile = selected_profile
    credentials = lookup(credential_profile, allow_runtime=True)
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
def evidence_claim_key(ciphertext_sha256: str, final_key: str | None = None) -> str:
    if not isinstance(ciphertext_sha256, str) or not _DIGEST.fullmatch(ciphertext_sha256):
        raise ValueError("invalid evidence claim digest")
    final_key = evidence_object_key(ciphertext_sha256) if final_key is None else final_key
    try:
        if final_key.startswith(EVIDENCE_OBJECT_PREFIX):
            if validate_evidence_object_key(final_key) != ciphertext_sha256:
                raise ValueError("invalid evidence claim object")
        elif final_key.startswith(EVIDENCE_INDEX_PREFIX):
            if validate_evidence_index_key(final_key) != ciphertext_sha256:
                raise ValueError("invalid evidence claim index")
        else:
            raise ValueError("invalid evidence claim final key")
    except (AttributeError, TypeError) as error:
        raise ValueError("invalid evidence claim final key") from error
    claim_digest = hashlib.sha256(b"josh-room-pcc-claim-v1\0" + final_key.encode()).hexdigest()
    return f"{EVIDENCE_CLAIM_PREFIX}{claim_digest}"


def validate_evidence_claim_key(key: str) -> str:
    match = _EVIDENCE_CLAIM_KEY.fullmatch(key)
    if not match:
        raise ValueError("invalid evidence claim key")
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
        staged = None
        descriptor = -1
        try:
            candidate = Path(path)
            current = candidate.parent
            while current != current.parent:
                if current.is_symlink():
                    raise ValueError("source-private")
                current = current.parent
            if candidate.is_symlink():
                raise ValueError("source-private")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(os.fspath(candidate), flags)
            source = os.fdopen(descriptor, "rb")
            descriptor = -1
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 or before.st_nlink != 1:
                raise ValueError("source-private")
            staged = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - snapshot lifetime spans the publication call
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                staged.write(chunk)
                size += len(chunk)
            after = os.fstat(source.fileno())
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size:
                raise ValueError("source-changed")
            staged.seek(0)
            source.close()
            source = None
            return staged, size, digest.hexdigest()
        except (OSError, ValueError) as error:
            if source is not None:
                source.close()
            elif descriptor >= 0:
                os.close(descriptor)
            if staged is not None:
                staged.close()
            raise R2EvidenceError("source-unavailable") from error

    def put_evidence_stream(self, source, size: int, ciphertext_sha256: str) -> R2EvidenceReceipt:
        """Stage and independently hash a ciphertext reader before publication."""
        if type(size) is not int or size < 0:
            raise ValueError("evidence size is invalid")
        if not _DIGEST.fullmatch(ciphertext_sha256):
            raise ValueError("invalid evidence ciphertext digest")
        if not hasattr(source, "read"):
            raise TypeError("evidence source is not readable")
        staged = tempfile.TemporaryFile(mode="w+b")  # noqa: SIM115 - staged reader lifetime spans the publication call
        observed = hashlib.sha256()
        total = 0
        try:
            while True:
                chunk = source.read(min(self.config.multipart_chunk_size, 1024 * 1024))
                if not chunk:
                    break
                total += len(chunk)
                if total > self.config.max_bytes:
                    raise R2EvidenceReadbackMismatch(published=False)
                observed.update(chunk)
                staged.write(chunk)
            if total != size or observed.hexdigest() != ciphertext_sha256:
                raise R2EvidenceReadbackMismatch(published=False)

            def factory():
                staged.seek(0)
                return staged

            factory.owns_source = False
            return self._put_evidence(evidence_object_key(ciphertext_sha256), factory, size, ciphertext_sha256)
        finally:
            staged.close()
    def get_evidence_bytes(self, key: str, expected_size: int | None = None) -> bytes:
        digest = validate_evidence_object_key(key)
        try:
            self._verify_evidence_remote(key, digest, expected_size, verify_body=False)
        except ValueError as error:
            raise R2EvidenceReadbackMismatch() from error
        if expected_size is not None and (
            type(expected_size) is not int or expected_size < 0 or expected_size > self.config.max_bytes
        ):
            raise R2EvidenceReadbackMismatch()
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        read_limit = self.config.max_bytes + 1
        if expected_size is not None:
            read_limit = min(read_limit, expected_size + 1)
        body = response["Body"].read(read_limit)
        if (
            len(body) != int(response.get("ContentLength", -1))
            or len(body) > self.config.max_bytes
            or (expected_size is not None and len(body) != expected_size)
        ):
            raise R2EvidenceReadbackMismatch()
        if hashlib.sha256(body).hexdigest() != digest:
            raise R2EvidenceReadbackMismatch()
        return body
    def download_evidence_file(self, key: str, destination: Path, expected_size: int) -> None:
        digest = validate_evidence_object_key(key)
        try:
            self._verify_evidence_remote(key, digest, expected_size, verify_body=False)
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

    def get_evidence_index_bytes(self, key: str, expected_size: int | None = None) -> bytes:
        digest = validate_evidence_index_key(key)
        if expected_size is not None and (
            type(expected_size) is not int or expected_size < 0 or expected_size > self.config.max_bytes
        ):
            raise R2EvidenceReadbackMismatch()
        try:
            self._verify_evidence_remote(key, digest, expected_size, verify_body=False)
        except ValueError as error:
            raise R2EvidenceReadbackMismatch() from error
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        read_limit = self.config.max_bytes + 1
        if expected_size is not None:
            read_limit = min(read_limit, expected_size + 1)
        body = response["Body"].read(read_limit)
        if (
            len(body) != int(response.get("ContentLength", -1))
            or len(body) > self.config.max_bytes
            or (expected_size is not None and len(body) != expected_size)
            or hashlib.sha256(body).hexdigest() != digest
        ):
            raise R2EvidenceReadbackMismatch()
        return body

    def discover_evidence_indexes(
        self,
        *,
        max_events: int = 1000,
        page_size: int = 100,
        max_pages: int = 128,
    ) -> list[R2EvidenceIndexRef]:
        """Discover encrypted indexes with explicit event and page bounds."""
        if type(max_events) is not int or not 0 < max_events <= 100_001:
            raise ValueError("evidence discovery bound is invalid")
        if type(page_size) is not int or not 0 < page_size <= _MAX_EVIDENCE_INDEX_PAGE:
            raise ValueError("evidence page size is invalid")
        if type(max_pages) is not int or not 0 < max_pages <= 1000:
            raise ValueError("evidence page bound is invalid")
        result: dict[str, R2EvidenceIndexRef] = {}
        token = None
        seen_tokens: set[str] = set()
        pages = 0
        incomplete = False
        response = {}
        while pages < max_pages:
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
                    incomplete = True
                    continue
                result.setdefault(key, R2EvidenceIndexRef(key, digest, size))
                if len(result) >= max_events:
                    break
            if len(result) >= max_events or not response.get("IsTruncated"):
                break
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                incomplete = True
                break
            seen_tokens.add(next_token)
            token = next_token
        else:
            incomplete = bool(response.get("IsTruncated")) and len(result) < max_events
        if incomplete:
            raise R2EvidenceError("index-discovery-incomplete")
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
        from .device import require_prepare_upload

        require_prepare_upload()
        try:
            queued = outbox.inspect_record(event_id)
        except Exception as error:
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
                index_key = evidence_index_key(queued.index_id)
            except ValueError as error:
                raise R2EvidenceOutboxPrecondition() from error
            try:
                self._verify_evidence_remote(evidence_key, queued.ciphertext_sha256, queued.ciphertext_size)
                index_size = self._verify_evidence_remote(index_key, queued.index_id, None)
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=False) from error
            except (ClientError, BotoCoreError, TimeoutError) as error:
                raise self._map_evidence_error(error, published=True) from error
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
            try:
                index_key = evidence_index_key(queued.index_id)
            except ValueError as error:
                raise R2EvidenceOutboxPrecondition() from error
            try:
                index_size = self._verify_evidence_remote(index_key, queued.index_id, None)
            except ValueError as error:
                if index_ciphertext is None:
                    raise R2EvidenceReadbackMismatch(published=False) from error
                candidate_digest = _file_digest(index_ciphertext) if isinstance(index_ciphertext, Path) else hashlib.sha256(index_ciphertext).hexdigest() if isinstance(index_ciphertext, bytes) else None
                if candidate_digest != queued.index_id:
                    raise R2EvidenceOutboxPrecondition()
                if isinstance(index_ciphertext, Path):
                    index = self.put_evidence_index_file(index_ciphertext)
                else:
                    index = self.put_evidence_index_bytes(index_ciphertext)
                if index.ciphertext_sha256 != queued.index_id:
                    raise R2EvidenceOutboxPrecondition()
                outbox.publish_index(event_id, owner, index_id=index.ciphertext_sha256)
            except (ClientError, BotoCoreError, TimeoutError) as error:
                raise self._map_evidence_error(error, published=True) from error
            else:
                index = R2EvidenceReceipt(index_key, queued.index_id, index_size, R2EvidenceMetrics(index_size, 1, 0, True, True))
        else:
            if index_ciphertext is None:
                evidence = replace(evidence, metrics=replace(evidence.metrics, orphaned=True), recovery="uploaded-unindexed")
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
                    raise self._map_evidence_error(error, retries=retries, published=False) from error
                retries += 1
            except ValueError as error:
                raise R2EvidenceReadbackMismatch(published=True, retries=retries) from error
            except (BotoCoreError, TimeoutError) as error:
                if attempt + 1 >= max(1, self.config.max_attempts):
                    try:
                        self._verify_evidence_remote(key, digest, size)
                    except ValueError as mismatch:
                        try:
                            if self._evidence_final_exists(key):
                                raise R2EvidenceConflict(retries=retries) from mismatch
                        except (ClientError, BotoCoreError, TimeoutError) as readback_error:
                            raise self._map_evidence_error(readback_error, retries=retries, published=False) from readback_error
                        raise self._map_evidence_error(error, retries=retries, published=False) from error
                    except (ClientError, BotoCoreError, TimeoutError) as readback_error:
                        raise self._map_evidence_error(readback_error, retries=retries, published=False) from readback_error
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

    def _validate_receipt_root(self) -> None:
        if self.receipt_dir is None:
            return
        root = self.receipt_dir
        current = root
        while current != current.parent:
            if current.exists():
                current_stat = current.lstat()
                if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode):
                    raise R2EvidenceError("multipart-state-unavailable")
            current = current.parent
        if root.is_symlink() or root.exists() and not root.is_dir():
            raise R2EvidenceError("multipart-state-unavailable")
        if root.exists():
            try:
                os.chmod(root, 0o700)
            except OSError as error:
                raise R2EvidenceError("multipart-state-unavailable") from error

    def _validate_evidence_state_storage(self, path: Path) -> None:
        parent = path.parent
        if parent.exists():
            parent_stat = parent.lstat()
            if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
                raise R2EvidenceError("multipart-state-unavailable")
            try:
                os.chmod(parent, 0o700)
            except OSError as error:
                raise R2EvidenceError("multipart-state-unavailable") from error
        if path.exists() or path.is_symlink():
            state_stat = path.lstat()
            if stat.S_ISLNK(state_stat.st_mode) or not stat.S_ISREG(state_stat.st_mode):
                raise R2EvidenceError("multipart-state-unavailable")
            try:
                os.chmod(path, 0o600)
            except OSError as error:
                raise R2EvidenceError("multipart-state-unavailable") from error
    @staticmethod
    def _sync_evidence_directory(directory: Path) -> None:
        if os.name != "posix":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _evidence_destination_fingerprint(self) -> str:
        identity = {
            "provider": self.__class__.__module__,
            "endpoint": getattr(self.config, "endpoint", ""),
            "bucket": getattr(self.config, "bucket", ""),
            "dimension": getattr(self.config, "dimension_id", None),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _evidence_state_path(self, digest: str, final_key: str | None = None) -> Path:
        root = self.receipt_dir / "evidence-multipart" if self.receipt_dir is not None else Path(tempfile.gettempdir()) / "josh-room-evidence-state"
        identity = final_key or digest
        state_id = hashlib.sha256(b"josh-room-pcc-state-v2\0" + self._evidence_destination_fingerprint().encode() + b"\0" + identity.encode()).hexdigest()
        return root / f"{state_id}.json"

    @contextmanager
    def _evidence_lock(self, digest: str):
        self._validate_receipt_root()
        root = self.receipt_dir / "evidence-multipart" if self.receipt_dir is not None else Path(tempfile.gettempdir()) / "josh-room-evidence"
        if root.is_symlink() or root.exists() and not root.is_dir():
            raise R2EvidenceError("multipart-lock-unavailable")
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise R2EvidenceError("multipart-lock-unavailable") from error
        if self.receipt_dir is not None:
            try:
                os.chmod(self.receipt_dir, 0o700)
            except OSError as error:
                raise R2EvidenceError("multipart-lock-unavailable") from error
        try:
            os.chmod(root, 0o700)
        except OSError as error:
            raise R2EvidenceError("multipart-lock-unavailable") from error
        lock_id = hashlib.sha256(b"josh-room-pcc-lock-v2\0" + self._evidence_destination_fingerprint().encode() + b"\0" + digest.encode()).hexdigest()
        lock_path = root / f".{lock_id}.lock"
        if lock_path.is_symlink() or lock_path.exists() and not stat.S_ISREG(lock_path.lstat().st_mode):
            raise R2EvidenceError("multipart-lock-unavailable")
        try:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            os.chmod(lock_path, 0o600)
            handle = os.fdopen(descriptor, "r+b")
        except OSError as error:
            raise R2EvidenceError("multipart-lock-unavailable") from error
        with handle:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                return
            if _msvcrt is None:
                raise R2EvidenceError("multipart-lock-unsupported")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)

    def _load_evidence_state(self, key: str, digest: str, size: int) -> dict:
        self._validate_receipt_root()
        path = self._evidence_state_path(digest, key)
        self._validate_evidence_state_storage(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b""
        except OSError as error:
            raise R2EvidenceError("multipart-state-unavailable") from error
        if not raw:
            state = {
                "version": 1,
                "key": key,
                "sha256": digest,
                "size": size,
                "claim_key": evidence_claim_key(digest, key),
                "fence": secrets.token_hex(16),
                "stage": "claiming",
                "upload_id": None,
                "parts": [],
            }
            self._save_evidence_state(state)
            return state
        if len(raw) > _MAX_EVIDENCE_STATE_BYTES:
            raise R2EvidenceConflict("multipart-state-bounded")
        try:
            state = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise R2EvidenceConflict("multipart-state-invalid") from error
        required = {"version", "key", "sha256", "size", "claim_key", "fence", "stage", "upload_id", "parts"}
        if not isinstance(state, dict) or set(state) != required:
            raise R2EvidenceConflict("multipart-state-invalid")
        if (
            type(state["version"]) is not int
            or state["version"] != 1
            or type(state["key"]) is not str
            or state["key"] != key
            or type(state["sha256"]) is not str
            or state["sha256"] != digest
            or type(state["size"]) is not int
            or state["size"] != size
            or type(state["claim_key"]) is not str
            or state["claim_key"] != evidence_claim_key(digest, key)
            or type(state["fence"]) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", state["fence"])
            or type(state["stage"]) is not str
            or state["stage"] not in {"claiming", "claimed", "uploading", "completing"}
            or state["upload_id"] is not None and (not isinstance(state["upload_id"], str) or len(state["upload_id"]) > 2048)
            or not isinstance(state["parts"], list)
            or len(state["parts"]) > _MAX_EVIDENCE_PARTS
        ):
            raise R2EvidenceConflict("multipart-state-invalid")
        seen: set[int] = set()
        for part in state["parts"]:
            if (
                not isinstance(part, dict)
                or set(part) != {"PartNumber", "ETag"}
                or type(part["PartNumber"]) is not int
                or not 1 <= part["PartNumber"] <= _MAX_EVIDENCE_PARTS
                or part["PartNumber"] in seen
                or not isinstance(part["ETag"], str)
                or not 0 < len(part["ETag"]) <= 4096
            ):
                raise R2EvidenceConflict("multipart-state-invalid")
            seen.add(part["PartNumber"])
        return state

    def _save_evidence_state(self, state: dict) -> None:
        self._validate_receipt_root()
        path = self._evidence_state_path(state["sha256"], state["key"])
        encoded = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > _MAX_EVIDENCE_STATE_BYTES:
            raise R2EvidenceError("multipart-state-bounded")
        if path.parent.is_symlink() or path.parent.exists() and not path.parent.is_dir():
            raise R2EvidenceError("multipart-state-unavailable")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise R2EvidenceError("multipart-state-unavailable") from error
        try:
            os.chmod(path.parent, 0o700)
        except OSError as error:
            raise R2EvidenceError("multipart-state-unavailable") from error
        self._validate_evidence_state_storage(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_evidence_directory(path.parent)
        except OSError as error:
            raise R2EvidenceError("multipart-state-unavailable") from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _clear_evidence_state(self, digest: str, final_key: str | None = None) -> bool:
        self._validate_receipt_root()
        path = self._evidence_state_path(digest, final_key)
        self._validate_evidence_state_storage(path)
        try:
            path.unlink(missing_ok=True)
            self._sync_evidence_directory(path.parent)
        except OSError:
            return False
        return True

    def _read_evidence_claim(self, claim_key: str, digest: str, size: int) -> dict | None:
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=claim_key)
        except ClientError as error:
            if _not_found(error):
                return None
            raise
        body = response["Body"].read(_MAX_EVIDENCE_CLAIM_BYTES + 1)
        if len(body) > _MAX_EVIDENCE_CLAIM_BYTES:
            raise R2EvidenceConflict("claim-bounded")
        try:
            claim = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise R2EvidenceConflict("claim-invalid") from error
        if (
            not isinstance(claim, dict)
            or set(claim) != {"version", "sha256", "size", "fence"}
            or claim["version"] != 1
            or claim["sha256"] != digest
            or type(claim["size"]) is not int
            or claim["size"] != size
            or not isinstance(claim["fence"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", claim["fence"])
        ):
            raise R2EvidenceConflict("claim-conflict")
        return claim

    def _claim_evidence_multipart(self, key: str, digest: str, size: int) -> dict:
        state = self._load_evidence_state(key, digest, size)
        claim_key = state["claim_key"]
        claim = {
            "version": 1,
            "sha256": digest,
            "size": size,
            "fence": state["fence"],
        }
        body = json.dumps(claim, separators=(",", ":"), sort_keys=True).encode()
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=claim_key,
                Body=body,
                ContentLength=len(body),
                IfNoneMatch="*",
                Metadata={"sha256": digest, "size": str(size)},
            )
        except ClientError as error:
            if not _is_precondition(error):
                raise
            existing = self._read_evidence_claim(claim_key, digest, size)
            if existing is None or existing["fence"] != state["fence"]:
                raise R2EvidenceConflict("claim-conflict") from error
        except (BotoCoreError, TimeoutError) as error:
            existing = self._read_evidence_claim(claim_key, digest, size)
            if existing is None:
                raise self._map_evidence_error(error, published=False) from error
            if existing["fence"] != state["fence"]:
                raise R2EvidenceConflict("claim-conflict") from error
        state["stage"] = "claimed"
        self._save_evidence_state(state)
        return state

    def _evidence_final_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if _not_found(error):
                return False
            raise
        return True

    def _verify_evidence_or_absent(self, key: str, digest: str, size: int) -> bool:
        try:
            self._verify_evidence_remote(key, digest, size)
        except ValueError:
            if self._evidence_final_exists(key):
                raise R2EvidenceReadbackMismatch(published=True)
            return False
        return True

    def _reset_evidence_upload(self, state: dict, key: str, retries: int) -> None:
        upload_id = state.get("upload_id")
        if upload_id is None:
            return
        try:
            self._abort_evidence_upload(key, upload_id, retries)
        except R2EvidenceAbortFailure:
            self._save_evidence_state(state)
            raise
        state["upload_id"] = None
        state["parts"] = []
        state["stage"] = "claimed"
        self._save_evidence_state(state)

    def _put_evidence_multipart(self, key, source_factory, size, digest, started):
        with self._evidence_lock(digest):
            return self._put_evidence_multipart_locked(key, source_factory, size, digest, started)

    def _put_evidence_multipart_locked(self, key, source_factory, size, digest, started):
        retries = 0
        max_attempts = max(1, self.config.max_attempts)
        try:
            if self._verify_evidence_or_absent(key, digest, size):
                self._clear_evidence_state(digest, key)
                return R2EvidenceReceipt(
                    key,
                    digest,
                    size,
                    R2EvidenceMetrics(size, 0, 0, True, True, False, _latency_ms(started)),
                )
        except R2EvidenceReadbackMismatch as error:
            self._clear_evidence_state(digest, key)
            raise R2EvidenceConflict(retries=retries) from error
        except (ClientError, BotoCoreError, TimeoutError) as error:
            raise self._map_evidence_error(error, published=False) from error

        try:
            state = self._claim_evidence_multipart(key, digest, size)
        except (ClientError, BotoCoreError, TimeoutError) as error:
            raise self._map_evidence_error(error, published=False) from error
        upload_id = state.get("upload_id")
        source = None
        committed = False
        preserve_state = False
        try:
            if upload_id is None:
                state["stage"] = "claimed"
                self._save_evidence_state(state)
                for attempt in range(max_attempts):
                    try:
                        upload_id = self.client.create_multipart_upload(
                            Bucket=self.config.bucket,
                            Key=key,
                            Metadata={"sha256": digest},
                        )["UploadId"]
                        if not isinstance(upload_id, str) or not upload_id:
                            raise ValueError("multipart upload id is invalid")
                        state["upload_id"] = upload_id
                        state["parts"] = []
                        state["stage"] = "uploading"
                        self._save_evidence_state(state)
                        break
                    except (ClientError, BotoCoreError, TimeoutError) as error:
                        retries += 1
                        if not _is_retryable(error) or attempt + 1 >= max_attempts:
                            raise self._map_evidence_error(error, retries=retries, published=False) from error
                        time.sleep(min(0.01 * (2 ** min(retries - 1, 4)), 0.1))
                if upload_id is None:
                    raise R2EvidenceRetryable(retries=retries)

            source = source_factory()
            prior_parts = {part["PartNumber"]: part for part in state["parts"]}
            parts: list[dict] = []
            part_number = 1
            observed_size = 0
            while True:
                chunk = source.read(self.config.multipart_chunk_size)
                if not chunk:
                    break
                observed_size += len(chunk)
                if part_number > _MAX_EVIDENCE_PARTS:
                    raise R2EvidenceError("multipart-part-limit")
                prior = prior_parts.get(part_number)
                if prior is None:
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
                            etag = result["ETag"]
                            if not isinstance(etag, str) or not etag:
                                raise ValueError("multipart part token is invalid")
                            prior = {"ETag": etag, "PartNumber": part_number}
                            prior_parts[part_number] = prior
                            state["parts"] = [prior_parts[number] for number in sorted(prior_parts)]
                            state["stage"] = "uploading"
                            self._save_evidence_state(state)
                            break
                        except (ClientError, BotoCoreError, TimeoutError) as error:
                            if _is_stale_multipart_error(error):
                                state["upload_id"] = None
                                state["parts"] = []
                                state["stage"] = "claimed"
                                self._save_evidence_state(state)
                                raise R2EvidenceRetryable("multipart-expired", retries=retries) from error
                            retries += 1
                            part_retries += 1
                            if not _is_retryable(error) or retries >= max_attempts:
                                raise self._map_evidence_error(error, retries=retries, published=False) from error
                            time.sleep(min(0.01 * (2 ** min(part_retries - 1, 4)), 0.1))
                parts.append({"ETag": prior["ETag"], "PartNumber": part_number})
                report_progress("upload", f"Uploading encrypted evidence • {_percent(observed_size, size)}%", current=observed_size, total=size)
                part_number += 1
            if observed_size != size:
                raise R2EvidenceReadbackMismatch(published=False, retries=retries)

            state["stage"] = "completing"
            state["parts"] = parts
            self._save_evidence_state(state)
            for attempt in range(max_attempts):
                try:
                    self.client.complete_multipart_upload(
                        Bucket=self.config.bucket,
                        Key=key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    )
                except (ClientError, BotoCoreError, TimeoutError) as error:
                    if _is_stale_multipart_error(error):
                        state["upload_id"] = None
                        state["parts"] = []
                        state["stage"] = "claimed"
                        self._save_evidence_state(state)
                        upload_id = None
                        raise R2EvidenceRetryable("multipart-expired", retries=retries) from error
                    if not _is_retryable(error):
                        raise self._map_evidence_error(error, retries=retries, published=False) from error
                    retries += 1
                    try:
                        if self._verify_evidence_or_absent(key, digest, size):
                            committed = True
                            self._clear_evidence_state(digest, key)
                            return R2EvidenceReceipt(
                                key,
                                digest,
                                size,
                                R2EvidenceMetrics(size, len(parts), retries, False, True, False, _latency_ms(started)),
                                "ambiguous-complete-verified",
                            )
                    except R2EvidenceReadbackMismatch:
                        committed = True
                        self._clear_evidence_state(digest, key)
                        raise
                    if attempt + 1 >= max_attempts:
                        code = str(error.response.get("Error", {}).get("Code")) if isinstance(error, ClientError) else ""
                        if code in {"429", "SlowDown", "Throttling"}:
                            raise self._map_evidence_error(error, retries=retries, published=False) from error
                        preserve_state = True
                        raise R2EvidenceAmbiguous(retries=retries) from error
                    time.sleep(min(0.01 * (2 ** min(retries - 1, 4)), 0.1))
                    continue
                try:
                    if not self._verify_evidence_or_absent(key, digest, size):
                        preserve_state = True
                        raise R2EvidenceAmbiguous(retries=retries)
                except R2EvidenceReadbackMismatch:
                    committed = True
                    self._clear_evidence_state(digest, key)
                    raise
                committed = True
                self._clear_evidence_state(digest, key)
                return R2EvidenceReceipt(
                    key,
                    digest,
                    size,
                    R2EvidenceMetrics(size, len(parts), retries, False, True, False, _latency_ms(started)),
                )
            preserve_state = True
            raise R2EvidenceAmbiguous(retries=retries)
        except R2EvidenceAmbiguous:
            preserve_state = True
            raise
        except R2EvidenceError:
            if upload_id is not None and not committed:
                self._reset_evidence_upload(state, key, retries)
            raise
        except ValueError as error:
            if upload_id is not None and not committed:
                self._reset_evidence_upload(state, key, retries)
            raise R2EvidenceReadbackMismatch(published=False, retries=retries) from error
        except (ClientError, BotoCoreError, TimeoutError) as error:
            if upload_id is not None and not committed:
                self._reset_evidence_upload(state, key, retries)
            raise self._map_evidence_error(error, retries=retries, published=False) from error
        finally:
            if source is not None and getattr(source_factory, "owns_source", True):
                try:
                    source.close()
                except (AttributeError, OSError):
                    pass
            if preserve_state:
                self._save_evidence_state(state)

    def _abort_evidence_upload(self, key: str, upload_id: str, retries: int) -> None:
        try:
            self.client.abort_multipart_upload(Bucket=self.config.bucket, Key=key, UploadId=upload_id)
        except Exception as error:
            raise R2EvidenceAbortFailure(retries=retries) from error

    def _verify_evidence_remote(
        self,
        key: str,
        digest: str,
        size: int | None,
        *,
        verify_body: bool = True,
    ) -> int:
        try:
            head = self.client.head_object(Bucket=self.config.bucket, Key=key)
        except ClientError as error:
            if _not_found(error):
                raise ValueError("evidence object is unavailable") from error
            raise
        observed_size = int(head.get("ContentLength", -1))
        if observed_size > self.config.max_bytes or size is not None and observed_size != size:
            raise ValueError("evidence object size mismatch")
        if not verify_body:
            return observed_size
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
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(error, ClientError) else None
        if status is not None and str(status) in {"401", "403", "408", "429", "500", "502", "503", "504"}:
            code = str(status)
        if isinstance(error, TimeoutError) or error.__class__.__name__ in {"ReadTimeoutError", "ConnectTimeoutError"} or code in {"408", "RequestTimeout", "504", "GatewayTimeout"}:
            return R2EvidenceTimeout(published=published, retries=retries)
        if code in {"429", "SlowDown", "Throttling", "TooManyRequests"}:
            return R2EvidenceRateLimited(published=published, retries=retries)
        if code in {"401", "403", "Unauthorized", "Forbidden", "ExpiredToken", "InvalidToken", "TokenRefreshRequired", "InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied"}:
            return R2EvidenceCredentialFailure(published=published, retries=retries)
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


def _provider_code(error: BaseException) -> str:
    if not isinstance(error, ClientError):
        return ""
    code = str(error.response.get("Error", {}).get("Code"))
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if status is not None and str(status) in {"401", "403", "404", "408", "409", "412", "425", "429", "500", "502", "503", "504"}:
        return str(status)
    return code


def _is_precondition(error: ClientError) -> bool:
    return _provider_code(error) in {"409", "412", "PreconditionFailed", "ConditionalRequestConflict"}


def _not_found(error: ClientError) -> bool:
    return _provider_code(error) in {"404", "NoSuchKey", "NotFound"}


def _is_stale_multipart_error(error: BaseException) -> bool:
    return _provider_code(error) in {"404", "NoSuchUpload", "InvalidUploadId", "NoSuchUploadId"}


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, ClientError):
        return _provider_code(error) in {
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
