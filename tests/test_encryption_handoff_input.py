"""An explicit existing input must never become an identity output request."""

from types import SimpleNamespace

import pytest
from test_minio_encryption_flow import FakeBackend, dimension, make_keyset

from josh_room import auth, cli, crypto


def private_identity(path):
    return auth._write_encryption_identity(path, make_keyset().operational_identity)


@pytest.fixture
def existing_keyset(tmp_path, monkeypatch):
    keyset = make_keyset()
    backend = FakeBackend(control=keyset.to_json())
    calls = []
    monkeypatch.setattr(auth, "lookup_encryption_identity", lambda *_args: calls.append("lookup") or keyset.operational_identity)
    monkeypatch.setattr(auth, "store_encryption_identity", lambda *_args: calls.append("store"))
    monkeypatch.setattr(crypto, "derive_recipient", lambda _path: keyset.operational_recipient)
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension())
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.delenv("JOSH_ROOM_ENCRYPTION_MATERIAL", raising=False)
    return keyset, backend, calls


@pytest.mark.parametrize("kind", ["missing", "removed-after-keyset-read"])
def test_explicit_cli_input_is_not_regenerated(tmp_path, monkeypatch, existing_keyset, kind):
    _keyset, backend, calls = existing_keyset
    handoff = tmp_path / "handoff"
    if kind == "removed-after-keyset-read":
        private_identity(handoff)
        original = backend.read_control

        def remove_before_admission(*args):
            value = original(*args)
            handoff.unlink()
            return value

        monkeypatch.setattr(backend, "read_control", remove_before_admission)
    monkeypatch.setenv("JOSH_ROOM_ENCRYPTION_MATERIAL", str(handoff))
    with (
        pytest.raises(ValueError, match="handoff"),
        cli._selected_encryption_environment(SimpleNamespace(), tmp_path / "instance"),
    ):
        pass
    assert not handoff.exists()
    assert calls == []
    assert all(call[0] == "read_control" for call in backend.calls)


def test_present_cli_input_remains_caller_owned(tmp_path, monkeypatch, existing_keyset):
    _keyset, _backend, calls = existing_keyset
    handoff = private_identity(tmp_path / "handoff")
    original = handoff.read_bytes()
    monkeypatch.setenv("JOSH_ROOM_ENCRYPTION_MATERIAL", str(handoff))
    with cli._selected_encryption_environment(SimpleNamespace(), tmp_path / "instance") as material:
        assert material.identity == handoff
    assert handoff.read_bytes() == original
    assert calls == []


def test_new_output_request_preserves_generation(tmp_path, existing_keyset):
    _keyset, backend, calls = existing_keyset
    output = tmp_path / "new-output"
    material = auth.resolve_encryption_material(dimension(), backend, identity_path=output)
    assert material.identity == output
    assert output.is_file()
    assert calls == ["lookup", "store"]


def test_existing_input_cannot_initialize_an_empty_backend(tmp_path, existing_keyset):
    keyset, _backend, calls = existing_keyset
    backend = FakeBackend()
    with pytest.raises(auth.EncryptionStateError, match="uninitialized"):
        auth.resolve_encryption_material(
            dimension(), backend, identity_path=private_identity(tmp_path / "handoff"),
            require_existing_identity=True, recovery_recipients=keyset.recovery_recipients,
        )
    assert calls == []
    assert backend.control is None
    assert all(call[0] in {"read_control", "read_catalog"} for call in backend.calls)
