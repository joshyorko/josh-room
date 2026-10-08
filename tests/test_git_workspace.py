from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from josh_room.git_workspace import GitWorkspaceError, validate_git_storage
from josh_room.room_store_operations import RoomStoreOperationsError, _scan_workspace
from josh_room.workspace_policy import load_capture_policy
from scripts.git_recovery_acceptance import (
    contents,
    exercise,
    git,
    operations,
    repository,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires real Git")


@pytest.mark.skipif(shutil.which("restic") is None, reason="requires real Restic")
def test_real_nested_git_save_noop_metadata_change_and_enter(tmp_path):
    result = exercise(tmp_path)
    assert result["git_repositories"] == 4
    assert result["source_unchanged"] == "passed"
    assert result["noop_added_bytes"] == 0


def test_changed_git_metadata_changes_stat_scan_and_content_fingerprint(tmp_path):
    from josh_room.workspace_state import workspace_fingerprint

    repository(tmp_path / "repo")
    policy = load_capture_policy(tmp_path)
    before = _scan_workspace(tmp_path, policy)
    fingerprint = workspace_fingerprint(tmp_path)
    git(tmp_path / "repo", "branch", "metadata-only")
    assert _scan_workspace(tmp_path, policy).signature != before.signature
    assert workspace_fingerprint(tmp_path) != fingerprint


@pytest.mark.parametrize("pointer", ["/outside/private", "C:/outside/private", "../outside", "..\\outside", "", "gitdir: wrong"])
def test_unsafe_gitfiles_fail_closed_without_modification(tmp_path, pointer):
    (tmp_path / ".git").write_text("gitdir: " + pointer + "\n")
    before = contents(tmp_path)
    with pytest.raises(RoomStoreOperationsError, match="Git storage"):
        _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    assert contents(tmp_path) == before


def test_external_linked_worktree_fails_before_opening_store(tmp_path):
    repository(tmp_path / "origin")
    workspace = tmp_path / "linked"
    git(tmp_path / "origin", "worktree", "add", "-b", "linked", str(workspace))
    before = contents(workspace)
    op = operations(tmp_path, workspace)
    op.ensure_keyset = lambda *_args: pytest.fail("unsafe Git storage must fail before provider access")
    with pytest.raises(RoomStoreOperationsError, match="linked worktrees"):
        op.save()
    assert contents(workspace) == before


@pytest.mark.parametrize("filename, content", [
    ("objects/info/alternates", "../../borrowed\n"),
    ("objects/info/http-alternates", "https://example.invalid/objects\n"),
    ("commondir", "/external/git-storage\n"),
    ("gitdir", "/external/worktree/.git\n"),
    ("config", '[include]\npath = /external/config\n'),
    ("config", '[core]\nworktree = /external/worktree\n'),
])
def test_external_dependencies_are_refused(tmp_path, filename, content):
    repository(tmp_path / "repo")
    path = tmp_path / "repo" / ".git" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    with pytest.raises(RoomStoreOperationsError, match="Git"):
        _scan_workspace(tmp_path, load_capture_policy(tmp_path))


@pytest.mark.parametrize("config", [
    '[remote "origin"]\nurl = https://synthetic-token@example.invalid/repo.git\n',
    '[http]\nextraHeader = Authorization: Bearer synthetic-secret\n',
    '[credential]\nhelper = store\n',
    '[remote "origin"]\nurl = https://synthetic-token@example.invalid/repo.git\nurl = https://example.invalid/safe.git\n',
    '[url "https://synthetic-token@example.invalid/"]\ninsteadOf = https://example.invalid/\n',
])
def test_credentials_survive_encrypted_capture_but_plaintext_export_is_refused(tmp_path, config):
    repository(tmp_path / "repo")
    path = tmp_path / "repo/.git/config"
    path.write_text(config)
    before = contents(tmp_path)
    scan = _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    with pytest.raises(GitWorkspaceError, match="plaintext") as error:
        validate_git_storage(tmp_path, scan.paths, portable_export=True)
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-token" not in str(error.value)
    assert contents(tmp_path) == before


def test_git_policy_identity_invalidates_old_noop_baseline(tmp_path):
    defaults = Path("src/josh_room/workspace_capture_defaults.json").read_bytes()
    old = defaults.replace(b'    "**/.pytest_cache",', b'    "**/.git",\n    "**/.pytest_cache",')
    old_digest = hashlib.sha256(old + b"\0ignore-absent\0\0active-runtime\0").hexdigest()
    assert load_capture_policy(tmp_path).sha256 != old_digest


def test_runtime_inside_git_storage_cannot_exclude_git_metadata(tmp_path):
    repository(tmp_path / "repo")
    policy = load_capture_policy(tmp_path, active_runtime_root=tmp_path / "repo/.git/objects")
    assert not policy.is_excluded("repo/.git/objects")
    assert not policy.is_excluded("repo/.git/objects/aa/object")


@pytest.mark.parametrize("unsafe", ["alternates", "credentials"])
def test_nested_bare_git_storage_cannot_bypass_validation(tmp_path, unsafe):
    bare = tmp_path / "vendor/objects.git"
    bare.mkdir(parents=True)
    git(bare, "init", "--bare")
    if unsafe == "alternates":
        (bare / "objects/info/alternates").write_text("/outside/borrowed\n")
        with pytest.raises(RoomStoreOperationsError, match="alternates"):
            _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    else:
        git(bare, "config", "remote.origin.url", "https://synthetic-token@example.invalid/repo.git")
        scan = _scan_workspace(tmp_path, load_capture_policy(tmp_path))
        with pytest.raises(GitWorkspaceError, match="plaintext"):
            validate_git_storage(tmp_path, scan.paths, portable_export=True)


def test_ignore_cannot_hide_part_of_bare_repository(tmp_path):
    bare = tmp_path / "vendor/bare-repository"
    bare.mkdir(parents=True)
    git(bare, "init", "--bare")
    (tmp_path / ".josh-roomignore").write_text("**/HEAD\n")
    with pytest.raises(RoomStoreOperationsError, match="Bare Git metadata"):
        _scan_workspace(tmp_path, load_capture_policy(tmp_path))


def test_submodule_credentials_refuse_plaintext_export(tmp_path):
    repository(tmp_path / "repo")
    (tmp_path / "repo/.gitmodules").write_text('[submodule "library"]\nurl = https://synthetic-token@example.invalid/library.git\n')
    scan = _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    with pytest.raises(GitWorkspaceError, match="plaintext"):
        validate_git_storage(tmp_path, scan.paths, portable_export=True)


def test_exact_restic_exclusions_are_absolute_and_protect_git(tmp_path):
    (tmp_path / "services/one/node_modules").mkdir(parents=True)
    (tmp_path / "other/services/one/node_modules").mkdir(parents=True)
    (tmp_path / ".git/refs/heads/node_modules").mkdir(parents=True)
    (tmp_path / ".josh-roomignore").write_text("services/one/node_modules\n")
    policy = load_capture_policy(tmp_path)
    excluded = policy.resolved_restic_excludes(tmp_path)
    assert str(tmp_path / "services/one/node_modules") in excluded
    assert not any(".git" in path for path in excluded)


def test_restore_refuses_broken_gitfile_before_destination_promotion(tmp_path):
    from test_room_store_operations import (
        REPOSITORY_ID,
        _Catalog,
        _descriptor,
        _operations,
        _Store,
    )

    from josh_room.restic_store import SnapshotEntry

    source = tmp_path / "source"
    source.mkdir()
    store = _Store(entries=[SnapshotEntry(".", "dir", 0, 0o700, None),
                            SnapshotEntry(".git", "file", 1, 0o600, None)])

    def restore(_snapshot, destination):
        destination.mkdir()
        (destination / ".git").write_text("gitdir: ../missing\n")

    store.restore = restore
    op = _operations(tmp_path, source, store, _Catalog(), binding=REPOSITORY_ID)
    with pytest.raises(RoomStoreOperationsError, match="Git storage"):
        op.restore(_descriptor(), tmp_path / "destination", write_restore_marker=lambda *_args: None)
    assert not (tmp_path / "destination").exists()
