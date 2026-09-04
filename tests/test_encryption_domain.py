import hashlib
import importlib
import io
import json
import subprocess
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from josh_room import crypto
from josh_room.catalog import Catalog
from josh_room.config import DimensionConfig
from josh_room.r2 import R2Backend, R2Config, R2Conflict

OPERATIONAL_RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"
RECOVERY_RECIPIENT = "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"


def domain_module():
    try:
        return importlib.import_module("josh_room.encryption_domain")
    except ModuleNotFoundError as error:
        pytest.fail(f"encryption domain contract module is missing: {error.name}")


def keyset(**overrides):
    module = domain_module()
    values = {
        "provider": "minio",
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "synthetic-bucket",
        "operational_identity": "AGE-SECRET-KEY-synthetic",
        "operational_recipient": OPERATIONAL_RECIPIENT,
        "recovery_recipients": [RECOVERY_RECIPIENT],
    }
    values.update(overrides)
    return module.EncryptionKeyset.create(**values)


def test_two_physical_buckets_receive_independent_random_keysets():
    first = keyset(bucket="first-bucket")
    second = keyset(bucket="second-bucket")

    assert first.encryption_domain_id != second.encryption_domain_id
    assert first.binding != second.binding
    assert first.key_generation == second.key_generation == 1


def test_aliases_of_one_physical_bucket_share_a_stable_binding():
    module = domain_module()

    first = module.physical_bucket_identity("minio", "https://MINIO.example.invalid/", "room")
    second = module.physical_bucket_identity("minio", "https://minio.example.invalid", "room")

    assert first == second


def test_keyset_enrollment_reconciles_aliases_and_rejects_identity_reuse():
    module = domain_module()
    first = keyset()
    alias = keyset(encryption_domain_id=str(first.encryption_domain_id))
    other_bucket = keyset(bucket="other-bucket")

    assert module.reconcile_keyset(first, alias) is first
    with pytest.raises(ValueError, match="operational identity is already bound"):
        module.reconcile_keyset(None, other_bucket, occupied=(first,))


def test_keyset_create_requires_operational_identity():
    module = domain_module()

    with pytest.raises(TypeError, match="operational.?identity"):
        module.EncryptionKeyset.create(
            provider="minio",
            endpoint="http://127.0.0.1:9000",
            bucket="synthetic-bucket",
            operational_recipient=OPERATIONAL_RECIPIENT,
            recovery_recipients=[RECOVERY_RECIPIENT],
        )


def test_keyset_rejects_unknown_fields():
    module = domain_module()
    body = keyset().to_dict()
    body["unexpected"] = "synthetic"

    with pytest.raises(ValueError, match="unknown keyset field"):
        module.EncryptionKeyset.from_dict(body)


def test_keyset_serialization_has_a_bounded_size():
    module = domain_module()
    body = keyset().to_dict()
    body["recovery_recipients"] = ["age1" + "r" * 70_000]

    with pytest.raises(ValueError, match="keyset exceeds maximum size"):
        module.EncryptionKeyset.from_json(json.dumps(body).encode())


def test_keyset_binding_rejects_provider_endpoint_or_bucket_mismatch():
    module = domain_module()
    body = keyset().to_dict()

    with pytest.raises(ValueError, match="provider binding"):
        module.EncryptionKeyset.from_dict(body, provider="r2")
    with pytest.raises(ValueError, match="endpoint binding"):
        module.EncryptionKeyset.from_dict(body, endpoint="http://127.0.0.1:9001")
    with pytest.raises(ValueError, match="bucket binding"):
        module.EncryptionKeyset.from_dict(body, bucket="another-bucket")


def test_keyset_has_random_domain_id_and_positive_generation():
    first = keyset()
    second = keyset()

    assert first.encryption_domain_id != second.encryption_domain_id
    assert first.key_generation > 0
    with pytest.raises(ValueError, match="key generation"):
        keyset(key_generation=0)


def test_keyset_rejects_duplicate_operational_and_recovery_recipients():
    with pytest.raises(ValueError, match="duplicate recipient"):
        keyset(recovery_recipients=[OPERATIONAL_RECIPIENT])
    with pytest.raises(ValueError, match="duplicate recipient"):
        keyset(recovery_recipients=[RECOVERY_RECIPIENT, RECOVERY_RECIPIENT])


def test_keyset_rejects_invalid_age_recipient_syntax():
    with pytest.raises(ValueError, match="recipient"):
        keyset(operational_recipient="age1daily")


def test_keyset_serializes_operational_identity_but_not_recovery_private_identity():
    module = domain_module()
    serialized = json.dumps(keyset().to_dict())

    assert "AGE-SECRET-KEY-synthetic" in serialized
    assert "recovery_identity" not in serialized
    with pytest.raises(ValueError, match="unknown keyset field"):
        module.EncryptionKeyset.from_dict({**keyset().to_dict(), "recovery_identity": "private"})


