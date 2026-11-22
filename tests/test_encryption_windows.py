"""Windows admission must use the native private ACL, never chmod emulation."""
import os
import subprocess
from types import SimpleNamespace

import pytest
from synthetic_identity import synthetic_identity

from josh_room import auth, cli, private_paths


def handoff_keyset():
    return SimpleNamespace(encryption_domain_id="00000000-0000-4000-8000-000000000001", key_generation=1,
                           operational_recipient="synthetic-recipient")


@pytest.fixture(autouse=True)
def acl_probe_recipient(monkeypatch):
    # These tests isolate real ACL admission; the installed MinIO/age job proves
    # actual key/recipient derivation separately, including explicit handoffs.
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: "synthetic-recipient")


def test_windows_existing_handoff_uses_native_acl_and_never_falls_back(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    identity.write_text(synthetic_identity("existing-windows"))
    identity.chmod(0o644)
    monkeypatch.setattr(auth, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(auth, "lookup_encryption_identity", lambda *_args: pytest.fail("unsafe handoff must not fall back"))
    checked = []
    monkeypatch.setattr(auth, "verify_private_path", lambda path, **kwargs: checked.append((path, kwargs)))
    assert auth._material_for_keyset(handoff_keyset(), identity).identity == identity
    assert checked == [(identity, {"directory": False})]
    for failure in (private_paths.PrivatePathError("unsafe ACL"), OSError("API unavailable")):
        def deny(*_args, failure=failure, **_kwargs):
            raise failure
        monkeypatch.setattr(auth, "verify_private_path", deny)
        with pytest.raises((private_paths.PrivatePathError, OSError)):
            auth._material_for_keyset(handoff_keyset(), identity)


def test_windows_handoff_retains_size_content_and_recipient_binding(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    monkeypatch.setattr(auth, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(auth, "verify_private_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(auth, "lookup_encryption_identity", lambda *_args: pytest.fail("invalid handoff must not fall back"))
    identity.write_text("x" * (16 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        auth._material_for_keyset(handoff_keyset(), identity)
    identity.write_text("synthetic invalid identity")
    with pytest.raises(ValueError):
        auth._material_for_keyset(handoff_keyset(), identity)
    identity.write_text(synthetic_identity("binding-identity"))
    selected = handoff_keyset()
    selected.operational_recipient = "another-synthetic-recipient"
    with pytest.raises(ValueError, match="does not match"):
        auth._material_for_keyset(selected, identity)


@pytest.mark.skipif(os.name == "nt", reason="POSIX identity modes")
@pytest.mark.parametrize(("mode", "accepted"), [(0o600, True), (0o400, True), (0o700, True), (0o640, False), (0o644, False)])
def test_posix_handoff_and_readiness_policy_preserved(tmp_path, monkeypatch, mode, accepted):
    identity = tmp_path / "identity"
    identity.write_text(synthetic_identity("posix-handoff"))
    identity.chmod(mode)
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identity))
    monkeypatch.setattr(cli, "_recipients", lambda: ["synthetic-a", "synthetic-b"])
    assert cli._encryption_material_ready() is accepted
    if accepted:
        assert auth._material_for_keyset(handoff_keyset(), identity).identity == identity
    else:
        with pytest.raises(ValueError, match="unsafe"):
            auth._material_for_keyset(handoff_keyset(), identity)


def test_windows_readiness_uses_acl_instead_of_posix_emulated_bits(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    identity.write_text(synthetic_identity("windows-readiness"))
    identity.chmod(0o644)
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identity))
    monkeypatch.setattr(cli, "_recipients", lambda: ["synthetic-a", "synthetic-b"])
    monkeypatch.setattr(cli, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    verified = []

    def verify(path, *, directory):
        assert path == identity and directory is False
        verified.append(path)

    monkeypatch.setattr(private_paths, "verify_private_path", verify)
    assert cli._encryption_material_ready()
    assert verified == [identity]
    monkeypatch.setattr(private_paths, "verify_private_path",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(private_paths.PrivatePathError("unsafe ACL")))
    assert not cli._encryption_material_ready()


def test_windows_identity_is_protected_before_writing_secret(tmp_path, monkeypatch):
    destination = tmp_path / "identity"
    value = synthetic_identity("windows-write")
    monkeypatch.setattr(auth, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    protected = []

    def protect(path):
        assert path.read_bytes() == b""
        protected.append(path)

    monkeypatch.setattr(auth, "protect_private_file", protect)
    auth._write_encryption_identity(destination, value)
    assert protected and destination.read_text() == value + "\n"


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL admission")
def test_native_windows_identity_acl_ready_then_inheritance_denied(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    auth._write_encryption_identity(identity, synthetic_identity("native-windows"))
    private_paths.verify_private_path(identity, directory=False)
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identity))
    monkeypatch.setattr(cli, "_recipients", lambda: ["synthetic-a", "synthetic-b"])
    assert cli._encryption_material_ready()
    assert auth._material_for_keyset(handoff_keyset(), identity).identity == identity
    subprocess.run(["icacls", str(identity), "/inheritance:e"], capture_output=True, check=True)
    assert not cli._encryption_material_ready()
    with pytest.raises(private_paths.PrivatePathError):
        auth._material_for_keyset(handoff_keyset(), identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows foreign ACL")
def test_native_windows_foreign_ace_denies_handoff_and_readiness(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    auth._write_encryption_identity(identity, synthetic_identity("foreign-ace"))
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identity))
    monkeypatch.setattr(cli, "_recipients", lambda: ["synthetic-a", "synthetic-b"])
    subprocess.run(["icacls", str(identity), "/grant", "*S-1-1-0:(R)"], capture_output=True, check=True)
    assert not cli._encryption_material_ready()
    with pytest.raises(private_paths.PrivatePathError):
        auth._material_for_keyset(handoff_keyset(), identity)


@pytest.mark.skipif(os.name != "nt", reason="native Windows file reparse point")
def test_native_windows_file_reparse_handoff_denied(tmp_path, monkeypatch):
    identity = tmp_path / "identity"
    auth._write_encryption_identity(identity, synthetic_identity("reparse-identity"))
    link = tmp_path / "redirect"
    try:
        link.symlink_to(identity)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("native file symlink privilege unavailable; reparse case unproven")
        raise
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(link))
    monkeypatch.setattr(cli, "_recipients", lambda: ["synthetic-a", "synthetic-b"])
    assert not cli._encryption_material_ready()
    with pytest.raises(ValueError, match="unsafe"):
        auth._material_for_keyset(handoff_keyset(), link)
