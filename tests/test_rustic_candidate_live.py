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
    saved_mtime = 1_700_000_000_123456700
    os.utime(file, ns=(saved_mtime, saved_mtime))
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
            assert restored.stat().st_mtime_ns == saved_mtime
            assert restored.stat().st_mode & 0o777 == file.stat().st_mode & 0o777
            if os.name == "posix":
                assert os.readlink(destination / "shortcut") == "folder/readonly\nnonexistent"
                assert (destination / "setuid-file").stat().st_mode & 0o7777 == 0o4755
            # A later writable snapshot must remain writable on fresh restore.
            file.chmod(0o666)
            file.write_bytes(b"synthetic writable replacement")
            changed = bulk.backup(workspace, parent=summary.snapshot_id)
            writable = tmp_path / "writable-relocated"
            bulk.restore(changed.snapshot_id, writable)
            assert (writable / "folder/readonly").read_bytes() == file.read_bytes()
            assert (writable / "folder/readonly").stat().st_mode & 0o777 == file.stat().st_mode & 0o777
            bulk.check(read_data=True)
    finally:
        file.chmod(0o600)
        restored = tmp_path / "relocated/folder/readonly"
        if restored.exists(): restored.chmod(0o600)


def test_candidate_wide_tree_stream_retains_bounds_without_aggregate_capture(candidate, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for index in range(15000):
        (workspace / (f"synthetic-{index:05d}-" + "x" * 96 + ".txt")).write_bytes(b"x")
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


@pytest.mark.parametrize('position', ['root', 'subtree'])
def test_candidate_rejects_valid_encrypted_tree_replay(candidate, tmp_path, position):
    """Replay authentic same-key ciphertext at an indexed tree's old location."""
    import json

    workspace = tmp_path / 'workspace'
    folder = workspace / 'folder'
    folder.mkdir(parents=True)
    file = folder / 'entry-000'
    file.write_bytes(b'')
    kwargs = authority(tmp_path)
    snapshots = []
    with RusticStore(**kwargs, executable=candidate) as store:
        store.initialize()
        for index in range(32):
            renamed = folder / f'entry-{index:03d}'
            if renamed != file: file.rename(renamed)
            file = renamed
            summary = store.backup(workspace)
            snapshot = store.snapshot(summary.snapshot_id)
            tree_id = snapshot.tree_id
            if position == 'subtree':
                raw = store._tree_blob(tree_id)
                tree_id = json.loads(raw)['nodes'][0]['subtree']
            snapshots.append((summary.snapshot_id, tree_id))
        locations = {}
        for index_file in (tmp_path / 'repo/index').iterdir():
            _, raw = store._capture(['cat', 'index', index_file.name])
            for pack in json.loads(raw)['packs']:
                for blob in pack['blobs']:
                    if blob['type'] == 'tree': locations[blob['id']] = (pack['id'], blob)
    groups = {}
    pair = None
    for snapshot_id, tree_id in snapshots:
        pack, blob = locations[tree_id]
        shape = (blob['length'], blob.get('uncompressed_length'))
        if shape in groups and groups[shape][1] != tree_id:
            pair = (groups[shape], (snapshot_id, tree_id, pack, blob)); break
        groups[shape] = (snapshot_id, tree_id, pack, blob)
    assert pair is not None, 'owned replay corpus did not produce equal-size tree records'
    (snapshot_id, expected_tree, target_pack, target), (_, replay_tree, replay_pack, replay) = pair
    assert expected_tree != replay_tree
    pack_root = tmp_path / 'repo/data'
    target_path = pack_root / target_pack[:2] / target_pack
    replay_path = pack_root / replay_pack[:2] / replay_pack
    original = target_path.read_bytes()
    ciphertext = replay_path.read_bytes()[replay['offset']:replay['offset'] + replay['length']]
    assert len(ciphertext) == target['length']
    corrupted = original[:target['offset']] + ciphertext + original[target['offset'] + target['length']:]
    target_path.write_bytes(corrupted)
    try:
        kwargs['cache_dir'] = tmp_path / 'fresh-replay-cache'
        with RusticStore(**kwargs, executable=candidate, metadata_format='nodes-v1') as store:
            store.open_existing()
            with pytest.raises(ResticStoreError): list(store.entries(snapshot_id))
        assert not (kwargs['cache_dir'] / 'rustic-inventory-v2-nodes').exists()
    finally:
        target_path.write_bytes(original)
