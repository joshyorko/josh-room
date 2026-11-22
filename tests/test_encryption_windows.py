"""Windows admission must use the native private ACL, never chmod emulation."""
import os
import subprocess
from types import SimpleNamespace

import pytest
from synthetic_identity import synthetic_identity

from josh_room import auth, cli, private_paths


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
    subprocess.run(["icacls", str(identity), "/inheritance:e"], capture_output=True, check=True)
    assert not cli._encryption_material_ready()
