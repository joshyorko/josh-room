import os

import pytest

from josh_room.workspace_init import (
    STARTER,
    append_exclusions,
    initialize_workspace,
    policy_edit_token,
)
from josh_room.workspace_policy import load_capture_policy


def test_initialize_selected_folder_preserves_existing_and_effective_rules(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    before = load_capture_policy(selected)
    assert initialize_workspace(selected)["state"] == "created"
    ignore = selected / ".josh-roomignore"
    assert ignore.read_bytes() == STARTER
    stamp = ignore.stat().st_mtime_ns
    assert initialize_workspace(selected)["state"] == "already-present"
    assert ignore.stat().st_mtime_ns == stamp
    assert load_capture_policy(selected).patterns == before.patterns
    assert load_capture_policy(selected).sha256 != before.sha256
    assert not (tmp_path / ".josh-roomignore").exists()
    ignore.write_text("# custom\ncustom-cache\n")
    raw = ignore.read_bytes()
    stamp = ignore.stat().st_mtime_ns
    initialize_workspace(selected)
    assert ignore.read_bytes() == raw and ignore.stat().st_mtime_ns == stamp


@pytest.mark.parametrize("kind", ["symlink", "directory", "invalid"])
def test_initialize_rejects_unsafe_existing_file(tmp_path, kind):
    ignore = tmp_path / ".josh-roomignore"
    if kind == "symlink":
        ignore.symlink_to(tmp_path / "other")
    elif kind == "directory":
        ignore.mkdir()
    else:
        ignore.write_text("!reinclude\n")
    with pytest.raises(ValueError):
        initialize_workspace(tmp_path)


def test_append_is_explicit_exact_idempotent_and_detects_edits(tmp_path):
    (tmp_path / "worktree").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "worktree").mkdir()
    initialize_workspace(tmp_path)
    ignore = tmp_path / ".josh-roomignore"
    raw = ignore.read_bytes()
    token = policy_edit_token(tmp_path)
    ignore.write_bytes(raw + b"# concurrent edit\n")
    with pytest.raises(ValueError, match="changed"):
        append_exclusions(tmp_path, ["worktree"], expected_token=token)
    assert ignore.read_bytes() == raw + b"# concurrent edit\n"
    append_exclusions(
        tmp_path, ["worktree"], expected_token=policy_edit_token(tmp_path)
    )
    assert load_capture_policy(tmp_path).is_excluded("worktree/a")
    assert not load_capture_policy(tmp_path).is_excluded("other/worktree/a")
    final = ignore.read_bytes()
    append_exclusions(
        tmp_path, ["worktree"], expected_token=policy_edit_token(tmp_path)
    )
    assert ignore.read_bytes() == final


def test_append_rejects_glob_names_and_source_mode_is_explicit(tmp_path):
    # Rejected before filesystem lookup, including on Windows where '*' is
    # itself an invalid filename character.
    with pytest.raises(ValueError, match="exact exclusion"):
        append_exclusions(
            tmp_path, ["work*tree"], expected_token=policy_edit_token(tmp_path)
        )
    assert not (tmp_path / ".josh-roomignore").exists()
    append_exclusions(
        tmp_path, [], expected_token=policy_edit_token(tmp_path), source_only=True
    )
    assert load_capture_policy(tmp_path).capture_mode == "source-only"


def test_concurrent_creation_reuses_valid_file_without_overwrite(tmp_path, monkeypatch):
    original = os.open

    def open_racing(path, flags, *args, **kwargs):
        if flags & os.O_EXCL:
            (tmp_path / ".josh-roomignore").write_text(
                "# concurrent owner\ncustom-cache\n"
            )
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_racing)
    assert initialize_workspace(tmp_path)["state"] == "already-present"
    assert (
        tmp_path / ".josh-roomignore"
    ).read_text() == "# concurrent owner\ncustom-cache\n"


def test_read_only_initialization_fails_without_file(tmp_path, monkeypatch):
    original = os.open

    def deny_creation(path, flags, *args, **kwargs):
        if flags & os.O_EXCL:
            raise PermissionError("read-only fixture")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_creation)
    with pytest.raises(PermissionError):
        initialize_workspace(tmp_path)
    assert not (tmp_path / ".josh-roomignore").exists()


def test_concurrent_unterminated_append_cannot_fuse_approved_rule(tmp_path, monkeypatch):
    (tmp_path / 'worktree').mkdir()
    initialize_workspace(tmp_path)
    ignore = tmp_path / '.josh-roomignore'
    original_write = os.write
    def racing_write(descriptor, data):
        with ignore.open('ab') as other:
            other.write(b'concurrent-prefix')
        return original_write(descriptor, data)
    monkeypatch.setattr(os, 'write', racing_write)
    with pytest.raises(ValueError, match='changed during append'):
        append_exclusions(tmp_path, ['worktree'], expected_token=policy_edit_token(tmp_path))
    assert b'concurrent-prefix\nworktree/**\n' in ignore.read_bytes()
    assert b'concurrent-prefixworktree' not in ignore.read_bytes()
