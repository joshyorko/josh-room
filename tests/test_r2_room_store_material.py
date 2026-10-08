import base64
import json
import uuid

import pytest

from josh_room import r2_room_store_material as client

SESSION = "a" * 64
CAPABILITY = "b" * 43
PHYSICAL = "c" * 64
IDENTITY = "synthetic-identity.age"
RECIPIENTS = (
    "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq",
    "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag",
)


class Broker:
    def __init__(self):
        self.record = None
        self.repository_id = None
        self.generation = 0
        self.compete = False
        self.winner = None
        self.ambiguous = False
        self.repository_error = None
        self.requests = []

    def __call__(self, method, session_id, operation, headers, body):
        self.requests.append((method, operation, dict(headers), body))
        assert session_id == SESSION
        assert headers["Authorization"] == f"Bearer {CAPABILITY}"
        assert "capability" not in str(body)
        if operation == "material" and method == "GET":
            return (
                (404, {"error": "room_store_material_missing"})
                if self.record is None
                else (200, {"status": "ready", "material": self.record})
            )
        if operation == "material":
            candidate = json.loads(body)
            if self.compete:
                self.compete = False
                self.record = {**self.winner, "repositoryId": None}
                self.generation = self.record["keysetGeneration"]
                return 409, {"error": "room_store_material_conflict"}
            if self.record is None:
                self.record = {**candidate, "repositoryId": None}
                self.generation = candidate["keysetGeneration"]
                if self.ambiguous:
                    self.ambiguous = False
                    raise OSError("connection lost after commit")
                return 201, {"status": "created", "material": self.record}
            return 409, {"error": "room_store_material_conflict"}
        payload = json.loads(body)
        if self.repository_error:
            return 503, {"error": self.repository_error}
        if self.repository_id:
            if self.repository_id != payload["repositoryId"]:
                return 409, {"error": "room_store_repository_conflict"}
            return 200, {
                "status": "bound",
                "repositoryId": self.repository_id,
                "keysetGeneration": self.generation,
            }
        if payload["expectedGeneration"] != self.generation:
            return 409, {"error": "room_store_generation_conflict"}
        self.repository_id = payload["repositoryId"]
        self.generation += 1
        self.record["repositoryId"] = self.repository_id
        self.record["keysetGeneration"] = self.generation
        return 201, {
            "status": "bound",
            "repositoryId": self.repository_id,
            "keysetGeneration": self.generation,
        }


@pytest.fixture
def authority(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "verify_private_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        client, "protect_private_directory", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(client, "protect_private_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        client.crypto,
        "encrypt",
        lambda data, recipients, output: output.write_bytes(data),
    )
    monkeypatch.setattr(
        client.crypto, "decrypt", lambda path, identities, limit: path.read_bytes()
    )
    monkeypatch.setattr(client.keyring, "store_room_store_secret", lambda *args: None)
    identity = tmp_path / IDENTITY
    identity.write_text("synthetic identity")
    broker = Broker()
    value = client.R2RoomStoreAuthority(
        SESSION, CAPABILITY, PHYSICAL, broker, identity, RECIPIENTS
    )
    return value, broker


def test_create_then_readback_caches_only_encrypted_winner(authority, monkeypatch):
    value, broker = authority
    cached = []
    monkeypatch.setattr(
        client.keyring, "store_room_store_secret", lambda *args: cached.append(args)
    )
    first = value.ensure_material()
    second = value.ensure_material()

    assert first == second
    assert first.physical_binding == PHYSICAL
    assert first.repository_id is None
    assert first.secret not in repr(value)
    assert len(cached) == 2
    record = broker.record
    assert record["repositoryId"] is None
    plaintext = base64.urlsafe_b64decode(record["ciphertext"] + "=")
    inner = json.loads(plaintext)
    assert set(inner) == {
        "format",
        "version",
        "physical_binding",
        "encryption_domain_id",
        "key_generation",
        "secret",
    }
    assert inner["physical_binding"] == PHYSICAL
    assert (
        str(uuid.UUID(inner["encryption_domain_id"])) == inner["encryption_domain_id"]
    )
    assert inner["secret"] == first.secret


def test_details_preserve_the_broker_uuid_domain_separate_from_physical_binding(
    authority,
):
    value, _ = authority
    details = value.ensure_material_details()
    assert details.physical_binding == PHYSICAL
    assert uuid.UUID(details.encryption_domain_id).version == 4
    assert details.encryption_domain_id != details.physical_binding
    assert details.key_generation == 1