def test_non_loopback_http_endpoints_are_rejected():
    domain_module()

    with pytest.raises(ValueError, match="loopback"):
        keyset(endpoint="http://storage.example.invalid:9000")
    keyset(endpoint="http://localhost:9000")


def test_managed_age_identity_generation_and_recipient_derivation(tmp_path, monkeypatch):
    identity = tmp_path / "domain.agekey"
    managed_age_keygen = tmp_path / "managed" / "bin" / "age-keygen"
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "-y" in argv:
            return subprocess.CompletedProcess(argv, 0, OPERATIONAL_RECIPIENT.encode() + b"\n", b"")
        Path(argv[argv.index("-o") + 1]).write_text("AGE-SECRET-KEY-synthetic\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(crypto, "_managed_executable", lambda _name: managed_age_keygen)
    monkeypatch.setattr(crypto.subprocess, "run", fake_run)

    crypto.generate_identity(identity)
    recipient = crypto.derive_recipient(identity)

    assert recipient == OPERATIONAL_RECIPIENT
    assert keyset(operational_recipient=recipient).operational_recipient == recipient
    assert identity.stat().st_mode & 0o777 == 0o600
    assert len(calls) == 2
    assert all("AGE-SECRET-KEY-synthetic" not in str(call) for call in calls)


def test_scoped_keyring_helpers_keep_identity_in_secret_input(monkeypatch):
    from josh_room import keyring

    calls = []
    monkeypatch.setattr(keyring, "available", lambda: True)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "AGE-SECRET-KEY-synthetic\n", "")

    monkeypatch.setattr(keyring.subprocess, "run", fake_run)
    keyring.store_encryption_identity("domain-1", 1, "AGE-SECRET-KEY-synthetic")
    assert keyring.lookup_encryption_identity("domain-1", 1) == "AGE-SECRET-KEY-synthetic"
    assert all("AGE-SECRET-KEY-synthetic" not in str(argv) for argv, _kwargs in calls)
    assert calls[0][1]["input"] == "AGE-SECRET-KEY-synthetic\n"


def test_encryption_material_requires_derived_identity_recipient(tmp_path, monkeypatch):
    module = domain_module()
    identity = tmp_path / "identity"
    identity.write_text("AGE-SECRET-KEY-synthetic\n")
    identity.chmod(0o600)
    monkeypatch.setattr(crypto, "derive_recipient", lambda _identity: OPERATIONAL_RECIPIENT)

    material = module.EncryptionMaterial(keyset(), identity)
    assert material.recipient == OPERATIONAL_RECIPIENT
    monkeypatch.setattr(crypto, "derive_recipient", lambda _identity: RECOVERY_RECIPIENT)
    with pytest.raises(ValueError, match="operational recipient"):
        module.EncryptionMaterial(keyset(), identity)


class ControlS3:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise ClientError({"Error": {"Code": "412"}}, "PutObject")
        if kwargs.get("IfMatch") and kwargs["IfMatch"] != self.objects.get(key, {}).get("etag"):
            raise ClientError({"Error": {"Code": "409"}}, "PutObject")
        body = kwargs["Body"].read() if hasattr(kwargs["Body"], "read") else kwargs["Body"]
        self.objects[key] = {"body": body, "etag": '"synthetic-etag"'}
        return {"ETag": '"synthetic-etag"'}

    def head_object(self, **kwargs):
        self.calls.append(("head_object", kwargs))
        try:
            item = self.objects[kwargs["Key"]]
        except KeyError as error:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject") from error
        return {"ContentLength": len(item["body"]), "ETag": item["etag"]}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        item = self.objects[kwargs["Key"]]
        return {"ContentLength": len(item["body"]), "Body": io.BytesIO(item["body"])}


def test_control_objects_are_allowlisted_and_conditionally_verified(tmp_path):
    module = domain_module()
    fake = ControlS3()
    store = R2Backend(R2Config("https://example.invalid", "synthetic", "synthetic"), client=fake, receipt_dir=tmp_path)
    body = b'{"synthetic":"control"}'

    with pytest.raises(ValueError, match="control key"):
        store.read_control("objects/not-control", 1024)
    assert store.read_control(module.KEYSET_CONTROL_KEY, 1024) == (None, None)
    etag = store.create_control(module.KEYSET_CONTROL_KEY, body)
    assert store.read_control(module.KEYSET_CONTROL_KEY, 1024) == (body, etag)
    store.replace_control(module.KEYSET_CONTROL_KEY, b'{"next":true}', etag)
    with pytest.raises(R2Conflict) as failure:
        store.replace_control(module.KEYSET_CONTROL_KEY, b'{"never":"reported"}', '"stale"')
    assert "never" not in str(failure.value)


def test_control_read_enforces_size_bound_without_exposing_body(tmp_path):
    module = domain_module()
    fake = ControlS3()
    store = R2Backend(R2Config("https://example.invalid", "synthetic", "synthetic"), client=fake, receipt_dir=tmp_path)
    fake.objects[module.MIGRATION_JOURNAL_KEY] = {"body": b"secret-control-body", "etag": '"etag"'}

    with pytest.raises(ValueError, match="control object exceeds maximum size") as failure:
        store.read_control(module.MIGRATION_JOURNAL_KEY, 4)
    assert "secret-control-body" not in str(failure.value)


