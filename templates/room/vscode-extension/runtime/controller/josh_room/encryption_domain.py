"""Contracts for encryption material scoped to one physical storage bucket."""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field, replace
from hmac import compare_digest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

KEYSET_CONTROL_KEY = "control/encryption-keyset.v1.json"
MIGRATION_JOURNAL_KEY = "control/migration-journal.v1.json"
CONTROL_KEYS = frozenset({KEYSET_CONTROL_KEY, MIGRATION_JOURNAL_KEY})
KEYSET_FORMAT_VERSION = 1
ROOM_STORE_KEYSET_FORMAT_VERSION = 2
ROOM_STORE_METADATA_FORMAT_VERSION = 1
ROOM_STORE_REPOSITORY_FORMAT_VERSION = 2
ROOM_STORE_PREFIX = "room-store/v1"
MAX_KEYSET_SIZE = 64 * 1024
CONTROL_OBJECT_MAX_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_KEYSET_FIELDS = frozenset(
    {
        "format_version",
        "encryption_domain_id",
        "provider",
        "endpoint",
        "bucket",
        "key_generation",
        "operational_identity",
        "operational_recipient",
        "recovery_recipients",
    }
)
_ROOM_STORE_FIELDS = frozenset(
    {
        "format_version",
        "secret",
        "repository_prefix",
        "repository_format",
        "physical_binding",
        "generation",
        "repository_id",
    }
)


def validate_room_store_secret(value: object) -> str:
    """Validate canonical base64url encoding of a 32-byte secret."""
    if not isinstance(value, str) or len(value) != 43:
        raise ValueError("Room Store secret encoding is invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError) as error:
        raise ValueError("Room Store secret encoding is invalid") from error
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        raise ValueError("Room Store secret encoding is invalid")
    return value


@dataclass(frozen=True)
class RoomStoreKeyset:
    secret: str = field(repr=False, compare=False)
    physical_binding: str
    generation: int = 1
    repository_id: str | None = None
    repository_prefix: str = ROOM_STORE_PREFIX
    repository_format: int = ROOM_STORE_REPOSITORY_FORMAT_VERSION
    format_version: int = ROOM_STORE_METADATA_FORMAT_VERSION

    def __post_init__(self):
        validate_room_store_secret(self.secret)
        if type(self.format_version) is not int or self.format_version != ROOM_STORE_METADATA_FORMAT_VERSION:
            raise ValueError("unsupported Room Store metadata format")
        if self.repository_prefix != ROOM_STORE_PREFIX:
            raise ValueError("unsupported Room Store repository prefix")
        if type(self.repository_format) is not int or self.repository_format != ROOM_STORE_REPOSITORY_FORMAT_VERSION:
            raise ValueError("unsupported Room Store repository format")
        if not isinstance(self.physical_binding, str) or not self.physical_binding:
            raise ValueError("Room Store physical binding is invalid")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("Room Store generation must be positive")
        if self.repository_id is not None and (
            not isinstance(self.repository_id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.repository_id)
        ):
            raise ValueError("Room Store repository id is invalid")

    @classmethod
    def from_dict(cls, body: object) -> RoomStoreKeyset:
        if not isinstance(body, dict):
            raise TypeError("Room Store metadata must be an object")
        unknown = set(body) - _ROOM_STORE_FIELDS
        if unknown:
            raise ValueError(f"unknown Room Store field: {min(unknown)}")
        missing = _ROOM_STORE_FIELDS - set(body)
        if missing:
            raise ValueError(f"missing Room Store field: {min(missing)}")
        return cls(**body)

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "secret": self.secret,
            "repository_prefix": self.repository_prefix,
            "repository_format": self.repository_format,
            "physical_binding": self.physical_binding,
            "generation": self.generation,
            "repository_id": self.repository_id,
        }


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


def validate_minio_transport(endpoint: str, *, verify_tls: object = True, ca_bundle: str | None = None) -> str:
    """Validate the effective transport before constructing an object-store client."""
    validate_endpoint(endpoint)
    if type(verify_tls) is not bool:
        raise TypeError("MinIO TLS verification must be a boolean")
    parsed = urlsplit(endpoint)
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise ValueError("MinIO remote endpoints must use HTTPS")
    if not verify_tls:
        raise ValueError("MinIO TLS verification cannot be disabled")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle):
        raise ValueError("MinIO CA bundle is invalid")
    return endpoint


def validate_encryption_domain_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("encryption domain id is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ValueError("encryption domain id is invalid") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("encryption domain id must be canonical UUID4")
    return value


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


def validate_recipient(value: object, label: str = "recipient") -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or any(
        character.isspace() or ord(character) < 0x20 for character in value
    ):
        raise ValueError(f"{label} is invalid")
    if not value.startswith("age1") or len(value) != 62 or any(character not in _BECH32_ALPHABET for character in value[4:]):
        raise ValueError(f"{label} has invalid age recipient syntax")
    data = [_BECH32_ALPHABET.index(character) for character in value[4:]]
    if _bech32_polymod(_bech32_hrp_expand("age") + data) != 1:
        raise ValueError(f"{label} has invalid age recipient syntax")
    payload = _bech32_convertbits(data[:-6], 5, 8, False)
    if payload is None or len(payload) != 32:
        raise ValueError(f"{label} has invalid age recipient syntax")
    return value


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(character) >> 5 for character in hrp] + [0] + [ord(character) & 31 for character in hrp]