def test_read_material_details_is_get_only_and_fails_when_missing(authority):
    value, broker = authority
    with pytest.raises(client.R2RoomStoreError, match="material_missing"):
        value.read_material_details()
    assert [(method, operation) for method, operation, *_ in broker.requests] == [
        ("GET", "material")
    ]


def test_read_material_details_validates_and_caches_existing_winner(authority, monkeypatch):
    value, broker = authority
    candidate = value._new_candidate()
    broker.record = {**candidate, "repositoryId": None}
    cached = []
    monkeypatch.setattr(
        client.keyring, "store_room_store_secret", lambda *args: cached.append(args)
    )

    details = value.read_material_details()

    assert details.physical_binding == PHYSICAL
    assert details.key_generation == 1
    assert len(cached) == 1
    assert [(method, operation) for method, operation, *_ in broker.requests] == [
        ("GET", "material")
    ]


def test_first_writer_conflict_reloads_winner_and_ambiguous_write_reads_back(authority):
    value, broker = authority
    broker.compete = True
    broker.winner = value._new_candidate()
    result = value.ensure_material()
    assert (
        result.secret
        == json.loads(base64.urlsafe_b64decode(broker.record["ciphertext"] + "="))[
            "secret"
        ]
    )

    value2, broker2 = authority
    broker2.ambiguous = True
    assert value2.ensure_material().secret


def test_repository_bind_advances_metadata_without_changing_ciphertext(authority):
    value, broker = authority
    initial = value.ensure_material()
    ciphertext = broker.record["ciphertext"]
    bound = value.bind_repository("d" * 64, expected_generation=initial.generation)
    assert bound.generation == initial.generation + 1
    assert bound.repository_id == "d" * 64
    assert broker.record["ciphertext"] == ciphertext
    assert (
        value.bind_repository(
            "d" * 64, expected_generation=initial.generation
        ).generation
        == bound.generation
    )
    method, operation, headers, body = next(
        request for request in broker.requests if request[1] == "repository"
    )
    assert (method, operation) == ("POST", "repository")
    assert json.loads(body) == {
        "repositoryId": "d" * 64,
        "expectedGeneration": bound.generation - 1,
    }
    assert headers["Authorization"] == f"Bearer {CAPABILITY}"


def test_repository_bind_rejects_conflict_and_unverified_write(authority, tmp_path):
    value, broker = authority
    initial = value.ensure_material()
    bound = value.bind_repository("d" * 64, initial.generation)
    with pytest.raises(client.R2RoomStoreError, match="repository_conflict"):
        value.bind_repository("e" * 64, initial.generation)
    assert broker.record["repositoryId"] == "d" * 64
    assert broker.record["keysetGeneration"] == bound.generation

    broker2 = Broker()
    identity = tmp_path / IDENTITY
    identity.write_text("synthetic identity")
    value2 = client.R2RoomStoreAuthority(
        SESSION, CAPABILITY, PHYSICAL, broker2, identity, RECIPIENTS
    )
    broker2.repository_error = "room_store_repository_write_unverified"
    with pytest.raises(client.R2RoomStoreError, match="repository_write_unverified"):
        value2.bind_repository("f" * 64)
    assert broker2.record["repositoryId"] is None


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (
            lambda a: object.__setattr__(a, "physical_binding", "e" * 64),
            "physical_binding_mismatch",
        ),
    ],
)
def test_rejects_wrong_physical_binding(authority, mutate, expected):
    value, broker = authority
    candidate = value._new_candidate()
    broker.record = {**candidate, "repositoryId": None}
    mutate(value)
    with pytest.raises(client.R2RoomStoreError, match=expected):
        value.ensure_material()


def test_secret_capability_is_not_in_repr_or_url(authority):
    value, _ = authority
    assert CAPABILITY not in repr(value)
    assert CAPABILITY not in value._url("material")


def test_r2_still_requires_native_secret_custody(authority, monkeypatch):
    value, _broker = authority
    monkeypatch.setattr(client.keyring, "store_room_store_secret", lambda *_args: (_ for _ in ()).throw(
        client.keyring.NativeSecretBackendUnavailable("unavailable")
    ))
    with pytest.raises(client.R2RoomStoreError, match="room_store_native_keyring_unavailable"):
        value.ensure_material()
