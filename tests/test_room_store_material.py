import dataclasses

import pytest
from test_minio_encryption_flow import DOMAIN_ID, FakeBackend, dimension, make_keyset

from josh_room import auth as auth_module
from josh_room.encryption_domain import EncryptionKeyset
from josh_room.r2 import R2Conflict


def secret(seed: int = 1) -> str:
    import base64

    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode().rstrip("=")


class RoomStoreBackend(FakeBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.replace_conflict_winner = None
        self.replace_calls = []

    def replace_control(self, key, body, expected_etag):
        self.calls.append(("replace_control", key, body, expected_etag))
        self.replace_calls.append((key, body, expected_etag))
        if self.control_etag != expected_etag:
            raise R2Conflict("control object conditional conflict")
        if self.replace_conflict_winner is not None:
            self.control = self.replace_conflict_winner.to_json()
            self.control_etag = '"control-winner"'
            raise R2Conflict("control object conditional conflict")
        self.control = body
        self.control_etag = '"control-next"'
        return self.control_etag


def v1_keyset(**overrides):
    return make_keyset(**overrides)


def test_ensure_room_store_keyset_upgrades_v1_once_and_caches_durable_winner(monkeypatch):
    original = v1_keyset()
    backend = RoomStoreBackend(control=original.to_json())
    cache = []
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *args: cache.append(args))

    upgraded = auth_module.ensure_room_store_keyset(dimension(encryption_domain_id=DOMAIN_ID), backend)

    assert upgraded.format_version == 2
    assert upgraded.key_generation == original.key_generation
    assert upgraded.room_store.generation == 1
    assert upgraded.room_store.repository_id is None
    assert backend.replace_calls[0][2] == '"control-1"'
    assert EncryptionKeyset.from_json(backend.control).room_store.secret == upgraded.room_store.secret
    assert cache == [(DOMAIN_ID, 1, upgraded.room_store.secret)]


def test_concurrent_v1_upgraders_reload_and_cache_the_remote_winner(monkeypatch):
    original = v1_keyset()
    winner = original.upgrade_for_room_store()
    winner = dataclasses.replace(winner, room_store=dataclasses.replace(winner.room_store, secret=secret(2)))
    backend = RoomStoreBackend(control=original.to_json())
    backend.replace_conflict_winner = winner
    cache = []
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *args: cache.append(args))

    result = auth_module.ensure_room_store_keyset(dimension(encryption_domain_id=DOMAIN_ID), backend)

    assert result.room_store.secret == winner.room_store.secret
    assert cache == [(DOMAIN_ID, 1, winner.room_store.secret)]
    assert EncryptionKeyset.from_json(backend.control).room_store.secret == winner.room_store.secret


@pytest.mark.parametrize("upgraded", [False, True])
def test_missing_native_keyring_uses_durable_room_store_material(monkeypatch, upgraded):
    from josh_room import keyring

    original = v1_keyset()
    if upgraded:
        original = original.upgrade_for_room_store()
    backend = RoomStoreBackend(control=original.to_json())
    monkeypatch.setattr(keyring, "available", lambda: False)
    monkeypatch.setattr(keyring, "secure_store", lambda *_args: pytest.fail("no fallback write"))
    selected = dimension(encryption_domain_id=DOMAIN_ID)

    material = auth_module.ensure_room_store_keyset(selected, backend)
    bound = auth_module.bind_room_store_repository(
        selected, backend, "a" * 64, expected_generation=1,
    )
    repeated = auth_module.bind_room_store_repository(
        selected, backend, "a" * 64, expected_generation=2,
    )

    assert material.room_store.secret == bound.room_store.secret
    assert bound.room_store.repository_id == "a" * 64
    assert bound.room_store.generation == 2
    assert repeated == bound
    assert EncryptionKeyset.from_json(backend.control) == bound


def test_keyring_cache_failure_happens_after_the_durable_upgrade(monkeypatch):
    backend = RoomStoreBackend(control=v1_keyset().to_json())
    monkeypatch.setattr(
        auth_module,
        "store_room_store_secret",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("OS Secret Service is unavailable")),
    )

    with pytest.raises(RuntimeError, match="OS Secret Service"):
        auth_module.ensure_room_store_keyset(dimension(encryption_domain_id=DOMAIN_ID), backend)

    assert EncryptionKeyset.from_json(backend.control).format_version == 2


