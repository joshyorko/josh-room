from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import josh_room.git_worktree_repair as repair_module
from josh_room.git_worktree_repair import (
    StaleWorktreeRecord,
    WorktreeRepairError,
    inspect_workspace,
    proposal_digest,
    repair_workspace,
)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, timeout=30)
    (repo / "fixture").write_text("fixture\n")
    subprocess.run(["git", "-C", str(repo), "add", "fixture"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture"], check=True, capture_output=True, timeout=30)
    worktree = tmp_path / "external-worktree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", "repair-test", str(worktree)], check=True, capture_output=True, timeout=30)
    return repo, worktree

def test_repair_is_exact_and_repeatable(tmp_path: Path) -> None:
    _repo_path, worktree = _repo(tmp_path)
    shutil.rmtree(worktree)
    proposal = inspect_workspace(tmp_path)
    assert len(proposal) == 1
    assert proposal[0].worktree_path == str(worktree)
    assert repair_workspace(tmp_path, proposal_digest(proposal)) == proposal
    assert inspect_workspace(tmp_path) == ()
    assert repair_workspace(tmp_path, proposal_digest(())) == ()

def test_locked_worktree_is_not_proposed(tmp_path: Path) -> None:
    repo, worktree = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(worktree), "--reason", "fixture"], check=True, capture_output=True, timeout=30)
    shutil.rmtree(worktree)
    assert inspect_workspace(tmp_path) == ()

def test_digest_mismatch_refuses_repair(tmp_path: Path) -> None:
    _repo_path, worktree = _repo(tmp_path)
    shutil.rmtree(worktree)
    inspect_workspace(tmp_path)
    with pytest.raises(WorktreeRepairError, match="proposal changed"):
        repair_workspace(tmp_path, "0" * 64)

def test_inaccessible_target_parent_is_not_proposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = tmp_path / "repo"
    admin = repository / ".git" / "worktrees" / "missing"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(str(tmp_path / "external" / ".git"))
    monkeypatch.setattr(repair_module, "_git", lambda *_args: "Removing worktrees/missing: stale")
    original_stat = Path.stat
    def inaccessible(path: Path, *args, **kwargs):
        if path == tmp_path / "external":
            raise PermissionError("unavailable mount")
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", inaccessible)
    assert repair_module._plan(repository) == ()


def test_cancelled_or_changed_approval_does_not_mutate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = StaleWorktreeRecord("/repo", "/repo/.git/worktrees/missing", "/external")
    monkeypatch.setattr(repair_module, "inspect_workspace", lambda _workspace: (record,))
    pruned = []
    monkeypatch.setattr(repair_module, "_git", lambda *args: pruned.append(args))
    with pytest.raises(WorktreeRepairError, match="proposal changed"):
        repair_workspace(tmp_path, "0" * 64)
    assert pruned == []

def test_cli_json_proposal_and_approval_smoke(tmp_path: Path) -> None:
    _repo_path, worktree = _repo(tmp_path)
    shutil.rmtree(worktree)
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    command = [sys.executable, "-m", "josh_room", "workspace", "worktree-repair", "--workspace", str(tmp_path), "--json"]
    proposal_process = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30, env=environment)
    assert proposal_process.returncode == 0, proposal_process.stderr
    proposal = json.loads(proposal_process.stdout)
    assert proposal["state"] == "approval-required"
    assert len(proposal["records"]) == 1
    approved = subprocess.run(command + ["--approve", "--proposal-digest", proposal["proposal_digest"]], capture_output=True, text=True, check=False, timeout=30, env=environment)
    assert approved.returncode == 0, approved.stderr
    assert json.loads(approved.stdout)["state"] == "repaired"
    assert repair_module.inspect_workspace(tmp_path) == ()
