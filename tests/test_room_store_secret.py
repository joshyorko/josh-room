import base64
import dataclasses
import json

import pytest
from synthetic_identity import synthetic_identity

from josh_room import encryption_domain, keyring

OPERATIONAL_RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"
RECOVERY_RECIPIENT = "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"


def keyset(**overrides):
    values = {
        "provider": "minio",
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "synthetic-bucket",
        "operational_identity": synthetic_identity("room-store"),
        "operational_recipient": OPERATIONAL_RECIPIENT,
        "recovery_recipients": [RECOVERY_RECIPIENT],
    }
    values.update(overrides)
    return encryption_domain.EncryptionKeyset.create(**values)


def secret(seed: int = 1) -> str:
    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode().rstrip("=")


def test_v1_keyset_round_trips_unchanged_until_explicit_upgrade():
    original = keyset()
    restored = encryption_domain.EncryptionKeyset.from_json(original.to_json())

    assert original.format_version == restored.format_version == 1
    assert "room_store" not in original.to_dict()
    assert restored.to_json() == original.to_json()


def test_upgrade_generates_a_random_room_store_secret_without_rotating_age_generation(monkeypatch):
    calls = []

    def random_bytes(size):
        calls.append(size)
        return bytes((index + len(calls) - 1) % 256 for index in range(size))

    monkeypatch.setattr(encryption_domain.secrets, "token_bytes", random_bytes)
    original = keyset(key_generation=7)
    upgraded = original.upgrade_for_room_store()
    store = upgraded.room_store

    assert calls == [32]
    assert upgraded.format_version == 2
    assert upgraded.key_generation == original.key_generation == 7
    assert store.secret == base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    assert store.repository_prefix == "room-store/v1"
    assert store.repository_format == 2
    assert store.physical_binding == original.binding
    assert store.repository_id is None
    assert store.generation == 1
    second = original.upgrade_for_room_store().room_store.secret
    assert calls == [32, 32]
    assert second != store.secret


def test_room_store_secret_is_hidden_from_repr_and_excluded_from_equality():
    first = keyset().upgrade_for_room_store()
    alternate = dataclasses.replace(first, room_store=dataclasses.replace(first.room_store, secret=secret(2)))

    assert first == alternate
    assert first.room_store == dataclasses.replace(first.room_store, secret=secret(2))
    assert first.room_store.secret not in repr(first)
    assert first.room_store.secret not in repr(first.room_store)


def test_v2_keyset_serialization_round_trips_and_rejects_unknown_fields():
    upgraded = keyset().upgrade_for_room_store()
    serialized = upgraded.to_json()

    assert encryption_domain.EncryptionKeyset.from_json(serialized) == upgraded
    body = json.loads(serialized)
    body["room_store"]["unexpected"] = "value"
    with pytest.raises(ValueError, match="unknown Room Store field"):
        encryption_domain.EncryptionKeyset.from_dict(body)


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": True},
        {"room_store": {"format_version": True}},
        {"room_store": {"generation": True}},
        {"room_store": {"repository_format": True}},
        {"room_store": {"secret": "short"}},
        {"room_store": {"secret": secret() + "="}},
        {"room_store": {"repository_prefix": "room-store/v2"}},
        {"room_store": {"repository_format": 1}},
    ],
)
def test_v2_keyset_rejects_invalid_or_noncanonical_metadata(change):
    body = keyset().upgrade_for_room_store().to_dict()
    if "room_store" in change:
        body["room_store"].update(change["room_store"])
    else:
        body.update(change)

    with pytest.raises((TypeError, ValueError)):
        encryption_domain.EncryptionKeyset.from_dict(body)


def test_v1_unknown_version_and_v1_room_store_extension_fail_closed():
    body = keyset().to_dict()
    with pytest.raises(ValueError, match="unsupported keyset format"):
        encryption_domain.EncryptionKeyset.from_dict({**body, "format_version": 3})
    with pytest.raises(ValueError, match="unknown keyset field"):
        encryption_domain.EncryptionKeyset.from_dict({**body, "room_store": {}})


def test_repository_binding_is_create_once_and_advances_only_room_store_generation():
    original = keyset(key_generation=5).upgrade_for_room_store()
    bound = original.bind_repository("a" * 64, expected_generation=1)

    assert bound.room_store.repository_id == "a" * 64
    assert bound.room_store.generation == 2
    assert bound.key_generation == 5
    assert bound.bind_repository("a" * 64, expected_generation=2) is bound
    with pytest.raises(ValueError, match="generation"):
        original.bind_repository("a" * 64, expected_generation=2)
    with pytest.raises(ValueError, match="repository id is immutable"):
        bound.bind_repository("b" * 64, expected_generation=2)


def test_alias_reconciliation_requires_the_same_secret_and_repository_id():
    first = keyset().upgrade_for_room_store()
    alias = keyset(endpoint="http://127.0.0.1:9000/").upgrade_for_room_store()
    same = dataclasses.replace(alias, room_store=dataclasses.replace(alias.room_store, secret=first.room_store.secret))

    assert encryption_domain.reconcile_keyset(first, same) is first
    different_secret = dataclasses.replace(alias, room_store=dataclasses.replace(alias.room_store, secret=secret(2)))
    with pytest.raises(ValueError, match="Room Store secret mismatch"):
        encryption_domain.reconcile_keyset(first, different_secret)

    bound = first.bind_repository("a" * 64, expected_generation=1)
    other_repository = dataclasses.replace(
        bound,
        room_store=dataclasses.replace(bound.room_store, repository_id="b" * 64),
    )
    with pytest.raises(ValueError, match="repository id mismatch"):
        encryption_domain.reconcile_keyset(bound, other_repository)


def test_room_store_keyring_scope_uses_domain_and_secret_generation(monkeypatch):
    calls = []
    monkeypatch.setattr(keyring, "available", lambda: True)
    monkeypatch.setattr(keyring, "_platform_name", lambda: "linux")
    monkeypatch.setattr(keyring, "secure_store", lambda *args: calls.append(("store", args)))
    monkeypatch.setattr(keyring, "secure_lookup", lambda *args: calls.append(("lookup", args)) or secret())

    keyring.store_room_store_secret("00000000-0000-4000-8000-000000000001", 1, secret())
    assert keyring.lookup_room_store_secret("00000000-0000-4000-8000-000000000001", 1) == secret()
    assert calls == [
        ("store", ("room-store-00000000-0000-4000-8000-000000000001-1", "room-store-secret", secret())),
        ("lookup", ("room-store-00000000-0000-4000-8000-000000000001-1", "room-store-secret")),
    ]


@pytest.mark.parametrize("domain,generation,value", [
    ("invalid", 1, secret()),
    ("00000000-0000-4000-8000-000000000001", 0, secret()),
    ("00000000-0000-4000-8000-000000000001", 1, "invalid"),
])
def test_unavailable_native_cache_still_validates_material(monkeypatch, domain, generation, value):
    monkeypatch.setattr(keyring, "available", lambda: False)
    with pytest.raises(ValueError):
        keyring.store_room_store_secret(domain, generation, value)
