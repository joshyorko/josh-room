import os
import shutil

import pytest

from josh_room.restic_store import ResticStore
from josh_room.room_store_operations import _validate_snapshot_entries


@pytest.mark.integration
def test_real_restic_internal_symlink_inventory_and_restore(tmp_path):
    if os.name == "nt" or not shutil.which("restic"):
        pytest.skip("requires Linux symlinks and pinned Restic")
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_text("synthetic payload\n")
    (source / "nested").mkdir()
    (source / "nested" / "link").symlink_to("../data.txt")
    password = tmp_path / "password"
    password.write_text("synthetic-test-password\n")
    password.chmod(0o600)
    with ResticStore(repository=tmp_path / "repo", cache_dir=tmp_path / "cache", password_file=password) as store:
        store.initialize()
        saved = store.backup(source)
        rows = list(store.entries(saved.snapshot_id))
        link = next(row for row in rows if row.entry_type == "symlink")
        assert link.link_target == "../data.txt"
        _validate_snapshot_entries(rows)
        store.restore(saved.snapshot_id, tmp_path / "restored")
        assert (tmp_path / "restored" / "nested" / "link").read_text() == "synthetic payload\n"
