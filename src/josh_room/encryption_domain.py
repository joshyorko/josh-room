"""Contracts for encryption material scoped to one physical storage bucket."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

KEYSET_CONTROL_KEY = "control/encryption-keyset.v1.json"
MIGRATION_JOURNAL_KEY = "control/migration-journal.v1.json"
CONTROL_KEYS = frozenset({KEYSET_CONTROL_KEY, MIGRATION_JOURNAL_KEY})
KEYSET_FORMAT_VERSION = 1
MAX_KEYSET_SIZE = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEYSET_FIELDS = frozenset(
    {
        "format_version",
        "encryption_domain_id",
        "provider",
        "endpoint",
        "bucket",
        "key_generation",
        "operational_recipient",
        "recovery_recipients",
    }
)


def validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in endpoint
    ):
        raise ValueError("endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
    except ValueError as error:
        raise ValueError("endpoint is invalid") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must use http/https with a hostname")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("endpoint must not contain userinfo")
    if parsed.scheme == "http":
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = parsed.hostname.lower() == "localhost"
        if not is_loopback:
            raise ValueError("non-loopback HTTP endpoint is not allowed")
    return endpoint


def physical_bucket_identity(provider: str, endpoint: str, bucket: str) -> str:
    """Return the stable non-secret identity used to compare physical aliases."""
    if provider not in {"r2", "minio"}:
        raise ValueError("unsupported encryption provider")
    validate_endpoint(endpoint)
    if not isinstance(bucket, str) or not bucket or not _IDENTIFIER.fullmatch(bucket):
        raise ValueError("bucket binding is invalid")
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname.lower()
    port = parsed.port
    if (parsed.scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    authority = hostname if port is None else f"{hostname}:{port}"
    normalized = urlunsplit((parsed.scheme.lower(), authority, parsed.path.rstrip("/"), "", ""))
    return f"{provider}:{normalized}:{bucket}"


def is_control_key(key: str) -> bool:
    return key in CONTROL_KEYS


def _validate_recipient(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(
        character.isspace() or ord(character) < 0x20 for character in value
    ):
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class EncryptionKeyset:
    encryption_domain_id: str
    provider: str
    endpoint: str
    bucket: str
    key_generation: int
    operational_recipient: str
    recovery_recipients: tuple[str, ...]
    format_version: int = KEYSET_FORMAT_VERSION

    def __post_init__(self):
        if self.format_version != KEYSET_FORMAT_VERSION:
            raise ValueError("unsupported keyset format")
        try:
            parsed_id = uuid.UUID(self.encryption_domain_id)
        except (ValueError, AttributeError, TypeError) as error:
            raise ValueError("encryption domain id is invalid") from error
        if parsed_id.version != 4:
            raise ValueError("encryption domain id must be random")
        if self.provider not in {"r2", "minio"}:
            raise ValueError("keyset provider is invalid")
        validate_endpoint(self.endpoint)
        if not isinstance(self.bucket, str) or not self.bucket or len(self.bucket) > 63:
            raise ValueError("keyset bucket is invalid")
        if type(self.key_generation) is not int or self.key_generation < 1:
            raise ValueError("key generation must be positive")
        operational = _validate_recipient(self.operational_recipient, "operational recipient")
        recovery = tuple(_validate_recipient(value, "recovery recipient") for value in self.recovery_recipients)
        if not recovery:
            raise ValueError("at least one recovery recipient is required")
        if len({operational, *recovery}) != len(recovery) + 1:
            raise ValueError("duplicate recipient")
        object.__setattr__(self, "operational_recipient", operational)
        object.__setattr__(self, "recovery_recipients", recovery)

    @property
    def binding(self) -> str:
        return physical_bucket_identity(self.provider, self.endpoint, self.bucket)

    @classmethod
    def create(
        cls,
        provider: str,
        endpoint: str,
        bucket: str,
        operational_recipient: str,
        recovery_recipients: list[str] | tuple[str, ...] | None = None,
        *,
        recovery_recipient: str | None = None,
        encryption_domain_id: str | None = None,
        key_generation: int = 1,
    ) -> EncryptionKeyset:
        if recovery_recipients is None:
            recovery_recipients = [recovery_recipient] if recovery_recipient is not None else []
        elif recovery_recipient is not None:
            raise ValueError("recovery recipients are ambiguous")
        return cls(
            encryption_domain_id or str(uuid.uuid4()),
            provider,
            endpoint,
            bucket,
            key_generation,
            operational_recipient,
            tuple(recovery_recipients),
        )

    @classmethod
    def from_dict(
        cls,
        body: dict,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        bucket: str | None = None,
    ) -> EncryptionKeyset:
        if not isinstance(body, dict):
            raise TypeError("keyset must be an object")
        unknown = set(body) - _KEYSET_FIELDS
        if unknown:
            raise ValueError(f"unknown keyset field: {min(unknown)}")
        missing = _KEYSET_FIELDS - set(body)
        if missing:
            raise ValueError(f"missing keyset field: {min(missing)}")
        values = dict(body)
        if provider is not None and values["provider"] != provider:
            raise ValueError("provider binding mismatch")
        if endpoint is not None and physical_bucket_identity(values["provider"], values["endpoint"], values["bucket"]) != physical_bucket_identity(values["provider"], endpoint, values["bucket"]):
            raise ValueError("endpoint binding mismatch")
        if bucket is not None and values["bucket"] != bucket:
            raise ValueError("bucket binding mismatch")
        if not isinstance(values["recovery_recipients"], list):
            raise TypeError("recovery recipients are invalid")
        return cls(**values)

    @classmethod
    def from_json(cls, body: bytes | str, **bindings) -> EncryptionKeyset:
        raw = body.encode() if isinstance(body, str) else body
        if not isinstance(raw, bytes) or len(raw) > MAX_KEYSET_SIZE:
            raise ValueError("keyset exceeds maximum size")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("keyset JSON is invalid") from error
        return cls.from_dict(value, **bindings)

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "encryption_domain_id": self.encryption_domain_id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "key_generation": self.key_generation,
            "operational_recipient": self.operational_recipient,
            "recovery_recipients": list(self.recovery_recipients),
        }

    def to_json(self) -> bytes:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        if len(body) > MAX_KEYSET_SIZE:
            raise ValueError("keyset exceeds maximum size")
        return body


@dataclass(frozen=True)
class EncryptionMaterial:
    keyset: EncryptionKeyset
    identity: Path

    @property
    def recipient(self) -> str:
        return self.keyset.operational_recipient

    @property
    def encryption_domain_id(self) -> str:
        return self.keyset.encryption_domain_id

    @property
    def key_generation(self) -> int:
        return self.keyset.key_generation
