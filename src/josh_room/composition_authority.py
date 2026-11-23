"""Append-only composition namespace, separate from legacy catalogs/Restic.

The namespace is a locator, not authorization. Credentials and age material
still come from the explicitly selected physical Dimension.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from botocore.exceptions import ClientError

from .composition_safety import CompositionSafetyState
from .composition_transaction import canonical
from .crypto import decrypt, encrypt
from .encryption_domain import physical_bucket_identity
from .local_store import ObjectRef

LIMIT = 2 * 1024 * 1024


def namespace_prefix(namespace):
    if (
        not isinstance(namespace, str)
        or re.fullmatch("[a-f0-9]{32}", namespace) is None
    ):
        raise ValueError("invalid composition namespace")
    return f"composition/v1/{namespace}/"


def dimension_binding(dimension, domain):
    # Path-bearing proxy aliases need separate provider key-mapping qualification.
    if urlsplit(dimension.endpoint).path not in ("", "/"):
        raise ValueError("composition requires a root provider endpoint")
    return {
        "provider": dimension.provider,
        "connection_id": dimension.connection_id or dimension.dimension_id,
        "dimension_id": dimension.dimension_id,
        "encryption_domain_id": domain,
        "physical_bucket": physical_bucket_identity(
            dimension.provider, dimension.endpoint, dimension.bucket
        ),
    }


class CompositionAuthority:
    """Specific S3-compatible encrypted CAS; deliberately no deletion API."""

    def __init__(self, backend, dimension, namespace, material, private_dir):
        self.client = backend.client
        self.bucket = dimension.bucket
        self.namespace = namespace
        self.prefix = namespace_prefix(namespace)
        self.binding = dimension_binding(dimension, material.encryption_domain_id)
        self.material = material
        self.private = Path(private_dir)
        self.recipients = list(
            dict.fromkeys([material.recipient, *material.keyset.recovery_recipients])
        )
        if len(self.recipients) < 2:
            raise ValueError(
                "composition requires distinct operational and recovery recipients"
            )

    def _read(self, suffix):
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=self.prefix + suffix
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in (
                "NoSuchKey",
                "404",
                "NotFound",
            ):
                return None, None
            raise ValueError("composition object read failed") from None
        stream = response["Body"]
        try:
            raw = stream.read(LIMIT + 1)
        finally:
            stream.close()
        etag = response.get("ETag")
        if len(raw) > LIMIT or not isinstance(etag, str) or not etag:
            raise ValueError("composition object exceeds bounds or has no paired ETag")
        return raw, etag

    def read(self):
        raw, etag = self._read("authority.age")
        if raw is None:
            return None, etag
        path = self.private / "authority-read.age"
        path.write_bytes(raw)
        path.chmod(0o600)
        try:
            body = json.loads(decrypt(path, [self.material.identity], LIMIT))
        finally:
            path.unlink(missing_ok=True)
        self.validate(body)
        return body, etag

    def validate(self, body):
        if (
            not isinstance(body, dict)
            or set(body)
            != {
                "format",
                "namespace",
                "binding",
                "request",
                "secret",
                "repository_id",
                "state",
                "capture_claims",
            }
            or body["format"] != "composition-authority-v1"
            or body["namespace"] != self.namespace
            or body["binding"] != self.binding
            or not isinstance(body["secret"], str)
            or re.fullmatch("[A-Za-z0-9_-]{43}", body["secret"]) is None
        ):
            raise ValueError("composition authority binding differs")
        if (
            not isinstance(body["capture_claims"], dict)
            or len(body["capture_claims"]) > 192
        ):
            raise ValueError("invalid composition capture claims")
        if (
            body["repository_id"] is not None
            and re.fullmatch("[a-f0-9]{64}", body["repository_id"]) is None
        ):
            raise ValueError("invalid composition repository binding")
        if body["state"] is not None:
            state = CompositionSafetyState.from_dict(body["state"]).to_dict()
            expected = {
                k: self.binding[k]
                for k in (
                    "provider",
                    "connection_id",
                    "dimension_id",
                    "encryption_domain_id",
                )
            }
            expected.update(
                repository_id=body["repository_id"], catalog_key="catalog.jroom.age"
            )
            if state["scope"] != expected:
                raise ValueError(
                    "composition state belongs to another namespace authority"
                )

    def put(self, body, expected_etag):
        self.validate(body)
        if len(canonical(body)) > LIMIT // 2:
            raise ValueError("composition authority exceeds size limit")
        path = self.private / "authority-write.age"
        encrypt(canonical(body), self.recipients, path)
        try:
            kwargs = {
                "Bucket": self.bucket,
                "Key": self.prefix + "authority.age",
                "Body": path.read_bytes(),
            }
            kwargs.update(
                {"IfNoneMatch": "*"}
                if expected_etag is None
                else {"IfMatch": expected_etag}
            )
            try:
                self.client.put_object(**kwargs)
            except Exception:  # noqa: BLE001 - SDK errors may contain credentials; reconcile exact state, never repeat write.
                observed, etag = self.read()
                if canonical(observed) != canonical(body):
                    raise ValueError(
                        "composition commit unconfirmed; resume with the same namespace"
                    ) from None
                return etag
            observed, etag = self.read()
            if canonical(observed) != canonical(body):
                raise ValueError("composition commit readback differs")
            return etag
        finally:
            path.unlink(missing_ok=True)

    def put_descriptor(self, descriptor):
        path = self.private / "descriptor-write.age"
        encrypt(descriptor.to_json().encode(), self.recipients, path)
        try:
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            key = "objects/sha256/" + digest
            self.client.put_object(
                Bucket=self.bucket, Key=self.prefix + key, Body=raw, IfNoneMatch="*"
            )
            observed, _ = self._read(key)
            if observed != raw:
                raise ValueError("composition descriptor readback differs")
            return ObjectRef(key, digest, len(raw))
        finally:
            path.unlink(missing_ok=True)

    def descriptor(self, record):
        from .logical_jat import LogicalJat

        key = record["object_key"]
        if re.fullmatch("objects/sha256/[a-f0-9]{64}", key) is None:
            raise ValueError("invalid composition descriptor key")
        raw, _ = self._read(key)
        if (
            raw is None
            or len(raw) != record["ciphertext_size"]
            or hashlib.sha256(raw).hexdigest() != record["ciphertext_sha256"]
        ):
            raise ValueError("composition descriptor is missing or corrupt")
        path = self.private / "descriptor-read.age"
        path.write_bytes(raw)
        path.chmod(0o600)
        try:
            return LogicalJat.from_dict(
                json.loads(decrypt(path, [self.material.identity], LIMIT))
            )
        finally:
            path.unlink(missing_ok=True)
