import re
from dataclasses import dataclass

from botocore.config import Config
from botocore.exceptions import ClientError

from .config import ConnectionConfig, DimensionConfig, resolve_dimension
from .keyring import lookup
from .object_store import ObjectStore
from .r2 import R2Backend

_BUCKET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?$")


class BucketListForbidden(PermissionError):
    def __init__(self, message: str, connection: ConnectionConfig | None = None):
        self.result = {
            "error_code": "bucket-list-forbidden",
            "recoverable": True,
        }
        if connection is not None:
            self.result.update({"connection_id": connection.connection_id, "provider": connection.provider})
        super().__init__(message)


class BucketAccessDenied(PermissionError):
    def __init__(self, message: str, connection: ConnectionConfig | None = None):
        self.result = {
            "error_code": "bucket-access-denied",
            "recoverable": True,
        }
        if connection is not None:
            self.result.update({"connection_id": connection.connection_id, "provider": connection.provider})
        super().__init__(message)


def validate_bucket_name(bucket: str) -> str:
    if not isinstance(bucket, str) or not _BUCKET_NAME.fullmatch(bucket):
        raise ValueError("bucket name must be 3-63 lowercase letters, numbers, dots, or hyphens")
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ValueError("bucket name contains an invalid separator")
    return bucket


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
    try:
        response = client.list_buckets()
    except ClientError as error:
        details = error.response.get("Error", {})
        code = str(details.get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code.lower() in {"accessdenied", "forbidden", "unauthorized"} or status == 403:
            raise BucketListForbidden("MinIO bucket listing is forbidden", connection) from error
        raise
    return sorted(bucket["Name"] for bucket in response.get("Buckets", []) if isinstance(bucket, dict) and bucket.get("Name"))


def create_bucket(connection: ConnectionConfig, bucket: str, client=None) -> str:
    bucket = validate_bucket_name(bucket)
    client = client or client_for_connection(connection)
    kwargs = {"Bucket": bucket}
    if connection.region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": connection.region}
    client.create_bucket(**kwargs)
    return bucket


def check_bucket_access(connection: ConnectionConfig, bucket: str, client=None) -> str:
    bucket = validate_bucket_name(bucket)
    client = client or client_for_connection(connection)
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as error:
        details = error.response.get("Error", {})
        code = str(details.get("Code", ""))
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code.lower() in {"accessdenied", "forbidden", "unauthorized", "nosuchbucket"} or status in {403, 404}:
            raise BucketAccessDenied("MinIO bucket is unavailable or access is denied", connection) from error
        raise
    return bucket


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