def test_room_store_keyset_rejects_unsupported_provider_and_missing_v1_record(monkeypatch):
    backend = RoomStoreBackend()
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *_args: pytest.fail("must not cache"))

    with pytest.raises(auth_module.EncryptionStateError, match="uninitialized"):
        auth_module.ensure_room_store_keyset(dimension(), backend)
    with pytest.raises(auth_module.EncryptionStateError, match="unsupported"):
        auth_module.ensure_room_store_keyset(dimension(provider="r2"), backend)


def test_repository_binding_persists_once_and_is_idempotent(monkeypatch):
    initial = v1_keyset().upgrade_for_room_store()
    backend = RoomStoreBackend(control=initial.to_json())
    cache = []
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *args: cache.append(args))

    bound = auth_module.bind_room_store_repository(
        dimension(encryption_domain_id=DOMAIN_ID),
        backend,
        "a" * 64,
        expected_generation=1,
    )
    repeated = auth_module.bind_room_store_repository(
        dimension(encryption_domain_id=DOMAIN_ID),
        backend,
        "a" * 64,
        expected_generation=2,
    )

    assert bound.room_store.repository_id == "a" * 64
    assert bound.room_store.generation == 2
    assert repeated == bound
    assert len(backend.replace_calls) == 1
    assert cache == [(DOMAIN_ID, 2, bound.room_store.secret), (DOMAIN_ID, 2, bound.room_store.secret)]


def test_concurrent_same_repository_binding_reloads_matching_winner(monkeypatch):
    initial = v1_keyset().upgrade_for_room_store()
    winner = initial.bind_repository("a" * 64, expected_generation=1)
    backend = RoomStoreBackend(control=initial.to_json())
    backend.replace_conflict_winner = winner
    cache = []
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *args: cache.append(args))

    result = auth_module.bind_room_store_repository(
        dimension(encryption_domain_id=DOMAIN_ID),
        backend,
        "a" * 64,
        expected_generation=1,
    )

    assert result.room_store.repository_id == "a" * 64
    assert result.room_store.generation == 2
    assert cache == [(DOMAIN_ID, 2, initial.room_store.secret)]


def test_repository_binding_rejects_stale_generation_and_competing_repository(monkeypatch):
    initial = v1_keyset().upgrade_for_room_store()
    backend = RoomStoreBackend(control=initial.to_json())
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *_args: pytest.fail("must not cache"))

    with pytest.raises(ValueError, match="generation"):
        auth_module.bind_room_store_repository(
            dimension(encryption_domain_id=DOMAIN_ID), backend, "a" * 64, expected_generation=2,
        )

    backend.replace_conflict_winner = initial.bind_repository("b" * 64, expected_generation=1)
    with pytest.raises(auth_module.EncryptionStateError, match="conflict"):
        auth_module.bind_room_store_repository(
            dimension(encryption_domain_id=DOMAIN_ID), backend, "a" * 64, expected_generation=1,
        )


def test_repository_binding_rejects_wrong_dimension_and_password(monkeypatch):
    initial = v1_keyset().upgrade_for_room_store()
    backend = RoomStoreBackend(control=initial.to_json())
    monkeypatch.setattr(auth_module, "store_room_store_secret", lambda *_args: pytest.fail("must not cache"))

    with pytest.raises(auth_module.EncryptionStateError, match="domain"):
        auth_module.bind_room_store_repository(
            dimension(encryption_domain_id="00000000-0000-4000-8000-000000000002"),
            backend,
            "a" * 64,
            expected_generation=1,
        )

    competing = dataclasses.replace(
        initial,
        room_store=dataclasses.replace(initial.room_store, secret=secret(2)),
    )
    backend.replace_conflict_winner = competing.bind_repository("a" * 64, expected_generation=1)
    with pytest.raises(auth_module.EncryptionStateError, match="conflict"):
        auth_module.bind_room_store_repository(
            dimension(encryption_domain_id=DOMAIN_ID),
            backend,
            "a" * 64,
            expected_generation=1,
        )


def test_native_cache_write_error_is_not_optional(monkeypatch):
    from josh_room import keyring

    backend = RoomStoreBackend(control=v1_keyset().to_json())
    monkeypatch.setattr(keyring, "available", lambda: True)

    def fail_write(*_args):
        raise keyring.SecureBackendError("synthetic native write failure")

    monkeypatch.setattr(keyring, "secure_store", fail_write)
    with pytest.raises(RuntimeError, match="secret import failed"):
        auth_module.ensure_room_store_keyset(dimension(encryption_domain_id=DOMAIN_ID), backend)
