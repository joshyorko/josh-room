"""Synthetic pinned-history regression matrix; real Restic when explicitly supplied."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from test_room_store_lifecycle import _context, _descriptor

from josh_room.logical_jat import LogicalJat
from josh_room.recovery import Recovery
from josh_room.restic_store import ResticStore
from josh_room.room_store_lifecycle import RoomStoreLifecycleError


def synthetic(nodes, blobs=None):
    raw = json.dumps({"nodes": nodes}).encode()
    tree = hashlib.sha256(raw).hexdigest()
    blobs = {**(blobs or {}), tree: raw}
    body = _descriptor().to_dict()
    body["workspace"]["tree_id"] = tree
    store = SimpleNamespace(snapshot=lambda _: SimpleNamespace(tree_id=tree, snapshot_id="b" * 64),
                            _capture=lambda args: (0, blobs[args[-1]]))
    return Recovery(_context(descriptor=LogicalJat.from_dict(body), store=store))


def file(name="old.txt", data=b"old\n"):
    digest = hashlib.sha256(data).hexdigest()
    return {"name": name, "type": "file", "size": len(data), "content": [digest]}, {digest: data}


def test_pin_pagination_and_selective_write(tmp_path):
    node, blobs = file()
    recovery = synthetic([node, {"name": "elsewhere", "type": "dir", "subtree": "f" * 64}], blobs)
    before = tmp_path / "current.txt"
    before.write_bytes(b"today")
    assert recovery.tree(limit=1)["next_offset"] == 1
    assert recovery.tree(offset=1, limit=1)["entries"][0]["name"] == "elsewhere"
    receipt = recovery.extract("old.txt", tmp_path / "recovered")
    assert receipt["snapshot_id"] == "jat-source"
    assert receipt["files"] == 1
    assert (tmp_path / "recovered").read_bytes() == b"old\n"
    assert before.read_bytes() == b"today"
    assert receipt["network_bytes_transferred"] is None
    with pytest.raises(RoomStoreLifecycleError):
        recovery.extract("old.txt", tmp_path / "recovered")


@pytest.mark.parametrize("name", ["../escape", "/root", "a/b", "a\\b", "CON", "x:", "x."])
def test_unsafe_names(name):
    with pytest.raises((ValueError, RuntimeError)):
        synthetic([{"name": name}]).tree()


@pytest.mark.parametrize("names", [["A", "a"], ["é", "e\u0301"]])
def test_portable_collisions(names):
    with pytest.raises(RoomStoreLifecycleError):
        synthetic([{"name": name} for name in names]).tree()


@pytest.mark.parametrize("kind", ["symlink", "socket", "fifo"])
def test_special_files_never_materialize(tmp_path, kind):
    recovery = synthetic([{"name": "unsafe", "type": kind}])
    with pytest.raises(RoomStoreLifecycleError):
        recovery.extract("unsafe", tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


def test_cancel_and_corruption_cleanup(tmp_path):
    node, blobs = file()
    recovery = synthetic([node], blobs)
    recovery.cancel = SimpleNamespace(cancelled=True)
    with pytest.raises(RoomStoreLifecycleError):
        recovery.extract("old.txt", tmp_path / "absent")
    recovery.cancel = None
    recovery.store._capture = lambda _: (0, b"corrupted")
    with pytest.raises(RoomStoreLifecycleError):
        recovery.extract("old.txt", tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


def test_wrong_dimension_and_legacy_fail_closed():
    context = _context()
    context.dimension.dimension_id = "wrong"
    with pytest.raises(RoomStoreLifecycleError):
        Recovery(context)
    context = _context()
    context.selected_record = {"snapshot_id": "portable"}
    with pytest.raises(RoomStoreLifecycleError):
        Recovery(context)


@pytest.mark.integration
def test_real_restic_three_pins(tmp_path):
    executable = os.environ.get("JOSH_ROOM_TEST_RESTIC")
    if not executable:
        pytest.skip("set JOSH_ROOM_TEST_RESTIC to pinned 0.19.1 executable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "配置.txt").write_bytes(b"nested synthetic")
    (workspace / "large.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    password = tmp_path / "password"
    password.write_text("synthetic-test-only")
    password.chmod(0o600)
    with ResticStore(repository=tmp_path / "repo", cache_dir=tmp_path / "cache",
                     password_file=password, executable=executable) as store:
        info = store.initialize()
        pins = []
        for data in (b"first\n", b"second\n", b"third\n"):
            (workspace / "changed.txt").write_bytes(data)
            result = store.backup(workspace)
            pins.append(result.snapshot_id)
        body = _descriptor(workspace_snapshot=pins[0]).to_dict()
        body["workspace"]["tree_id"] = store.snapshot(pins[0]).tree_id
        body["workspace"]["repository_id"] = info.repository_id
        context = _context(descriptor=LogicalJat.from_dict(body), store=store, repo_id=info.repository_id)
        recovery = Recovery(context)
        assert recovery.tree(limit=1)["next_offset"] == 1
        result = recovery.extract("changed.txt", tmp_path / "selected")
        assert (tmp_path / "selected").read_bytes() == b"first\n"
        assert (workspace / "changed.txt").read_bytes() == b"third\n"
        recovery.extract("nested", tmp_path / "subtree")
        assert (tmp_path / "subtree" / "配置.txt").read_bytes() == b"nested synthetic"
        assert not (tmp_path / "subtree" / "large.bin").exists()
        print(json.dumps({"blob_bytes_read": result["blob_bytes_read"], "blob_calls": result["blob_calls"],
                          "selected_bytes": 6, "unrelated_bytes": 2 * 1024 * 1024, "pins": len(pins)}))


def test_metadata_comparison_and_current_no_follow(tmp_path):
    node, blobs = file()
    first = synthetic([node], blobs)
    other_node, other_blobs = file(data=b"new\n")
    second = synthetic([other_node], other_blobs)
    assert first.diff("old.txt", other=second)["change"] == "modified"
    assert first.diff("old.txt", other=first)["change"] == "unchanged"
    (tmp_path / "old.txt").write_bytes(b"old\n")
    assert first.diff("old.txt", workspace=tmp_path)["change"] == "unchanged"
    (tmp_path / "old.txt").unlink()
    assert first.diff("old.txt", workspace=tmp_path)["change"] == "removed"
    (tmp_path / "old.txt").symlink_to("missing")
    with pytest.raises(RoomStoreLifecycleError):
        first.diff("old.txt", workspace=tmp_path)


def test_marker_and_late_cancel_leave_no_plaintext(tmp_path):
    marker, blobs = file(name=".josh-room.json")
    with pytest.raises(RoomStoreLifecycleError):
        synthetic([marker], blobs).extract(".josh-room.json", tmp_path / "absent")
    node, blobs = file()
    recovery = synthetic([node], blobs)
    token = SimpleNamespace(cancelled=False)
    recovery.cancel = token
    original = recovery.content
    def cancel_after_read(row):
        data = original(row)
        token.cancelled = True
        return data
    recovery.content = cancel_after_read
    with pytest.raises(RoomStoreLifecycleError):
        recovery.extract("old.txt", tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


def test_promotion_race_preserves_competing_destination(tmp_path, monkeypatch):
    import josh_room.recovery as module
    from josh_room.room_store_operations import _rename_directory_noreplace
    node, blobs = file()
    def raced(source, target):
        target.write_bytes(b"other actor")
        _rename_directory_noreplace(source, target)
    monkeypatch.setattr(module, "_rename_directory_noreplace", raced)
    with pytest.raises(RuntimeError):
        synthetic([node], blobs).extract("old.txt", tmp_path / "absent")
    assert (tmp_path / "absent").read_bytes() == b"other actor"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["absent"]


def test_cli_parser_dispatch_preserves_pin(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from josh_room import cli
    node, blobs = file()
    recovery = synthetic([node], blobs)
    @contextmanager
    def opened(args, instance, **kwargs):
        assert kwargs == {"project_id": "source", "snapshot_id": "jat-source"}
        yield recovery.context
    monkeypatch.setattr(cli, "_open_room_store_context", opened)
    args = cli.build_parser().parse_args(["recovery", "extract", "source", "--dimension", "archive",
                                   "--snapshot", "jat-source", "--path", "old.txt",
                                   "--destination", str(tmp_path / "extracted"), "--json"])
    result = cli.dispatch(args, tmp_path)
    assert result["status"] == "recovered"
    assert (tmp_path / "extracted").read_bytes() == b"old\n"


def test_dispatch_rejects_resolved_alias_and_latest(tmp_path):
    from josh_room.recovery import run_recovery
    node, blobs = file()
    context = synthetic([node], blobs).context
    for snapshot in ("latest", "different-point"):
        with pytest.raises(RoomStoreLifecycleError):
            run_recovery(context, SimpleNamespace(snapshot=snapshot))
