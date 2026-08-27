import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .keyring import available

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_IDENTIFIER = re.compile(_IDENTIFIER_PATTERN)
_COMMON_DIMENSION_FIELDS = {"bucket", "catalog_key", "credential_profile", "display_name", "endpoint", "provider", "region"}
_PROVIDER_DIMENSION_OPTIONS = {
    "r2": {"max_attempts", "max_bytes", "multipart_chunk_size", "multipart_threshold", "temporary_credentials", "timeout_seconds"},
    "minio": {"ca_bundle", "max_attempts", "max_bytes", "multipart_chunk_size", "multipart_threshold", "path_style", "timeout_seconds", "verify_tls"},
}
_LEGACY_DIMENSION_NAMES = {"r2": "Cloudflare R2", "minio": "MinIO"}
_BOOLEAN_DIMENSION_OPTIONS = {"path_style", "temporary_credentials", "verify_tls"}
_POSITIVE_INTEGER_DIMENSION_OPTIONS = {"max_attempts", "max_bytes", "multipart_chunk_size", "multipart_threshold", "timeout_seconds"}
_STRING_DIMENSION_OPTIONS = {"ca_bundle"}


def _validate_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Dimension {name} must match {_IDENTIFIER_PATTERN}")


def _validate_option(name: str, value: object) -> None:
    if name in _BOOLEAN_DIMENSION_OPTIONS and type(value) is not bool:
        raise TypeError(f"Dimension {name.replace('_', ' ')} must be a boolean")
    if name in _POSITIVE_INTEGER_DIMENSION_OPTIONS and (type(value) is not int or value < 1):
        raise ValueError(f"Dimension {name.replace('_', ' ')} must be a positive integer")
    if name in _STRING_DIMENSION_OPTIONS and not isinstance(value, str):
        raise TypeError(f"Dimension {name.replace('_', ' ')} must be a string")


@dataclass(frozen=True)
class DimensionConfig:
    dimension_id: str
    display_name: str
    provider: str
    endpoint: str
    bucket: str
    credential_profile: str
    catalog_key: str = "catalog.jroom.age"
    region: str = "auto"
    options: tuple[tuple[str, object], ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self):
        if self.provider not in _PROVIDER_DIMENSION_OPTIONS:
            raise ValueError(f"unsupported Dimension provider: {self.provider}")
        for name in ("dimension_id", "display_name", "endpoint", "bucket", "credential_profile", "catalog_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"Dimension {name} must be a non-empty string")
        _validate_identifier("dimension id", self.dimension_id)
        _validate_identifier("catalog key", self.catalog_key)
        if not isinstance(self.region, str):
            raise TypeError("Dimension region must be a string")
        if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in self.endpoint):
            raise ValueError("Dimension endpoint must not contain whitespace or control characters")
        try:
            endpoint = urlsplit(self.endpoint)
        except ValueError as error:
            raise ValueError("Dimension endpoint is invalid") from error
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ValueError("Dimension endpoint must use http/https with a hostname")
        if endpoint.username is not None or endpoint.password is not None or "@" in endpoint.netloc:
            raise ValueError("Dimension endpoint must not contain userinfo")
        unsupported = sorted(name for name, _value in self.options if name not in _PROVIDER_DIMENSION_OPTIONS[self.provider])
        if unsupported:
            raise ValueError(f"unsupported Dimension setting: {unsupported[0]}")
        for name, value in self.options:
            _validate_option(name, value)

    @classmethod
    def from_private(cls, dimension_id: str, body: dict) -> "DimensionConfig":
        if not isinstance(body, dict):
            raise TypeError("Dimension configuration must be an object")
        provider = body.get("provider")
        allowed = _COMMON_DIMENSION_FIELDS | _PROVIDER_DIMENSION_OPTIONS.get(provider, set())
        unsupported = sorted(set(body) - allowed)
        if unsupported:
            raise ValueError(f"unsupported Dimension setting: {unsupported[0]}")
        missing = sorted({"display_name", "provider", "endpoint", "bucket", "credential_profile"} - set(body))
        if missing:
            raise ValueError(f"missing Dimension setting: {missing[0]}")
        default_region = "auto" if provider == "r2" else "us-east-1"
        option_names = _PROVIDER_DIMENSION_OPTIONS.get(provider, set())
        return cls(
            dimension_id=dimension_id,
            display_name=body["display_name"],
            provider=provider,
            endpoint=body["endpoint"],
            bucket=body["bucket"],
            credential_profile=body["credential_profile"],
            catalog_key=body.get("catalog_key", "catalog.jroom.age"),
            region=body.get("region", default_region),
            options=tuple(sorted((name, body[name]) for name in option_names if name in body)),
        )

    def to_private(self) -> dict:
        return {
            "display_name": self.display_name,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "credential_profile": self.credential_profile,
            "catalog_key": self.catalog_key,
            "region": self.region,
            **dict(self.options),
        }

    def option(self, name: str, default=None):
        return dict(self.options).get(name, default)


