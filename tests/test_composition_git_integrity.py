import os
import subprocess

import pytest

from josh_room.composition import validate_child_git
from josh_room.composition_git_integrity import verify_staged_git
from josh_room.composition_lifecycle import _child_view, capture_plan
from josh_room.config import DimensionConfig


def repository(tmp_path):
    root = tmp_path / "child"
    subprocess.run(["git", "init", "-q", root], check=True)
    (root / "file").write_text("committed")
    subprocess.run(["git", "-C", root, "add", "file"], check=True)
    subprocess.run(["git", "-C", root, "-c", "user.name=Fixture", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True)
    return root


def inventory(root):
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_catalog_preserves_git_history_with_inactive_worktree_config(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    root = repository(source)
    subprocess.run(["git", "-C", root, "config", "extensions.worktreeConfig", "true"], check=True)
    head = subprocess.check_output(["git", "-C", root, "rev-parse", "HEAD"])
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    stage = tmp_path / "staged"
    preview = capture_plan(source, dimension, "1" * 32, "parent",
                           [{"path": "child", "outcome": "child"}], root_files_only=True)
    assert preview["request"]["child_evidence"]["child"]["capture_mode"] == "full-repository"
    _child_view(source, "child", stage, preview["request"])
    verify_staged_git(stage, inventory(stage), "full-repository", tmp_path)
    assert subprocess.check_output(["git", "-C", stage, "rev-parse", "HEAD"]) == head
    assert subprocess.check_output(["git", "-C", stage, "show", "HEAD:file"]) == b"committed"
    assert (stage / ".git/config").read_bytes() == (root / ".git/config").read_bytes()


@pytest.mark.parametrize("ref", ["HEAD", ":file"])
def test_private_verification_refuses_missing_commit_or_staged_blob(tmp_path, ref):
    root = repository(tmp_path)
    (root / "file").write_text("staged")
    subprocess.run(["git", "-C", root, "add", "file"], check=True)
    oid = subprocess.check_output(["git", "-C", root, "rev-parse", ref], text=True).strip()
    verify_staged_git(root, inventory(root), "full-repository", tmp_path)
    (root / ".git/objects" / oid[:2] / oid[2:]).unlink()
    with pytest.raises(ValueError, match="integrity"):
        verify_staged_git(root, inventory(root), "full-repository", tmp_path)


@pytest.mark.parametrize("partial", ["promisor", "config", "shallow"])
def test_incomplete_git_forms_refused_during_admission(tmp_path, partial):
    root = repository(tmp_path)
    if partial == "config":
        subprocess.run(["git", "-C", root, "config", "extensions.partialclone", "origin"], check=True)
    else:
        (root / ".git" / ("shallow" if partial == "shallow" else "objects/pack/objects.promisor")).touch()
    with pytest.raises(ValueError, match="partial|shallow"):
        validate_child_git(root, inventory(root), "full-repository")


def test_verifier_ignores_inherited_git_storage_and_local_fsck_config(tmp_path, monkeypatch):
    root = repository(tmp_path)
    subprocess.run(["git", "-C", root, "config", "fsck.skipList", "/absent"], check=True)
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", os.fspath(tmp_path / "absent"))
    verify_staged_git(root, inventory(root), "full-repository", tmp_path)


def test_verifies_packed_objects_refs_and_reflog_roots(tmp_path):
    root = repository(tmp_path)
    subprocess.run(["git", "-C", root, "gc", "-q"], check=True)
    verify_staged_git(root, inventory(root), "full-repository", tmp_path)
    blob = subprocess.check_output(["git", "-C", root, "hash-object", "-w", "--stdin"],
                                   input=b"reflog only").decode().strip()
    subprocess.run(["git", "-C", root, "update-ref", "--create-reflog", "refs/test/proof", blob], check=True)
    subprocess.run(["git", "-C", root, "update-ref", "refs/test/proof", "HEAD"], check=True)
    (root / ".git/objects" / blob[:2] / blob[2:]).unlink()
    with pytest.raises(ValueError, match="integrity"):
        verify_staged_git(root, inventory(root), "full-repository", tmp_path)