def _bech32_polymod(values: list[int]) -> int:
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(generators):
            if top >> index & 1:
                checksum ^= generator
    return checksum


def _bech32_convertbits(data: list[int], from_bits: int, to_bits: int, pad: bool) -> list[int] | None:
    accumulator = 0
    bits = 0
    result = []
    maximum = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            return None
        accumulator = accumulator << from_bits | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append(accumulator >> bits & maximum)
    if pad:
        if bits:
            result.append(accumulator << (to_bits - bits) & maximum)
    elif bits >= from_bits or accumulator << (to_bits - bits) & maximum:
        return None
    return result


def _validate_operational_identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 16 * 1024:
        raise ValueError("operational identity is invalid")
    lines = value.splitlines()
    if len(lines) != 1 or not re.fullmatch(r"AGE-SECRET-KEY-[A-Za-z0-9-]+", lines[0]):
        raise ValueError("operational identity is invalid")
    identity = lines[0]
    suffix = identity[len("AGE-SECRET-KEY-"):]
    if len(identity) != 74 or not suffix.startswith("1") or any(
        character not in _BECH32_ALPHABET for character in suffix[1:].lower()
    ):
        raise ValueError("operational identity is invalid")
    data = [_BECH32_ALPHABET.index(character) for character in suffix[1:].lower()]
    if _bech32_polymod(_bech32_hrp_expand("age-secret-key-") + data) != 1:
        raise ValueError("operational identity is invalid")
    payload = _bech32_convertbits(data[:-6], 5, 8, False)
    if payload is None or len(payload) != 32:
        raise ValueError("operational identity is invalid")
    return identity


def validate_operational_identity(value: object) -> str:
    """Validate one age identity line without returning or logging its secret."""
    return _validate_operational_identity(value)


