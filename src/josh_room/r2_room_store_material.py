"""Cloudflare-authorized, age-encrypted Room Store material broker client."""

from __future__ import annotations

import base64
import json
import re
import secrets
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import crypto, keyring
from .encryption_domain import (
    RoomStoreKeyset,
    validate_recipient,
    validate_room_store_secret,
)
from .private_paths import (
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)

_FORMAT = "josh-room-r2-room-store-material"
_VERSION = 1
_MAX_CIPHERTEXT = 16_384
_MAX_PLAINTEXT = 4096
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MATERIAL_FIELDS = {
    "format",
    "version",
    "domainId",
    "keysetGeneration",
    "ciphertext",
    "repositoryId",
}
_INNER_FIELDS = {
    "format",
    "version",
    "physical_binding",
    "encryption_domain_id",
    "key_generation",
    "secret",
}


class R2RoomStoreError(RuntimeError):
    """Public-safe Room Store broker failure."""


@dataclass(frozen=True, slots=True)
class R2RoomStoreAuthority:
    session_id: str
    capability: str = field(repr=False)
    physical_binding: str
    endpoint_transport: str | Callable = "https://room-store.invalid"
    identity_path: Path = field(default=Path(), repr=False)
    recipients: tuple[str, ...] = ()
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.session_id
        ):
            raise ValueError("R2 Room Store session is invalid")
        if not isinstance(self.capability, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", self.capability
        ):
            raise ValueError("R2 Room Store capability is invalid")
        if not isinstance(self.physical_binding, str) or not _HASH.fullmatch(
            self.physical_binding
        ):
            raise ValueError("R2 Room Store physical binding is invalid")
        if isinstance(self.endpoint_transport, str):
            parsed = urlsplit(self.endpoint_transport)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError("R2 Room Store endpoint must use HTTPS")
        if not isinstance(self.identity_path, Path):
            object.__setattr__(self, "identity_path", Path(self.identity_path))
        verify_private_path(self.identity_path, directory=False)
        recipients = tuple(validate_recipient(item) for item in self.recipients)
        if len(set(recipients)) != len(recipients) or len(recipients) < 2:
            raise ValueError("R2 Room Store requires distinct age recipients")
        object.__setattr__(self, "recipients", recipients)
        if type(self.timeout) not in {int, float} or not 0 < self.timeout <= 60:
            raise ValueError("R2 Room Store timeout is invalid")

    def __repr__(self) -> str:
        return (
            "R2RoomStoreAuthority(session_id=<redacted>, capability=<redacted>, "
            f"physical_binding={self.physical_binding!r}, endpoint_transport=<redacted>, "
            "identity_path=<redacted>, recipients=<redacted>)"
        )

    def ensure_material(self) -> RoomStoreKeyset:
        """Create or recover material, then cache only the broker's durable winner."""
        try:
            record = self._read_material()
        except R2RoomStoreError as error:
            if str(error) != "room_store_material_missing":
                raise
            record = None

        if record is None:
            candidate = self._new_candidate()
            try:
                status, body = self._request("POST", "material", candidate)
                record = self._record(body, expected_status="created", status=status)
            except R2RoomStoreError as error:
                # A competing first writer or an ambiguous write is resolved only by
                # reloading and decrypting the durable record.
                try:
                    record = self._read_material()
                except R2RoomStoreError:
                    raise error from None

        material = self._decrypt_record(record)
        result = RoomStoreKeyset(
            secret=material["secret"],
            physical_binding=self.physical_binding,
            generation=record["keysetGeneration"],
            repository_id=record["repositoryId"],
        )
        self._cache(material["encryption_domain_id"], result.generation, result.secret)
        return result

    def bind_repository(
        self, repository_id: str, expected_generation: int | None = None
    ) -> RoomStoreKeyset:
        """Bind an initialized Restic repository using broker CAS metadata."""
        if not isinstance(repository_id, str) or not _HASH.fullmatch(repository_id):
            raise ValueError("R2 Room Store repository id is invalid")
        current = self.ensure_material()
        expected = (
            current.generation if expected_generation is None else expected_generation
        )
        if type(expected) is not int or expected < 1:
            raise ValueError("R2 Room Store generation is invalid")
        try:
            status, body = self._request(
                "POST",
                "repository",
                {"repositoryId": repository_id, "expectedGeneration": expected},
            )
            if status not in {200, 201} or not isinstance(body, dict):
                raise R2RoomStoreError("room_store_repository_response_invalid")
            if (
                body.get("repositoryId") != repository_id
                or type(body.get("keysetGeneration")) is not int
            ):
                raise R2RoomStoreError("room_store_repository_response_invalid")
            generation = body["keysetGeneration"]
            if generation not in {expected, expected + 1}:
                raise R2RoomStoreError("room_store_generation_conflict")
        except R2RoomStoreError as error:
            # A lost response can only be accepted after a fresh material readback.
            winner = self._read_material()
            if winner["repositoryId"] != repository_id:
                raise error from None
            generation = winner["keysetGeneration"]
        bound_record = self._read_material()
        material = self._decrypt_record(bound_record)
        if (
            bound_record["repositoryId"] != repository_id
            or bound_record["keysetGeneration"] != generation
        ):
            raise R2RoomStoreError("room_store_repository_readback_mismatch")
        result = RoomStoreKeyset(
            material["secret"], self.physical_binding, generation, repository_id
        )
        self._cache(material["encryption_domain_id"], generation, result.secret)
        return result

    def _url(self, operation: str) -> str:
        return f"{self.endpoint_transport}/session/{self.session_id}/room-store/{operation}"

    def _request(
        self, method: str, operation: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        body = (
            None
            if payload is None
            else json.dumps(payload, separators=(",", ":")).encode()
        )
        headers = {
            "Authorization": f"Bearer {self.capability}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            if callable(self.endpoint_transport):
                response = self.endpoint_transport(
                    method, self.session_id, operation, headers, body
                )
                status, response_body = response
            else:
                request = Request(
                    self._url(operation), data=body, headers=headers, method=method
                )
                try:
                    with urlopen(request, timeout=self.timeout) as response:
                        status, response_body = response.status, response.read(32_768)
                except HTTPError as response:
                    status, response_body = response.code, response.read(32_768)
        except R2RoomStoreError:
            raise
        except Exception:  # noqa: BLE001 - transport diagnostics may contain credentials.
            raise R2RoomStoreError("room_store_transport_failed") from None
        if type(status) is not int or not 100 <= status <= 599:
            raise R2RoomStoreError("room_store_response_invalid")
        try:
            result = (
                response_body
                if isinstance(response_body, dict)
                else json.loads(response_body)
            )
        except (TypeError, ValueError):
            raise R2RoomStoreError("room_store_response_invalid") from None
        if not isinstance(result, dict):
            raise R2RoomStoreError("room_store_response_invalid")
        if status >= 400:
            code = result.get("error")
            if code == "room_store_material_missing" and operation == "material":
                raise R2RoomStoreError(code)
            if code not in {
                "room_store_capability_required",
                "room_store_capability_invalid",
                "room_store_scope_mismatch",
                "room_store_capability_expired",
                "room_store_invalid_material",
                "room_store_invalid_repository",
                "room_store_material_conflict",
                "room_store_repository_conflict",
                "room_store_generation_conflict",
                "room_store_material_write_unverified",
                "room_store_repository_write_unverified",
                "room_store_material_missing",
                "room_store_durable_authority_unavailable",
                "room_store_not_found",
                "room_store_method_not_allowed",
            }:
                raise R2RoomStoreError("room_store_request_rejected")
            raise R2RoomStoreError(code)
        return status, result

    def _read_material(self) -> dict:
        status, body = self._request("GET", "material")
        if status != 200 or body.get("status") != "ready":
            raise R2RoomStoreError("room_store_material_response_invalid")
        return self._record(body, expected_status="ready", status=status)

    def _record(self, body: dict, *, expected_status: str, status: int) -> dict:
        if status not in (
            {200, 201} if expected_status in {"ready", "created"} else {200}
        ):
            raise R2RoomStoreError("room_store_material_response_invalid")
        record = body.get("material")
        if (
            body.get("status") != expected_status
            or not isinstance(record, dict)
            or set(record) != _MATERIAL_FIELDS
        ):
            raise R2RoomStoreError("room_store_material_response_invalid")
        if record["format"] != _FORMAT or record["version"] != _VERSION:
            raise R2RoomStoreError("room_store_material_unsupported")
        if record["domainId"] != self.physical_binding:
            raise R2RoomStoreError("room_store_physical_binding_mismatch")
        if (
            type(record["keysetGeneration"]) is not int
            or not 1 <= record["keysetGeneration"] <= 2_147_483_647
        ):
            raise R2RoomStoreError("room_store_material_response_invalid")
        if (
            not isinstance(record["ciphertext"], str)
            or not 1 <= len(record["ciphertext"]) <= _MAX_CIPHERTEXT
            or not re.fullmatch(r"[A-Za-z0-9_-]+", record["ciphertext"])
        ):
            raise R2RoomStoreError("room_store_material_response_invalid")
        if record["repositoryId"] is not None and (
            not isinstance(record["repositoryId"], str)
            or not _HASH.fullmatch(record["repositoryId"])
        ):
            raise R2RoomStoreError("room_store_material_response_invalid")
        return record

    def _new_candidate(self) -> dict:
        inner = {
            "format": _FORMAT,
            "version": _VERSION,
            "physical_binding": self.physical_binding,
            "encryption_domain_id": str(uuid.uuid4()),
            "key_generation": 1,
            "secret": base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode()
            .rstrip("="),
        }
        plaintext = json.dumps(inner, sort_keys=True, separators=(",", ":")).encode()
        ciphertext = self._encrypt(plaintext)
        return {
            "format": _FORMAT,
            "version": _VERSION,
            "domainId": self.physical_binding,
            "keysetGeneration": 1,
            "ciphertext": ciphertext,
        }

    def _encrypt(self, plaintext: bytes) -> str:
        with tempfile.TemporaryDirectory(prefix="josh-room-r2-material-") as temporary:
            directory = Path(temporary)
            protect_private_directory(directory)
            output = directory / "material.age"
            try:
                crypto.encrypt(plaintext, list(self.recipients), output)
                verify_private_path(output, directory=False)
                ciphertext = (
                    base64.urlsafe_b64encode(output.read_bytes()).decode().rstrip("=")
                )
            except (crypto.CryptoError, OSError, ValueError, TypeError, RuntimeError):
                raise R2RoomStoreError("room_store_encryption_failed") from None
        if not 1 <= len(ciphertext) <= _MAX_CIPHERTEXT:
            raise R2RoomStoreError("room_store_ciphertext_limit")
        return ciphertext

    def _decrypt_record(self, record: dict) -> dict:
        try:
            raw = base64.urlsafe_b64decode(
                record["ciphertext"] + "=" * ((4 - len(record["ciphertext"]) % 4) % 4)
            )
            if (
                base64.urlsafe_b64encode(raw).decode().rstrip("=")
                != record["ciphertext"]
            ):
                raise ValueError("noncanonical ciphertext")
            with tempfile.TemporaryDirectory(
                prefix="josh-room-r2-material-"
            ) as temporary:
                directory = Path(temporary)
                protect_private_directory(directory)
                encrypted = directory / "material.age"
                encrypted.write_bytes(raw)
                protect_private_file(encrypted)
                verify_private_path(self.identity_path, directory=False)
                plaintext = crypto.decrypt(
                    encrypted, [self.identity_path], _MAX_PLAINTEXT
                )
            if len(plaintext) > _MAX_PLAINTEXT:
                raise ValueError("material too large")
            inner = json.loads(plaintext)
        except (
            crypto.CryptoError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            RuntimeError,
        ):
            raise R2RoomStoreError("room_store_material_decryption_failed") from None
        if not isinstance(inner, dict) or set(inner) != _INNER_FIELDS:
            raise R2RoomStoreError("room_store_material_schema_invalid")
        if (
            inner["format"] != _FORMAT
            or type(inner["version"]) is not int
            or inner["version"] != _VERSION
        ):
            raise R2RoomStoreError("room_store_material_unsupported")
        if inner["physical_binding"] != self.physical_binding:
            raise R2RoomStoreError("room_store_physical_binding_mismatch")
        try:
            parsed = uuid.UUID(inner["encryption_domain_id"])
            if parsed.version != 4 or str(parsed) != inner["encryption_domain_id"]:
                raise ValueError("domain")
            if (
                type(inner["key_generation"]) is not int
                or not 1 <= inner["key_generation"] <= 2_147_483_647
            ):
                raise ValueError("generation")
            validate_room_store_secret(inner["secret"])
        except (TypeError, ValueError):
            raise R2RoomStoreError("room_store_material_identity_invalid") from None
        return inner

    def _cache(self, encryption_domain_id: str, generation: int, secret: str) -> None:
        try:
            keyring.store_room_store_secret(encryption_domain_id, generation, secret)
        except (RuntimeError, OSError, ValueError):
            raise R2RoomStoreError("room_store_native_keyring_unavailable") from None
