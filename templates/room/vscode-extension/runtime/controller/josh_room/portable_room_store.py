"""Opt-in MinIO and S3 encrypted custody through the shared Durable Object authority."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import replace
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from botocore.exceptions import BotoCoreError

from . import keyring
from .encryption_domain import EncryptionKeyset, physical_bucket_identity
from .r2_room_store_material import R2RoomStoreAuthority, R2RoomStoreError

MARKER_FORMAT = "josh-room-portable-room-store-v1"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def domain_id(dimension):
    binding = physical_bucket_identity(dimension.provider, dimension.endpoint, dimension.bucket)
    return hashlib.sha256(binding.encode()).hexdigest()


def authority(dimension, material, *, transport=None):
    endpoint = dimension.option("room_store_authority_endpoint")
    profile = dimension.option("room_store_authority_profile")
    parsed = urlsplit(endpoint or "")
    if (
        dimension.provider not in {"minio", "s3"}
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
        or not isinstance(profile, str) or not profile
    ):
        raise ValueError("portable Room Store requires a configured HTTPS authority and keyring profile")
    binding = domain_id(dimension)
    if transport is None:
        token = keyring.secure_lookup(profile, "room-store-capability")
        if not isinstance(token, str) or len(token) > 4096 or any(c.isspace() for c in token):
            raise ValueError("portable Room Store capability is invalid")
        opener = build_opener(_NoRedirect())

        def transport(method, _session, operation, _headers, body):
            request = Request(
                f"{endpoint.rstrip('/')}/room-store/{binding}/{operation}",
                data=body, method=method,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                with opener.open(request, timeout=10) as response:
                    return response.status, response.read(32768)
            except HTTPError as response:
                return response.code, response.read(32768)

    return R2RoomStoreAuthority(
        "0" * 64, "0" * 43, binding, transport, material.identity,
        (material.keyset.operational_recipient, *material.keyset.recovery_recipients),
    )


def _marker_base(body, dimension):
    if (
        not isinstance(body, dict)
        or set(body) != {"format", "authority", "domainId", "keyset"}
        or body["format"] != MARKER_FORMAT
        or body["authority"] != dimension.option("room_store_authority_endpoint")
        or body["domainId"] != domain_id(dimension)
    ):
        raise ValueError("portable Room Store authority selection mismatch")
    base = EncryptionKeyset.from_dict(
        body["keyset"], provider=dimension.provider, endpoint=dimension.endpoint, bucket=dimension.bucket,
    )
    if base.format_version != 1:
        raise ValueError("portable Room Store enrollment record must not contain a password")
    return base


def marker_keyset(body, dimension, *, material_factory, transport=None):
    base = _marker_base(body, dimension)
    material = material_factory(base)
    details = authority(dimension, material, transport=transport).read_material_details()
    if details.encryption_domain_id != base.encryption_domain_id or details.key_generation != base.key_generation:
        raise ValueError("portable Room Store encryption domain mismatch")
    return replace(base, format_version=2, room_store=replace(details.keyset, physical_binding=base.binding))


def migrate(dimension, backend, material, *, transport=None):
    """Fence legacy writers with conditional publication after encrypted winner verification.

    The caller must quiesce clients and explicitly invoke this on an approved
    Dimension. This function never runs as a side effect of Save or Restore.
    """
    from .encryption_domain import KEYSET_CONTROL_KEY

    raw, etag = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    if raw is None or not etag:
        raise ValueError("portable Room Store migration requires a versioned keyset")
    source = EncryptionKeyset.from_json(raw, provider="minio", endpoint=dimension.endpoint, bucket=dimension.bucket)
    if (
        source != material.keyset or source.room_store is None
        or material.keyset.room_store is None
        or source.room_store.secret != material.keyset.room_store.secret
    ):
        raise ValueError("portable Room Store migration requires the current format-2 material")
    broker = authority(dimension, material, transport=transport)
    metadata = source.room_store
    inner = {
        "format": "josh-room-r2-room-store-material", "version": 1,
        "physical_binding": broker.physical_binding,
        "encryption_domain_id": source.encryption_domain_id,
        "key_generation": source.key_generation, "secret": metadata.secret,
    }
    generation = metadata.generation - (metadata.repository_id is not None)
    if generation < 1:
        raise ValueError("portable Room Store source generation is invalid")
    candidate = {
        "format": inner["format"], "version": 1, "domainId": broker.physical_binding,
        "keysetGeneration": generation,
        "ciphertext": broker._encrypt(json.dumps(inner, sort_keys=True, separators=(",", ":")).encode()),
    }
    try:
        broker._request("POST", "material", candidate)
    except R2RoomStoreError as error:
        if str(error) not in {"room_store_material_conflict", "room_store_transport_failed", "room_store_material_write_unverified"}:
            raise
    winner = broker.read_material_details()
    if winner.secret != metadata.secret or winner.encryption_domain_id != source.encryption_domain_id or winner.key_generation != source.key_generation:
        raise ValueError("portable Room Store migration winner mismatch")
    if metadata.repository_id is not None:
        winner = broker.bind_repository_details(metadata.repository_id, generation)
    if winner.generation != metadata.generation or winner.repository_id != metadata.repository_id:
        raise ValueError("portable Room Store migration binding mismatch")
    base = replace(source, format_version=1, room_store=None)
    marker = {
        "format": MARKER_FORMAT, "authority": dimension.option("room_store_authority_endpoint"),
        "domainId": domain_id(dimension), "keyset": base.to_dict(),
    }
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    try:
        backend.replace_control(KEYSET_CONTROL_KEY, encoded, etag)
    except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError):
        observed, _ = backend.read_control(KEYSET_CONTROL_KEY, 65536)
        if observed != encoded:
            raise RuntimeError("portable Room Store migration publication unverified") from None
    observed, _ = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    if observed != encoded:
        raise RuntimeError("portable Room Store migration publication unverified")
    return marker



def _initialize_s3(dimension, backend, source, etag):
    from .auth import _assert_keyset_matches_dimension, _material_for_keyset
    from .encryption_domain import KEYSET_CONTROL_KEY

    _assert_keyset_matches_dimension(dimension, source)
    broker = authority(dimension, _material_for_keyset(source))
    inner = {
        "format": "josh-room-r2-room-store-material", "version": 1,
        "physical_binding": broker.physical_binding,
        "encryption_domain_id": source.encryption_domain_id,
        "key_generation": source.key_generation,
        "secret": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    }
    candidate = {
        "format": inner["format"], "version": 1, "domainId": broker.physical_binding,
        "keysetGeneration": 1,
        "ciphertext": broker._encrypt(json.dumps(inner, sort_keys=True, separators=(",", ":")).encode()),
    }
    try:
        broker._request("POST", "material", candidate)
    except R2RoomStoreError as error:
        if str(error) not in {"room_store_material_conflict", "room_store_transport_failed", "room_store_material_write_unverified"}:
            raise
    winner = broker.read_material_details()
    if winner.encryption_domain_id != source.encryption_domain_id or winner.key_generation != source.key_generation:
        raise ValueError("portable Room Store enrollment winner mismatch")
    marker = {
        "format": MARKER_FORMAT, "authority": dimension.option("room_store_authority_endpoint"),
        "domainId": domain_id(dimension), "keyset": source.to_dict(),
    }
    encoded = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    try:
        backend.replace_control(KEYSET_CONTROL_KEY, encoded, etag)
    except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError):
        if backend.read_control(KEYSET_CONTROL_KEY, 65536)[0] != encoded:
            raise RuntimeError("portable Room Store enrollment publication unverified") from None
    if backend.read_control(KEYSET_CONTROL_KEY, 65536)[0] != encoded:
        raise RuntimeError("portable Room Store enrollment publication unverified")
    return replace(source, format_version=2, room_store=replace(winner.keyset, physical_binding=source.binding))

def selected_keyset(dimension, backend, *, repository_id=None, expected_generation=None, initialize=False):
    from .auth import _material_for_keyset, _validate_minio_backend_transport
    from .encryption_domain import KEYSET_CONTROL_KEY
    _validate_minio_backend_transport(dimension, backend)
    raw, etag = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    if raw is None:
        return None
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get("format") != MARKER_FORMAT:
        if (
            initialize and dimension.provider == "s3"
            and (dimension.option("room_store_authority_endpoint") or dimension.option("room_store_authority_profile"))
        ):
            source = EncryptionKeyset.from_json(raw, provider="s3", endpoint=dimension.endpoint, bucket=dimension.bucket)
            if source.format_version == 1:
                if not etag:
                    raise ValueError("portable Room Store enrollment requires a versioned keyset")
                return _initialize_s3(dimension, backend, source, etag)
        return None
    selected = marker_keyset(body, dimension, material_factory=_material_for_keyset)
    if repository_id is None:
        return selected
    material = _material_for_keyset(replace(selected, format_version=1, room_store=None))
    details = authority(dimension, material).bind_repository_details(repository_id, expected_generation)
    return replace(selected, room_store=replace(details.keyset, physical_binding=selected.binding))


def rollback(dimension, backend, *, transport=None):
    """Permanently seal the DO writer before restoring the latest legacy keyset."""
    from .auth import _material_for_keyset
    from .encryption_domain import KEYSET_CONTROL_KEY

    if dimension.provider != "minio":
        raise ValueError("portable Room Store rollback is limited to MinIO migration")
    raw, etag = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    body = json.loads(raw)
    base = _marker_base(body, dimension)
    material = _material_for_keyset(base)
    broker = authority(dimension, material, transport=transport)
    status, result = broker._request("POST", "seal", {})
    if status != 200 or result != {"status": "sealed"}:
        raise RuntimeError("portable Room Store rollback seal unverified")
    selected = marker_keyset(body, dimension, material_factory=lambda _base: material, transport=transport)
    encoded = selected.to_json()
    try:
        backend.replace_control(KEYSET_CONTROL_KEY, encoded, etag)
    except (BotoCoreError, OSError, RuntimeError, TypeError, ValueError):
        observed, _ = backend.read_control(KEYSET_CONTROL_KEY, 65536)
        if observed != encoded:
            raise RuntimeError("portable Room Store rollback publication unverified") from None
    observed, _ = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    if observed != encoded:
        raise RuntimeError("portable Room Store rollback publication unverified")
    return selected
