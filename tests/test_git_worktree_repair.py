from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from josh_room.git_worktree_repair import WorktreeRepairError, inspect_workspace, repair_workspace


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "fixture").write_text("fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"], check=True, capture_output=True)
    worktree = tmp_path / "external-worktree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "repair-test", str(worktree)], check=True, capture_output=True)
    return repo, worktree


def test_repair_is_exact_and_repeatable(tmp_path: Path) -> None:
    _repo_path, worktree = _repo(tmp_path)
    shutil.rmtree(worktree)
    proposal = inspect_workspace(tmp_path)
    assert len(proposal) == 1
    assert proposal[0].worktree_path == str(worktree)
    assert repair_workspace(tmp_path, proposal) == proposal
    assert inspect_workspace(tmp_path) == ()
    assert repair_workspace(tmp_path, ()) == ()


def test_locked_worktree_is_not_proposed(tmp_path: Path) -> None:
    repo, worktree = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(worktree), "--reason", "fixture"], check=True, capture_output=True)
    shutil.rmtree(worktree)
    assert inspect_workspace(tmp_path) == ()


def test_state_change_requires_refresh(tmp_path: Path) -> None:
    _repo_path, worktree = _repo(tmp_path)
    shutil.rmtree(worktree)
    proposal = inspect_workspace(tmp_path)
    (tmp_path / "state-change").write_text("changed\n")
    with pytest.raises(WorktreeRepairError, match="proposal changed"):
        repair_workspace(tmp_path, proposal + (proposal[0],))