class DimensionRegistry:
    """Named Dimensions over the existing concrete R2 and MinIO backends."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._dimensions = dimension_configs(self.config)

    @property
    def dimensions(self) -> dict[str, DimensionConfig]:
        return dict(self._dimensions)

    def select(self, dimension_id: str | None = None) -> DimensionConfig:
        selected = dimension_id or self.config.get("default_dimension")
        if not selected and self.config.get("default_backend") in self._dimensions:
            selected = self.config["default_backend"]
        if not selected and self._dimensions:
            selected = next(iter(self._dimensions))
        if not selected:
            raise ValueError("no Dimension is configured")
        try:
            return self._dimensions[selected]
        except KeyError as error:
            raise ValueError(f"Dimension {selected} is missing") from error

    def get(self, dimension_id: str) -> DimensionConfig | None:
        return self._dimensions.get(dimension_id)

    def __iter__(self):
        return iter(self._dimensions.items())


def resolve_dimension(config: dict | None, dimension_id: str | None = None) -> DimensionConfig:
    return DimensionRegistry(config).select(dimension_id)


def dimension_configs(config: dict | None) -> dict[str, DimensionConfig]:
    config = config or {}
    records = config.get("dimensions", {})
    if not isinstance(records, dict):
        raise TypeError("Dimensions configuration must be an object")
    dimensions = {dimension_id: DimensionConfig.from_private(dimension_id, body) for dimension_id, body in records.items()}
    for provider, display_name in _LEGACY_DIMENSION_NAMES.items():
        if provider in config and provider not in dimensions:
            dimensions[provider] = DimensionConfig.from_private(provider, {**config[provider], "display_name": display_name, "provider": provider})
    return dimensions


def config_dir() -> Path:
    return Path(os.environ.get("JOSH_ROOM_CONFIG_DIR", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "josh-room"))


def private_config() -> dict | None:
    runtime = os.environ.get("JOSH_ROOM_RUNTIME_CONFIG")
    if runtime:
        path = Path(runtime)
        if path.is_file():
            return json.loads(path.read_text())
    path = config_dir() / "config.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def save_private_config(body: dict) -> Path:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "config.json"
    fd, temp_name = tempfile.mkstemp(prefix=".config.", dir=directory)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, path)
        return path
    finally:
        temp.unlink(missing_ok=True)


def auth_status() -> dict:
    config = private_config()
    dimensions = dimension_configs(config)
    if not dimensions:
        return {"state": "unconfigured", "mode": "unconfigured", "credentials_verified": False}
    legacy = config.get("r2") if config else None
    if legacy and not config.get("dimensions"):
        if not available():
            return {"state": "keyring-unavailable", "mode": "s3-api-credentials", "credentials_verified": False, "bucket_configured": bool(legacy.get("bucket"))}
        return {"state": "configured-unverified", "mode": "s3-api-credentials", "credential_source": "os-secret-service", "credentials_verified": False, "bucket_configured": bool(legacy.get("bucket")), "temporary_credentials_preferred": bool(legacy.get("temporary_credentials", True))}
    legacy = config.get("r2") if config else None
    if legacy and not config.get("dimensions"):
        if not available():
            return {"state": "keyring-unavailable", "mode": "s3-api-credentials", "credentials_verified": False, "bucket_configured": bool(legacy.get("bucket"))}
        return {"state": "configured-unverified", "mode": "s3-api-credentials", "credential_source": "os-secret-service", "credentials_verified": False, "bucket_configured": bool(legacy.get("bucket")), "temporary_credentials_preferred": bool(legacy.get("temporary_credentials", True))}
    if not available():
        return {"state": "keyring-unavailable", "mode": "s3-api-credentials", "credentials_verified": False, "dimensions": sorted(dimensions)}
    return {"state": "configured-unverified", "mode": "s3-api-credentials", "credential_source": "os-secret-service", "credentials_verified": False, "dimensions": sorted(dimensions)}
