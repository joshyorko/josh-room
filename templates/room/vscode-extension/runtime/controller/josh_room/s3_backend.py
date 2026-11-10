"""AWS and HTTPS S3-compatible adapter over the existing object-store engine."""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlsplit

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import DimensionConfig, default_s3_endpoint, resolve_dimension
from .keyring import lookup
from .minio import MinioBackend
from .s3 import BucketAccessDenied, BucketListForbidden
from .s3 import check_bucket_access as _check_bucket_access
from .s3 import create_bucket as _create_bucket
from .s3 import list_buckets as _list_buckets


class S3ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        dimension_id: str | None = None,
        connection_id: str | None = None,
    ):
        self.result = {
            "provider": "s3",
            "error_code": code,
            "recoverable": code not in {"credentials-invalid", "signature-failure"},
        }
        if dimension_id:
            self.result["dimension_id"] = dimension_id
        if connection_id:
            self.result["connection_id"] = connection_id
        super().__init__(f"Amazon S3 operation failed: {code}")


def _sanitized_error(error, context):
    if isinstance(error, ClientError):
        details = error.response.get("Error", {})
        code = str(details.get("Code", "")).lower()
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {
            "expiredtoken",
            "invalidaccesskeyid",
            "invalidtoken",
            "signaturedoesnotmatch",
        }:
            label = (
                "signature-failure"
                if code == "signaturedoesnotmatch"
                else "credentials-invalid"
            )
        elif code in {
            "authorizationheadermalformed",
            "permanentredirect",
            "incorrectendpoint",
        }:
            label = "region-or-endpoint-mismatch"
        elif code in {"accessdenied", "forbidden", "unauthorized"} or status == 403:
            label = "access-denied"
        elif code == "nosuchbucket" or status == 404:
            label = "bucket-unavailable"
        else:
            label = "request-failed"
    else:
        label = "transport-unavailable"
    if getattr(context, "connection_id", None):
        return S3ProviderError(label, connection_id=context.connection_id)
    return S3ProviderError(label, dimension_id=getattr(context, "dimension_id", None))


@contextmanager
def sdk_error_boundary(context):
    """Keep SDK control flow intact until an S3 operation reaches the caller."""
    try:
        yield
    except (ClientError, BotoCoreError) as error:
        if getattr(context, "provider", None) != "s3":
            raise
        raise _sanitized_error(error, context) from None


