"""Actual candidate engine tests; executable and its build digest are explicit."""

import hashlib
import os
from pathlib import Path

import pytest

from josh_room.private_paths import protect_private_file
from josh_room.restic_store import ResticStoreError
from josh_room.rustic_store import RusticStore


@pytest.fixture
def candidate():
    value = os.environ.get("JOSH_ROOM_RUSTIC_CANDIDATE_BINARY")
    expected = os.environ.get("JOSH_ROOM_RUSTIC_CANDIDATE_SHA256")
    if not value or not expected:
        pytest.skip("explicit disposable candidate and build digest are required")
    path = Path(value).resolve(strict=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    return str(path)


def authority(tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-candidate-contract")
    protect_private_file(password)
    return {"repository": tmp_path / "repo", "password_file": password,
            "cache_dir": tmp_path / "cache"}


def test_candidate_stream_matches_authenticated_blobs_and_restore(candidate, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "folder").mkdir(parents=True)
    file = workspace / "folder/readonly"
    file.write_bytes(b"synthetic readonly bytes")
    file.chmod(0o444)
    if os.name == "posix":
        (workspace / "shortcut").symlink_to("folder/readonly\nnonexistent")
        special = workspace / "setuid-file"
        special.write_bytes(b"synthetic mode bits")
        special.chmod(0o4755)
    kwargs = authority(tmp_path)
    try:
        with RusticStore(**kwargs, executable=candidate) as legacy:
            legacy.initialize()
            summary = legacy.backup(workspace)
            exact = {row.path: row for row in legacy.entries(summary.snapshot_id)}
        with RusticStore(**kwargs, executable=candidate, metadata_format="nodes-v1") as bulk:
            bulk.open_existing()
            snapshot = bulk.snapshot(summary.snapshot_id)
            observed = {row.path: row for row in bulk.entries(summary.snapshot_id, expected_tree_id=snapshot.tree_id)}
            assert observed == exact
            destination = tmp_path / "relocated"
            bulk.restore(summary.snapshot_id, destination)
            restored = destination / "folder/readonly"
            assert restored.read_bytes() == file.read_bytes()
            assert restored.stat().st_mode & 0o777 == file.stat().st_mode & 0o777
            if os.name == "posix":
                assert os.readlink(destination / "shortcut") == "folder/readonly\nnonexistent"
                assert (destination / "setuid-file").stat().st_mode & 0o7777 == 0o4755
            bulk.check(read_data=True)
    finally:
        file.chmod(0o600)
        restored = tmp_path / "relocated/folder/readonly"
        if restored.exists(): restored.chmod(0o600)


def test_candidate_wide_tree_stream_retains_bounds_without_aggregate_capture(candidate, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(15000):
        (workspace / f"synthetic-{index:05d}.txt").write_bytes(b"x")
    kwargs = authority(tmp_path)
    with RusticStore(**kwargs, executable=candidate) as legacy:
        legacy.initialize()
        summary = legacy.backup(workspace)
        with pytest.raises(ResticStoreError) as error:
            list(legacy.entries(summary.snapshot_id))
        assert error.value.code.value == "invalid-output"
    with RusticStore(**kwargs, executable=candidate, metadata_format="nodes-v1") as bulk:
        bulk.open_existing()
        rows = list(bulk.entries(summary.snapshot_id))
        assert len(rows) == 15000 and all(row.size == 1 for row in rows)
        destination = tmp_path / "wide-relocated"
        bulk.restore(summary.snapshot_id, destination)
        assert len(list(destination.iterdir())) == 15000
        assert all(path.read_bytes() == b"x" for path in destination.iterdir())
        bulk.check(read_data=True)
