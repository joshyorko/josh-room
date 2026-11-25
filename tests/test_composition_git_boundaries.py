"""Standalone Git manifests are payload, not active repository boundaries."""

import hashlib
import shutil
import subprocess

import pytest

from josh_room.composition import validate_child_git
from josh_room.composition_lifecycle import (
    CompositionAdmissionError,
    _child_view,
    capture_plan,
)
from josh_room.config import DimensionConfig


def test_catalog_git_root_explains_why_review_cannot_open(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "child").mkdir()
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    with pytest.raises(CompositionAdmissionError, match="Save it as one Room") as failure:
        capture_plan(tmp_path, dimension, "1" * 32, "parent",
                     [{"path": "child", "outcome": "child"}], root_files_only=True)
    assert failure.value.result["error_code"] == "composition-root-git-history"
    assert str(tmp_path) not in str(failure.value)


def test_standalone_gitmodules_can_be_planned_and_staged_as_ordinary_content(tmp_path):
    source = tmp_path / "source"
    child = source / "child"
    child.mkdir(parents=True)
    content = '[submodule "example"]\n path = example\n'
    (child / ".gitmodules").write_text(content)
    (child / "payload.txt").write_text("ordinary copied source")
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    preview = capture_plan(source, dimension, "1" * 32, "parent",
                           [{"path": "child", "outcome": "child"}], root_files_only=True)
    target = tmp_path / "staged"
    _child_view(source, "child", target, preview["request"])
    assert (target / ".gitmodules").read_text() == content
    assert (target / "payload.txt").read_text() == "ordinary copied source"


@pytest.mark.parametrize("gitfile", [False, True])
def test_gitmodules_with_active_git_metadata_is_still_refused(tmp_path, gitfile):
    (tmp_path / ".gitmodules").write_text("synthetic manifest")
    if gitfile:
        (tmp_path / ".git").write_text("gitdir: elsewhere")
    else:
        (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="submodules"):
        validate_child_git(tmp_path, [".gitmodules"], "full-repository")


def test_gitfile_without_gitmodules_is_still_refused(tmp_path):
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="gitfiles"):
        validate_child_git(tmp_path, [".git"], "full-repository")


def test_active_submodule_manifest_refusal_has_a_safe_actionable_code(tmp_path):
    if not shutil.which("git"):
        pytest.skip("requires Git for a real repository boundary")
    source = tmp_path / "source"
    child = source / "child"
    child.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    (child / ".gitmodules").write_text("synthetic active repository manifest")
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    with pytest.raises(CompositionAdmissionError, match="submodules") as failure:
        capture_plan(source, dimension, "1" * 32, "parent",
                     [{"path": "child", "outcome": "child"}], root_files_only=True)
    assert failure.value.result["error_code"] == "composition-child-git-unsupported"
    assert str(tmp_path) not in str(failure.value)


def test_active_worktree_config_offers_files_only_for_that_child(tmp_path):
    if not shutil.which("git"):
        pytest.skip("requires Git for a real repository boundary")
    source = tmp_path / "source"
    child = source / "child"
    child.mkdir(parents=True)
    (source / "other").mkdir()
    (child / "payload.txt").write_text("synthetic source content")
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    subprocess.run(["git", "-C", str(child), "config", "extensions.worktreeConfig", "true"], check=True)
    (child / ".git/config.worktree").write_text("[core]\n\tfilemode = true\n")
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    assignments = [{"path": name, "outcome": "child"} for name in ("child", "other")]
    with pytest.raises(CompositionAdmissionError) as failure:
        capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)
    assert failure.value.result == {
        "error_code": "composition-child-git-files-only",
        "child_path_sha256": hashlib.sha256(b"child").hexdigest(),
    }

    assignments[0]["capture_mode"] = "source-only"
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)
    evidence = preview["request"]["child_evidence"]
    assert {name: row["capture_mode"] for name, row in evidence.items()} == {
        "child": "source-only", "other": "full-repository",
    }
    target = tmp_path / "staged"
    _child_view(source, "child", target, preview["request"])
    assert (target / "payload.txt").read_text() == "synthetic source content"
    assert not (target / ".git").exists()


def test_catalog_child_keeps_nested_repo_and_its_own_ignore_file(tmp_path):
    if not shutil.which("git"):
        pytest.skip("requires Git for a nested repository")
    source = tmp_path / "source"
    repo = source / "group" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "payload.txt").write_text("inner project file")
    (repo / ".josh-roomignore").write_text("payload.txt\n")
    dimension = DimensionConfig(
        "synthetic", "Synthetic", "minio", "https://storage.example.invalid",
        "synthetic-bucket", "synthetic-profile", connection_id="synthetic",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    assignments = [{"path": "group", "outcome": "child"}]
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)
    target = tmp_path / "staged"
    _child_view(source, "group", target, preview["request"])
    assert (target / "repo/.git/HEAD").is_file()
    assert (target / "repo/.josh-roomignore").read_bytes() == b"payload.txt\n"
    assert (target / "repo/payload.txt").read_text() == "inner project file"
    (repo / ".josh-room-composition.json").write_text("{}")
    with pytest.raises(CompositionAdmissionError, match="reserved Room metadata"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)
