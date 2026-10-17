"""Strict bulk inventory contracts for the disposable candidate engine."""

import io
import json
from types import SimpleNamespace

import pytest

from josh_room import rustic_store
from josh_room.restic_store import ResticStoreError, SnapshotInfo

SNAPSHOT = SnapshotInfo("a" * 64, "b" * 64, None, "2026-10-17T00:00:00Z", (".",))


def stream():
    return [
        {"struct_type": "snapshot", "schema_version": 1, "id": SNAPSHOT.snapshot_id,
         "tree": SNAPSHOT.tree_id, "paths": ["."]},
        {"struct_type": "node", "path": "folder", "node":
            {"name": "folder", "type": "dir", "mode": (1 << 31) | 0o755, "subtree": "c" * 64}},
        {"struct_type": "node", "path": "folder/file", "node":
            {"name": "file", "type": "file", "mode": (1 << 23) | 0o755, "size": 42}},
        {"struct_type": "node", "path": "shortcut", "node":
            {"name": "shortcut", "type": "symlink", "mode": 0o777, "linktarget": "space and\nnewline"}},
        {"struct_type": "summary", "nodes": 3},
    ]


def encode(values):
    return [json.dumps(v).encode() + b"\n" for v in values]


def test_bulk_stream_preserves_exact_modes_links_and_snapshot_binding():
    rows = rustic_store.parse_node_stream(encode(stream()), SNAPSHOT)
    assert [row.path for row in rows] == ["folder", "folder/file", "shortcut"]
    assert rows[1].mode == (1 << 23) | 0o755
    assert rows[2].link_target == "space and\nnewline"


@pytest.mark.parametrize("case", ["id", "tree", "paths", "schema", "no-header", "duplicate-header",
    "truncated", "count", "extra-after-summary", "duplicate-path", "traversal", "absolute",
    "name-mismatch", "no-parent", "cycle", "unknown-mode", "raw-link", "too-deep"])
def test_bulk_stream_fails_closed(case):
    values = stream()
    if case in {"id", "tree", "paths"}:
        values[0][case] = ["other"] if case == "paths" else "e" * 64
    elif case == "schema": values[0]["schema_version"] = 2
    elif case == "no-header": values.pop(0)
    elif case == "duplicate-header": values.insert(1, values[0])
    elif case == "truncated": values.pop()
    elif case == "count": values[-1]["nodes"] = 4
    elif case == "extra-after-summary": values.append(values[1])
    elif case == "duplicate-path": values.insert(3, values[2])
    elif case == "traversal": values[2]["path"] = "../file"
    elif case == "absolute": values[2]["path"] = "/folder/file"
    elif case == "name-mismatch": values[2]["node"]["name"] = "other"
    elif case == "no-parent": values.pop(1)
    elif case == "cycle": values[1]["node"]["subtree"] = SNAPSHOT.tree_id
    elif case == "unknown-mode": values[2]["node"].pop("mode")
    elif case == "raw-link": values[3]["node"]["linktarget_raw"] = "Yg=="
    elif case == "too-deep": values[2]["path"] = "a/" * 258 + "file"
    with pytest.raises(ResticStoreError):
        rustic_store.parse_node_stream(encode(values), SNAPSHOT)


def test_bulk_stream_bounds_each_event_and_total_entries():
    with pytest.raises(ResticStoreError):
        rustic_store.parse_node_stream(encode(stream()), SNAPSHOT, max_entries=2)
    with pytest.raises(ResticStoreError):
        rustic_store.parse_node_stream(encode(stream()), SNAPSHOT, max_event_bytes=10)
    values = encode(stream())
    values[0] = values[0].replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    with pytest.raises(ResticStoreError):
        rustic_store.parse_node_stream(values, SNAPSHOT)


@pytest.mark.parametrize("failure", ["exit", "warning", "truncated"])
def test_bulk_process_failure_does_not_publish_cache_or_fall_back(tmp_path, failure):
    from josh_room.restic_store import RepositoryInfo

    password = tmp_path / "password"
    password.write_text("synthetic-node-stream")
    password.chmod(0o600)
    calls = []
    values = encode(stream()[:-1] if failure == "truncated" else stream())

    def spawn(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=io.BytesIO(b"".join(values)),
            stderr=io.BytesIO(b"unexpected diagnostic" if failure == "warning" else b""),
            poll=lambda: 1 if failure == "exit" else 0,
            wait=lambda **_: 1 if failure == "exit" else 0)

    with rustic_store.RusticStore(repository=tmp_path / "repo", cache_dir=tmp_path / "cache",
        password_file=password, metadata_format="nodes-v1", process_factory=spawn) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        store._snapshot_info[SNAPSHOT.snapshot_id] = SNAPSHOT
        with pytest.raises(ResticStoreError):
            list(store.entries(SNAPSHOT.snapshot_id, expected_tree_id=SNAPSHOT.tree_id))
        assert not (tmp_path / "cache/rustic-inventory-v2-nodes").exists()
    assert len(calls) == 1 and "--json-nodes" in calls[0]
