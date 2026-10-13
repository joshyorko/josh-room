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


def test_missing_administrative_gitdir_is_skipped_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = tmp_path / "repo"
    admin = repository / ".git" / "worktrees" / "missing"
    admin.mkdir(parents=True)
    monkeypatch.setattr(repair_module, "_git", lambda *_args: "Removing worktrees/missing: stale")
    assert repair_module._plan(repository) == ()


def test_cancelled_or_changed_approval_does_not_mutate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = StaleWorktreeRecord("/repo", "/repo/.git/worktrees/missing", "/external")
    monkeypatch.setattr(repair_module, "inspect_workspace", lambda _workspace: (record,))
    mutations = []
    monkeypatch.setattr(repair_module, "_git", lambda *args: mutations.append(args))
    with pytest.raises(WorktreeRepairError, match="proposal changed"):
        repair_workspace(tmp_path, "0" * 64)
    assert mutations == []


def test_records_changed_after_digest_check_are_not_mutated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = StaleWorktreeRecord("/repo", "/repo/.git/worktrees/missing", "/external")
    calls = 0

    def changing_proposal(_workspace):
        nonlocal calls
        calls += 1
        return (record,) if calls == 1 else ()

    mutations = []
    monkeypatch.setattr(repair_module, "inspect_workspace", changing_proposal)
    monkeypatch.setattr(repair_module, "_git", lambda *args: mutations.append(args))
    with pytest.raises(WorktreeRepairError, match="changed"):
        repair_workspace(tmp_path, proposal_digest((record,)))
    assert mutations == []


def test_repair_removes_only_approved_records_in_mixed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, eligible = _repo(tmp_path)
    locked = tmp_path / "locked-worktree"
    inaccessible_parent = tmp_path.parent / f"inaccessible-parent-{tmp_path.name}"
    inaccessible = inaccessible_parent / "inaccessible-worktree"
    for path, branch in ((locked, "locked-test"), (inaccessible, "inaccessible-test")):
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(path)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(locked), "--reason", "fixture"], check=True, capture_output=True, timeout=30)

    outside_repo = tmp_path.parent / f"outside-{tmp_path.name}"
    subprocess.run(["git", "init", str(outside_repo)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(outside_repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True, timeout=30)
    outside_worktree = tmp_path.parent / f"outside-worktree-{tmp_path.name}"
    subprocess.run(["git", "-C", str(outside_repo), "worktree", "add", "-b", "outside-test", str(outside_worktree)], check=True, capture_output=True, timeout=30)

    for path in (eligible, locked, inaccessible, outside_worktree):
        shutil.rmtree(path)

    original_stat = Path.stat

    def selective_stat(path: Path, *args, **kwargs):
        if path == inaccessible_parent:
            raise PermissionError("synthetic inaccessible mount")
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "stat", selective_stat)
        proposal = inspect_workspace(tmp_path)
        assert len(proposal) == 1
        assert proposal[0].worktree_path == str(eligible)
        assert repair_workspace(tmp_path, proposal_digest(proposal)) == proposal

    remaining = inspect_workspace(tmp_path)
    assert len(remaining) == 1
    assert remaining[0].worktree_path == str(inaccessible)
    assert (repo / ".git" / "worktrees" / "locked-worktree" / "locked").is_file()
    assert (repo / ".git" / "worktrees" / "inaccessible-worktree" / "gitdir").is_file()
    assert (outside_repo / ".git" / "worktrees" / outside_worktree.name / "gitdir").is_file()
    shutil.rmtree(outside_repo)
    shutil.rmtree(inaccessible_parent)

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
