"""Native Windows recovery gates; fixture authority, real pinned encrypted Restic."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from test_recovery import file, synthetic
from test_recovery import test_real_restic_three_pins as real_vertical

from josh_room.recovery import current_fingerprint
from josh_room.room_store_lifecycle import RoomStoreLifecycleError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows acceptance")


def test_native_pinned_restic_recovery(tmp_path):
    # CI must never quietly turn a missing pinned executable into a skip.
    executable = os.environ.get("JOSH_ROOM_TEST_RESTIC")
    assert executable and Path(executable).is_file(), "pinned Windows Restic is required"
    real_vertical(tmp_path)


def test_native_windows_current_compare_and_hardlink_denial(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "配置.txt").write_bytes(b"current synthetic")
    assert current_fingerprint(workspace, "配置.txt") == {
        "type": "file", "size": 17,
        "sha256": hashlib.sha256(b"current synthetic").hexdigest(),
    }
    assert current_fingerprint(workspace, "absent.txt") is None
    os.link(workspace / "配置.txt", workspace / "hardlink.txt")
    with pytest.raises(RoomStoreLifecycleError):
        current_fingerprint(workspace, "hardlink.txt")
    (workspace / "hardlink.txt").unlink()
    (workspace / ".josh-roomignore").write_text("配置.txt\n", encoding="utf-8")
    with pytest.raises(RoomStoreLifecycleError):
        current_fingerprint(workspace, "配置.txt")


def test_native_windows_junction_denial_and_no_clobber(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.txt").write_bytes(b"must remain unchanged")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    junction = workspace / "redirect"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                   check=True, capture_output=True)
    try:
        with pytest.raises(RoomStoreLifecycleError):
            current_fingerprint(workspace, "redirect/payload.txt")
        with pytest.raises(RoomStoreLifecycleError):
            current_fingerprint(junction, "payload.txt")
        node, blobs = file()
        recovery = synthetic([node], blobs)
        with pytest.raises(RoomStoreLifecycleError):
            recovery.extract("old.txt", junction / "absent")
        target = tmp_path / "already-exists"
        target.write_bytes(b"existing")
        with pytest.raises(RoomStoreLifecycleError):
            recovery.extract("old.txt", target)
        assert target.read_bytes() == b"existing"
        import josh_room.recovery as module
        from josh_room.room_store_operations import _rename_directory_noreplace
        def raced(source, destination):
            destination.write_bytes(b"competing actor")
            _rename_directory_noreplace(source, destination)
        monkeypatch.setattr(module, "_rename_directory_noreplace", raced)
        with pytest.raises((RuntimeError, OSError)):
            recovery.extract("old.txt", tmp_path / "race")
        assert (tmp_path / "race").read_bytes() == b"competing actor"
        assert not list(tmp_path.glob(".room-recovery-*"))
        assert (outside / "payload.txt").read_bytes() == b"must remain unchanged"
    finally:
        # Remove only the owned junction, never traverse or remove its target.
        junction.rmdir()


def test_native_windows_current_writer_is_denied(tmp_path):
    current = tmp_path / "file.txt"
    current.write_bytes(b"unchanged")
    with current.open("r+b"), pytest.raises(RoomStoreLifecycleError):
        current_fingerprint(tmp_path, "file.txt")
    assert current.read_bytes() == b"unchanged"
