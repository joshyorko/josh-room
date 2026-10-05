from __future__ import annotations

from josh_room import parent_inventory
from josh_room.restic_store import SnapshotEntry


def test_inventory_only_reuses_exact_repository_snapshot_tree(tmp_path):
    rows = [SnapshotEntry("file.txt", "file", 7, 0o600, None)]
    args = (tmp_path, "https://synthetic.invalid/repo", "a" * 64, "b" * 64, "c" * 64)
    parent_inventory.write_inventory(*args, rows)
    assert parent_inventory.read_inventory(*args) == rows
    assert parent_inventory.read_inventory(*args[:-1], "d" * 64) is None
    assert (
        parent_inventory.read_inventory(
            tmp_path, "https://other.invalid/repo", *args[2:]
        )
        is None
    )


def test_corrupt_insecure_and_symlink_inventory_are_cache_misses(tmp_path):
    rows = [SnapshotEntry("file.txt", "file", 7, 0o600, None)]
    args = (tmp_path, "https://synthetic.invalid/repo", "a" * 64, "b" * 64, "c" * 64)
    parent_inventory.write_inventory(*args, rows)
    path = next((tmp_path / "parent-inventories").glob("*.json"))
    original = path.read_bytes()
    path.write_bytes(original.replace(b"file.txt", b"fake.txt"))
    assert parent_inventory.read_inventory(*args) is None
    path.write_bytes(original)
    path.chmod(0o666)
    assert parent_inventory.read_inventory(*args) is None
    path.chmod(0o600)
    other = tmp_path / "other.json"
    path.rename(other)
    path.symlink_to(other)
    assert parent_inventory.read_inventory(*args) is None
