import base64
import dataclasses
import json

import pytest
from test_minio_encryption_flow import dimension, identity, make_keyset
from test_r2_room_store_material import Broker
from test_room_store_material import RoomStoreBackend

from josh_room import auth
from josh_room import portable_room_store as portable
from josh_room import r2_room_store_material as client
from josh_room.encryption_domain import EncryptionKeyset


@pytest.fixture
def migration(monkeypatch, tmp_path):
    source = make_keyset().upgrade_for_room_store().bind_repository("a" * 64, expected_generation=1)
    material = type("Material", (), {"keyset": source, "identity": identity(tmp_path / "identity")})()
    selected = dimension(options=(("room_store_authority_endpoint", "https://authority.example.invalid"), ("room_store_authority_profile", "portable")))
    backend = RoomStoreBackend(control=source.to_json())
    broker = Broker()
    original = broker.__call__

    def transport(method, session, operation, headers, body):
        if operation == "seal":
            broker.repository_error = "room_store_authority_sealed"
            return 200, {"status": "sealed"}
        from test_r2_room_store_material import CAPABILITY, SESSION
        return original(method, SESSION, operation, {**headers, "Authorization": f"Bearer {CAPABILITY}"}, body)

    def encrypt(data, recipients, output):
        output.write_bytes(data)
        output.chmod(0o600)

    monkeypatch.setattr(client.crypto, "encrypt", encrypt)
    monkeypatch.setattr(client.crypto, "decrypt", lambda path, identities, limit: path.read_bytes())
    monkeypatch.setattr(client.keyring, "store_room_store_secret", lambda *args: None)
    monkeypatch.setattr(auth, "_material_for_keyset", lambda keyset: type("Material", (), {"keyset": keyset, "identity": material.identity})())
    return selected, backend, material, broker, transport


def test_migration_encrypts_only_password_preserves_domain_and_fences_legacy(migration):
    selected, backend, material, broker, transport = migration
    marker = portable.migrate(selected, backend, material, transport=transport)
    assert material.keyset.room_store.secret not in backend.control.decode()
    with pytest.raises((ValueError, TypeError)):
        EncryptionKeyset.from_json(backend.control)
    inner = json.loads(base64.urlsafe_b64decode(broker.record["ciphertext"] + "=" * (-len(broker.record["ciphertext"]) % 4)))
    assert inner["secret"] == material.keyset.room_store.secret
    assert "operational_identity" not in inner
    assert "recovery_recipients" not in inner
    assert broker.record["repositoryId"] == material.keyset.room_store.repository_id
    assert broker.record["keysetGeneration"] == material.keyset.room_store.generation
    resolved = portable.marker_keyset(marker, selected, material_factory=lambda _: material, transport=transport)
    assert resolved == material.keyset
    assert resolved.room_store.secret == material.keyset.room_store.secret


def test_unmigrated_store_keeps_legacy_authority_without_writes(migration, monkeypatch):
    selected, backend, _material, broker, _transport = migration
    before = backend.control
    monkeypatch.setattr(portable, "authority", lambda *_args, **_kwargs: pytest.fail("unmigrated store contacted DO authority"))
    assert portable.selected_keyset(selected, backend) is None
    assert backend.control == before
    assert broker.record is None


def test_rollback_seals_before_restoring_legacy_and_keeps_password(migration):
    selected, backend, material, broker, transport = migration
    portable.migrate(selected, backend, material, transport=transport)
    restored = portable.rollback(selected, backend, transport=transport)
    assert broker.repository_error == "room_store_authority_sealed"
    assert restored == material.keyset
    assert restored.room_store.secret == material.keyset.room_store.secret
    assert EncryptionKeyset.from_json(backend.control) == material.keyset


def test_migration_cannot_overwrite_a_concurrent_legacy_bind(migration):
    selected, backend, material, _broker, transport = migration
    backend.replace_conflict_winner = material.keyset
    with pytest.raises(RuntimeError, match="publication unverified"):
        portable.migrate(selected, backend, material, transport=transport)
    assert EncryptionKeyset.from_json(backend.control) == material.keyset


def test_marker_rejects_wrong_authority_alias_before_network_or_decryption(migration):
    selected, backend, material, _broker, transport = migration
    marker = portable.migrate(selected, backend, material, transport=transport)
    marker["authority"] = "https://wrong.example.invalid"
    with pytest.raises(ValueError, match="selection mismatch"):
        portable.marker_keyset(marker, selected, material_factory=lambda _: pytest.fail("no decryption"), transport=lambda *args: pytest.fail("no network"))


def test_migration_never_accepts_same_metadata_with_a_different_password(migration):
    selected, backend, material, broker, transport = migration
    changed = dataclasses.replace(material.keyset, room_store=dataclasses.replace(material.keyset.room_store, secret=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")))
    backend.control = changed.to_json()
    with pytest.raises(ValueError, match="current format-2"):
        portable.migrate(selected, backend, material, transport=transport)
    assert broker.record is None