def test_control_publication_failure_is_explicitly_outcome_unknown(tmp_path):
    module = domain_module()
    fake = ControlS3()

    def fail(**_kwargs):
        raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")

    fake.put_object = fail
    store = R2Backend(R2Config("https://example.invalid", "synthetic", "synthetic"), client=fake, receipt_dir=tmp_path)

    with pytest.raises(RuntimeError) as failure:
        store.create_control(module.KEYSET_CONTROL_KEY, b"control-body")
    assert failure.value.__class__.__name__ == "R2PublicationError"
    assert failure.value.published is True
    assert "control-body" not in str(failure.value)


def test_control_transport_failure_is_outcome_unknown(tmp_path):
    module = domain_module()
    fake = ControlS3()

    def fail(**_kwargs):
        raise EndpointConnectionError(endpoint_url="https://synthetic.invalid")

    fake.put_object = fail
    store = R2Backend(R2Config("https://example.invalid", "synthetic", "synthetic"), client=fake, receipt_dir=tmp_path)

    with pytest.raises(RuntimeError) as failure:
        store.create_control(module.KEYSET_CONTROL_KEY, b"control-body")
    assert failure.value.__class__.__name__ == "R2PublicationError"
    assert failure.value.published is True


def test_control_limit_is_fixed_and_not_configured_object_limit(tmp_path):
    module = domain_module()
    fake = ControlS3()
    store = R2Backend(
        R2Config("https://example.invalid", "synthetic", "synthetic", max_bytes=1),
        client=fake,
        receipt_dir=tmp_path,
    )

    store.create_control(module.KEYSET_CONTROL_KEY, b"two")
    assert store.read_control(module.KEYSET_CONTROL_KEY, 3)[0] == b"two"


def test_dimension_config_round_trips_encryption_domain_id():
    dimension = DimensionConfig.from_private(
        "archive",
        {
            "display_name": "Archive",
            "provider": "r2",
            "endpoint": "https://example.invalid",
            "bucket": "archive",
            "credential_profile": "archive-profile",
            "encryption_domain_id": "domain-1",
        },
    )

    assert dimension.encryption_domain_id == "domain-1"
    assert dimension.to_private()["encryption_domain_id"] == "domain-1"


def test_catalog_validates_domain_id_alongside_v2_dimension_id():
    module = domain_module()
    catalog = Catalog.empty(dimension_id="archive", encryption_domain_id="domain-1")

    assert catalog.body["dimension_id"] == "archive"
    assert catalog.body["encryption_domain_id"] == "domain-1"
    assert Catalog.from_body(catalog.body, dimension_id="archive", encryption_domain_id="domain-1").encryption_domain_id == "domain-1"
    with pytest.raises(ValueError, match="domain"):
        Catalog.from_body(catalog.body, dimension_id="archive", encryption_domain_id="domain-2")
    with pytest.raises(ValueError, match="encryption domain"):
        Catalog({**catalog.body, "encryption_domain_id": "../bad"})
    assert module.EncryptionKeyset.from_json(json.dumps(keyset().to_dict()).encode()).provider == "minio"


def test_read_only_envelope_verification_streams_without_output(tmp_path, monkeypatch):
    from josh_room.envelope import build_envelope_file, verify_envelope_file

    payload = tmp_path / "payload"
    payload.write_bytes(b"synthetic-payload")
    envelope = tmp_path / "envelope.tar"
    build_envelope_file(
        {
            "format_version": 1,
            "project_id": "synthetic",
            "snapshot_id": "snap-1",
            "created_at": "2026-09-04T00:00:00+00:00",
            "payload": {
                "format": "jat-hauler",
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                "size": payload.stat().st_size,
                "producer_version": "synthetic",
            },
            "source": {},
        },
        payload,
        envelope,
    )
    before = set(tmp_path.iterdir())
    monkeypatch.setattr("josh_room.envelope.tempfile.mkstemp", lambda **_kwargs: pytest.fail("verification wrote output"))

    assert verify_envelope_file(envelope)["snapshot_id"] == "snap-1"
    assert set(tmp_path.iterdir()) == before


def test_catalog_accepts_optional_encryption_domain_id_and_reads_v1():
    legacy = Catalog.empty().add_snapshot(
        "synthetic",
        "Synthetic",
        {
            "snapshot_id": "snap-1",
            "object_key": "objects/sha256/" + "a" * 64,
            "ciphertext_sha256": "a" * 64,
            "ciphertext_size": 1,
        },
    )
    module = domain_module()
    current = Catalog.empty(encryption_domain_id="domain-1")

    assert legacy.encryption_domain_id is None
    assert current.encryption_domain_id == "domain-1"
    assert module.EncryptionKeyset.from_json(json.dumps(keyset().to_dict()).encode()).provider == "minio"