@dataclass(frozen=True)
class EncryptionKeyset:
    encryption_domain_id: str
    provider: str
    endpoint: str
    bucket: str
    key_generation: int
    operational_identity: str = field(repr=False)
    operational_recipient: str
    recovery_recipients: tuple[str, ...]
    format_version: int = KEYSET_FORMAT_VERSION
    room_store: RoomStoreKeyset | None = field(default=None, repr=False)

    def __post_init__(self):
        if type(self.format_version) is not int or self.format_version not in {KEYSET_FORMAT_VERSION, ROOM_STORE_KEYSET_FORMAT_VERSION}:
            raise ValueError("unsupported keyset format")
        if self.format_version == KEYSET_FORMAT_VERSION and self.room_store is not None:
            raise ValueError("version 1 keyset cannot contain Room Store metadata")
        if self.format_version == ROOM_STORE_KEYSET_FORMAT_VERSION:
            if not isinstance(self.room_store, RoomStoreKeyset):
                raise ValueError("version 2 keyset requires Room Store metadata")
            if self.room_store.physical_binding != self.binding:
                raise ValueError("Room Store physical binding mismatch")
        validate_encryption_domain_id(self.encryption_domain_id)
        if self.provider not in {"r2", "minio"}:
            raise ValueError("keyset provider is invalid")
        validate_endpoint(self.endpoint)
        if not isinstance(self.bucket, str) or not self.bucket or len(self.bucket) > 63:
            raise ValueError("keyset bucket is invalid")
        if type(self.key_generation) is not int or self.key_generation < 1:
            raise ValueError("key generation must be positive")
        operational_identity = validate_operational_identity(self.operational_identity)
        operational = validate_recipient(self.operational_recipient, "operational recipient")
        recovery = tuple(validate_recipient(value, "recovery recipient") for value in self.recovery_recipients)
        if not recovery:
            raise ValueError("at least one recovery recipient is required")
        if len({operational, *recovery}) != len(recovery) + 1:
            raise ValueError("duplicate recipient")
        object.__setattr__(self, "operational_recipient", operational)
        object.__setattr__(self, "operational_identity", operational_identity)
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
        operational_identity: str,
        operational_recipient: str,
        recovery_recipients: list[str] | tuple[str, ...] | None = None,
        *,
        recovery_recipient: str | None = None,
        encryption_domain_id: str | None = None,
        key_generation: int = 1,
        existing: EncryptionKeyset | None = None,
        occupied: tuple[EncryptionKeyset, ...] = (),
    ) -> EncryptionKeyset:
        if recovery_recipients is None:
            recovery_recipients = [recovery_recipient] if recovery_recipient is not None else []
        elif recovery_recipient is not None:
            raise ValueError("recovery recipients are ambiguous")
        candidate = cls(
            encryption_domain_id or str(uuid.uuid4()),
            provider,
            endpoint,
            bucket,
            key_generation,
            operational_identity,
            operational_recipient,
            tuple(recovery_recipients),
        )
        return reconcile_keyset(existing, candidate, occupied=occupied)

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
        version = body.get("format_version")
        if type(version) is not int or version not in {KEYSET_FORMAT_VERSION, ROOM_STORE_KEYSET_FORMAT_VERSION}:
            raise ValueError("unsupported keyset format")
        fields = _KEYSET_FIELDS if version == KEYSET_FORMAT_VERSION else _KEYSET_FIELDS | {"room_store"}
        unknown = set(body) - fields
        if unknown:
            raise ValueError(f"unknown keyset field: {min(unknown)}")
        missing = fields - set(body)
        if missing:
            raise ValueError(f"missing keyset field: {min(missing)}")
        values = dict(body)
        if version == ROOM_STORE_KEYSET_FORMAT_VERSION:
            values["room_store"] = RoomStoreKeyset.from_dict(values["room_store"])
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
    def reconcile(
        cls,
        existing: EncryptionKeyset | None,
        candidate: EncryptionKeyset,
        *,
        occupied: tuple[EncryptionKeyset, ...] = (),
    ) -> EncryptionKeyset:
        return reconcile_keyset(existing, candidate, occupied=occupied)

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
        body = {
            "format_version": self.format_version,
            "encryption_domain_id": self.encryption_domain_id,
            "provider": self.provider,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "key_generation": self.key_generation,
            "operational_identity": self.operational_identity,
            "operational_recipient": self.operational_recipient,
            "recovery_recipients": list(self.recovery_recipients),
        }
        if self.format_version == ROOM_STORE_KEYSET_FORMAT_VERSION:
            body["room_store"] = self.room_store.to_dict()
        return body

    def upgrade_for_room_store(self) -> EncryptionKeyset:
        """Explicitly add a random Room Store secret without rotating age keys."""
        if self.format_version == ROOM_STORE_KEYSET_FORMAT_VERSION:
            return self
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        metadata = RoomStoreKeyset(secret=secret, physical_binding=self.binding)
        return replace(self, format_version=ROOM_STORE_KEYSET_FORMAT_VERSION, room_store=metadata)

    def bind_repository(self, repository_id: str, *, expected_generation: int) -> EncryptionKeyset:
        """Bind the initialized repository once using Room Store metadata CAS generation."""
        if self.format_version != ROOM_STORE_KEYSET_FORMAT_VERSION or self.room_store is None:
            raise ValueError("Room Store keyset upgrade is required")
        if type(expected_generation) is not int or expected_generation != self.room_store.generation:
            raise ValueError("Room Store generation mismatch")
        if not isinstance(repository_id, str) or not re.fullmatch(r"[0-9a-f]{64}", repository_id):
            raise ValueError("Room Store repository id is invalid")
        if self.room_store.repository_id is not None:
            if self.room_store.repository_id == repository_id:
                return self
            raise ValueError("Room Store repository id is immutable")
        metadata = replace(
            self.room_store,
            repository_id=repository_id,
            generation=self.room_store.generation + 1,
        )
        return replace(self, room_store=metadata)

    def to_json(self) -> bytes:
        body = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        if len(body) > MAX_KEYSET_SIZE:
            raise ValueError("keyset exceeds maximum size")
        return body


@dataclass(frozen=True)
class EncryptionMaterial:
    keyset: EncryptionKeyset
    identity: Path

    def __post_init__(self):
        from .crypto import derive_recipient

        identity = Path(self.identity)
        actual_recipient = derive_recipient(identity)
        if actual_recipient != self.keyset.operational_recipient:
            raise ValueError("operational recipient does not match identity")
        object.__setattr__(self, "identity", identity)

    @property
    def recipient(self) -> str:
        return self.keyset.operational_recipient

    @property
    def encryption_domain_id(self) -> str:
        return self.keyset.encryption_domain_id

    @property
    def key_generation(self) -> int:
        return self.keyset.key_generation


def reconcile_keyset(
    existing: EncryptionKeyset | None,
    candidate: EncryptionKeyset,
    *,
    occupied: tuple[EncryptionKeyset, ...] = (),
) -> EncryptionKeyset:
    """Return the remote winner for a physical bucket and reject identity reuse."""
    if not isinstance(candidate, EncryptionKeyset):
        raise TypeError("keyset candidate is invalid")
    if existing is not None:
        if not isinstance(existing, EncryptionKeyset):
            raise TypeError("existing keyset is invalid")
        if existing.binding != candidate.binding:
            raise ValueError("keyset physical bucket binding mismatch")
        if existing.format_version != candidate.format_version:
            raise ValueError("keyset version mismatch")
        if existing.format_version == ROOM_STORE_KEYSET_FORMAT_VERSION:
            if not compare_digest(existing.room_store.secret, candidate.room_store.secret):
                raise ValueError("Room Store secret mismatch")
            if existing.room_store.repository_id != candidate.room_store.repository_id:
                raise ValueError("Room Store repository id mismatch")
        return existing
    for item in occupied:
        if item.operational_identity == candidate.operational_identity and item.binding != candidate.binding:
            raise ValueError("operational identity is already bound to another bucket")
    return candidate
