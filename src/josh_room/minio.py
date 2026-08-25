from dataclasses import dataclass

from botocore.config import Config

from .keyring import lookup
from .object_store import ObjectStore
from .r2 import R2Backend


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

    @classmethod
    def from_private(cls, config: dict) -> "MinioConfig":
        values = (config or {}).get("minio")
        if not values:
            raise ValueError("private MinIO configuration is unavailable")
        names = ("region", "catalog_key", "multipart_threshold", "multipart_chunk_size", "max_bytes", "timeout_seconds", "max_attempts", "verify_tls", "ca_bundle", "path_style")
        return cls(values["endpoint"], values["bucket"], values["credential_profile"], **{name: values[name] for name in names if name in values})


class MinioBackend(R2Backend, ObjectStore):
    def _client_from_keyring(self):
        import boto3
        credentials = lookup(self.config.credential_profile)
        verify = self.config.ca_bundle if self.config.ca_bundle else self.config.verify_tls
        return boto3.client("s3", endpoint_url=self.config.endpoint, region_name=self.config.region,
            aws_access_key_id=credentials["access-key-id"], aws_secret_access_key=credentials["secret-access-key"],
            aws_session_token=credentials.get("session-token"), verify=verify,
            config=Config(connect_timeout=self.config.timeout_seconds, read_timeout=self.config.timeout_seconds,
                retries={"max_attempts": self.config.max_attempts, "mode": "standard"},
                s3={"addressing_style": "path" if self.config.path_style else "virtual"}))
