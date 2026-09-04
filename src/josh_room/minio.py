from dataclasses import dataclass

from botocore.config import Config

from .config import ConnectionConfig, DimensionConfig, resolve_dimension
from .keyring import lookup
from .object_store import ObjectStore
from .r2 import R2Backend
from .s3 import BucketAccessDenied as _S3BucketAccessDenied
from .s3 import BucketListForbidden as _S3BucketListForbidden
from .s3 import check_bucket_access as _check_bucket_access
from .s3 import create_bucket as _create_bucket
from .s3 import list_buckets as _list_buckets
from .s3 import validate_bucket_name as _validate_bucket_name


class BucketListForbidden(_S3BucketListForbidden):
    def __init__(self, message: str, connection: ConnectionConfig | None = None, context=None, **_kwargs):
        connection = connection or context
        super().__init__(message, "minio", connection.connection_id if connection else None)


class BucketAccessDenied(_S3BucketAccessDenied):
    def __init__(self, message: str, connection: ConnectionConfig | None = None, context=None, **_kwargs):
        connection = connection or context
        super().__init__(message, "minio", connection.connection_id if connection else None)


def validate_bucket_name(bucket: str) -> str:
    return _validate_bucket_name(bucket)


def client_for_connection(connection: ConnectionConfig):
    if connection.provider != "minio":
        raise ValueError("bucket operations require a MinIO connection")
    if connection.auth_state == "disconnected":
        raise RuntimeError("MinIO connection is disconnected; reconnect before use")
    import boto3

    credentials = lookup(connection.credential_profile, allow_runtime=False)
    verify = connection.option("ca_bundle") or connection.option("verify_tls", True)
    return boto3.client(
        "s3",
        endpoint_url=connection.endpoint,
        region_name=connection.region,
        aws_access_key_id=credentials["access-key-id"],
        aws_secret_access_key=credentials["secret-access-key"],
        aws_session_token=credentials.get("session-token"),
        verify=verify,
        config=Config(
            connect_timeout=connection.option("timeout_seconds", 60),
            read_timeout=connection.option("timeout_seconds", 60),
            retries={"max_attempts": connection.option("max_attempts", 4), "mode": "standard"},
            s3={"addressing_style": "path" if connection.option("path_style", True) else "virtual"},
        ),
    )


def list_buckets(connection: ConnectionConfig, client=None) -> list[str]:
    client = client or client_for_connection(connection)
    return _list_buckets(client, "MinIO", error_type=BucketListForbidden, context=connection)


def create_bucket(connection: ConnectionConfig, bucket: str, client=None) -> str:
    client = client or client_for_connection(connection)
    return _create_bucket(client, bucket, region=connection.region)


def check_bucket_access(connection: ConnectionConfig, bucket: str, client=None) -> str:
    client = client or client_for_connection(connection)
    return _check_bucket_access(client, bucket, "MinIO", error_type=BucketAccessDenied, context=connection)


@dataclass(frozen=True)
class MinioConfig:
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
    verify_tls: bool = True
    ca_bundle: str | None = None
    path_style: bool = True
    dimension_id: str | None = None
    auth_state: str = "configured"

    @classmethod
    def from_dimension(cls, dimension: DimensionConfig) -> "MinioConfig":
        if dimension.provider != "minio":
            raise ValueError("selected Dimension is not a MinIO Dimension")
        return cls(endpoint=dimension.endpoint, bucket=dimension.bucket, credential_profile=dimension.credential_profile, region=dimension.region, catalog_key=dimension.catalog_key, multipart_threshold=dimension.option("multipart_threshold", cls.multipart_threshold), multipart_chunk_size=dimension.option("multipart_chunk_size", cls.multipart_chunk_size), max_bytes=dimension.option("max_bytes", cls.max_bytes), timeout_seconds=dimension.option("timeout_seconds", cls.timeout_seconds), max_attempts=dimension.option("max_attempts", cls.max_attempts), verify_tls=dimension.option("verify_tls", True), ca_bundle=dimension.option("ca_bundle"), path_style=dimension.option("path_style", True), dimension_id=dimension.dimension_id, auth_state=dimension.auth_state)

    @classmethod
    def from_private(cls, config: dict | DimensionConfig, dimension_id: str | None = None) -> "MinioConfig":
        if isinstance(config, DimensionConfig):
            return cls.from_dimension(config)
        if dimension_id:
            return cls.from_dimension(resolve_dimension(config, dimension_id))
        values = (config or {}).get("minio")
        if not values:
            raise ValueError("private MinIO configuration is unavailable")
        names = ("region", "catalog_key", "multipart_threshold", "multipart_chunk_size", "max_bytes", "timeout_seconds", "max_attempts", "verify_tls", "ca_bundle", "path_style", "auth_state")
        return cls(values["endpoint"], values["bucket"], values["credential_profile"], **{name: values[name] for name in names if name in values}, dimension_id="minio")



class MinioBackend(R2Backend, ObjectStore):
    def _require_connected(self):
        if self.config.auth_state == "disconnected":
            raise RuntimeError("MinIO connection is disconnected; reconnect before use")

    def put_bytes(self, key: str, body: bytes):
        self._require_connected()
        return super().put_bytes(key, body)

    def put_file(self, key: str, path):
        self._require_connected()
        return super().put_file(key, path)

    def get_bytes(self, key: str, expected_digest=None, expected_size=None) -> bytes:
        self._require_connected()
        return super().get_bytes(key, expected_digest=expected_digest, expected_size=expected_size)

    def download_file(self, key: str, destination, expected_digest, expected_size) -> None:
        self._require_connected()
        return super().download_file(key, destination, expected_digest, expected_size)

    def verify_object(self, key: str, expected_digest: str, expected_size: int):
        self._require_connected()
        return super().verify_object(key, expected_digest, expected_size)

    def read_catalog(self):
        self._require_connected()
        return super().read_catalog()

    def conditional_catalog_put(self, body: bytes, expected_etag):
        self._require_connected()
        return super().conditional_catalog_put(body, expected_etag)

    def read_control(self, key: str, max_bytes: int):
        self._require_connected()
        return super().read_control(key, max_bytes)

    def create_control(self, key: str, body: bytes):
        self._require_connected()
        return super().create_control(key, body)

    def replace_control(self, key: str, body: bytes, expected_etag: str):
        self._require_connected()
        return super().replace_control(key, body, expected_etag)

    def delete_object(self, key: str) -> None:
        self._require_connected()
        return super().delete_object(key)

    def _client_from_keyring(self):
        self._require_connected()
        import boto3

        credentials = lookup(self.config.credential_profile, allow_runtime=False)
        verify = self.config.ca_bundle if self.config.ca_bundle else self.config.verify_tls
        return boto3.client("s3", endpoint_url=self.config.endpoint, region_name=self.config.region,
            aws_access_key_id=credentials["access-key-id"], aws_secret_access_key=credentials["secret-access-key"],
            aws_session_token=credentials.get("session-token"), verify=verify,
            config=Config(connect_timeout=self.config.timeout_seconds, read_timeout=self.config.timeout_seconds,
                retries={"max_attempts": self.config.max_attempts, "mode": "standard"},
                s3={"addressing_style": "path" if self.config.path_style else "virtual"}))