def client_for_connection(connection):
    if connection.provider != "s3":
        raise ValueError("bucket operations require an S3 connection")
    if connection.auth_state == "disconnected":
        raise RuntimeError("S3 connection is disconnected; reconnect before use")
    import boto3

    try:
        credentials = lookup(connection.credential_profile, allow_runtime=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise S3ProviderError(
            "credentials-unavailable", connection_id=connection.connection_id
        ) from None
    return boto3.client(
        "s3",
        endpoint_url=connection.endpoint,
        region_name=connection.region,
        aws_access_key_id=credentials["access-key-id"],
        aws_secret_access_key=credentials["secret-access-key"],
        aws_session_token=credentials.get("session-token"),
        config=Config(
            connect_timeout=connection.option("timeout_seconds", 60),
            read_timeout=connection.option("timeout_seconds", 60),
            retries={
                "max_attempts": connection.option("max_attempts", 4),
                "mode": "standard",
            },
            s3={
                "addressing_style": "path"
                if connection.option("path_style", False)
                else "virtual"
            },
        ),
    )


def list_buckets(connection, client=None):
    try:
        return _list_buckets(
            client or client_for_connection(connection),
            "Amazon S3",
            error_type=BucketListForbidden,
            context=connection.connection_id,
        )
    except BucketListForbidden as error:
        raise _sanitized_error(error.__cause__, connection) from None
    except (ClientError, BotoCoreError) as error:
        raise _sanitized_error(error, connection) from None


def create_bucket(connection, bucket, client=None):
    try:
        return _create_bucket(
            client or client_for_connection(connection),
            bucket,
            region=connection.region,
        )
    except (ClientError, BotoCoreError) as error:
        raise _sanitized_error(error, connection) from None


def check_bucket_access(connection, bucket, client=None):
    try:
        return _check_bucket_access(
            client or client_for_connection(connection),
            bucket,
            "Amazon S3",
            error_type=BucketAccessDenied,
            context=connection.connection_id,
        )
    except BucketAccessDenied as error:
        raise _sanitized_error(error.__cause__, connection) from None
    except (ClientError, BotoCoreError) as error:
        raise _sanitized_error(error, connection) from None


@dataclass(frozen=True)
class S3Config:
    provider: ClassVar[str] = "s3"
    endpoint: str
    bucket: str
    credential_profile: str
    region: str = "us-east-1"
    catalog_key: str = "catalog.jroom.age"
    multipart_threshold: int = 64 * 1024 * 1024
    multipart_chunk_size: int = 16 * 1024 * 1024
    max_bytes: int = 8 * 1024 * 1024 * 1024
    timeout_seconds: int = 60
    max_attempts: int = 4
    path_style: bool = False
    verify_tls: bool = True
    ca_bundle: str | None = None
    dimension_id: str | None = None
    auth_state: str = "configured"

    def __post_init__(self):
        if not isinstance(self.endpoint, str) or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in self.endpoint
        ):
            raise ValueError("S3 endpoint contains whitespace or control characters")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("S3 endpoint must use HTTPS without userinfo")
        if (
            not isinstance(self.region, str)
            or not self.region.strip()
            or self.region.lower() == "auto"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", self.region)
        ):
            raise ValueError("S3 region must be explicit")
        if self.verify_tls is not True:
            raise ValueError("S3 TLS certificate verification cannot be disabled")

    @classmethod
    def from_dimension(cls, dimension: DimensionConfig) -> "S3Config":
        if dimension.provider != "s3":
            raise ValueError("selected Dimension is not an S3 Dimension")
        return cls(
            endpoint=dimension.endpoint,
            bucket=dimension.bucket,
            credential_profile=dimension.credential_profile,
            region=dimension.region,
            catalog_key=dimension.catalog_key,
            multipart_threshold=dimension.option(
                "multipart_threshold", cls.multipart_threshold
            ),
            multipart_chunk_size=dimension.option(
                "multipart_chunk_size", cls.multipart_chunk_size
            ),
            max_bytes=dimension.option("max_bytes", cls.max_bytes),
            timeout_seconds=dimension.option("timeout_seconds", cls.timeout_seconds),
            max_attempts=dimension.option("max_attempts", cls.max_attempts),
            path_style=dimension.option("path_style", False),
            verify_tls=True,
            dimension_id=dimension.dimension_id,
            auth_state=dimension.auth_state,
        )

    @classmethod
    def from_private(
        cls, config: dict | DimensionConfig, dimension_id: str | None = None
    ) -> "S3Config":
        if isinstance(config, DimensionConfig):
            return cls.from_dimension(config)
        if dimension_id:
            return cls.from_dimension(resolve_dimension(config, dimension_id))
        values = (config or {}).get("s3")
        if not values:
            raise ValueError("private S3 configuration is unavailable")
        if not isinstance(values.get("region"), str) or not values["region"].strip():
            raise ValueError("private S3 configuration requires an explicit region")
        return cls(
            endpoint=values.get("endpoint") or default_s3_endpoint(values["region"]),
            bucket=values["bucket"],
            credential_profile=values["credential_profile"],
            region=values["region"],
            path_style=values.get("path_style", False),
            dimension_id="s3",
        )


class S3Backend(MinioBackend):
    """S3 provider identity with the same encrypted Restic object-store behavior."""

    def _require_connected(self):
        if self.config.auth_state == "disconnected":
            raise S3ProviderError("connection-disconnected", dimension_id=self.config.dimension_id)
