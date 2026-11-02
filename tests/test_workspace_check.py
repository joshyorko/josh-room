from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _cli(workspace: Path, state_root: Path) -> subprocess.CompletedProcess[str]:
    home = state_root / "home"
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(state_root / "config"),
        "XDG_STATE_HOME": str(state_root / "state"),
        "JOSH_ROOM_INSTANCE": str(state_root / "instance"),
        "PYTHONPATH": str(ROOT / "src"),
    }
    for key in ("JOSH_ROOM_IDENTITY", "JOSH_ROOM_ENCRYPTION_MATERIAL", "ROBOCORP_HOME"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "josh_room", "workspace-check", "--workspace", str(workspace), "--json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _tree_digest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[relative] = "directory"
    return result


def test_unmarked_workspace_check_is_read_only_and_needs_no_provider_or_age(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("ordinary workspace\n", encoding="utf-8")
    before = _tree_digest(workspace)

    result = _cli(workspace, tmp_path / "state")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scope"] == "local-workspace-validation"
    assert payload["entry_count"] == 1
    assert payload["signature_algorithm"] == "josh-room-stat-v1"
    assert _tree_digest(workspace) == before
    assert not (tmp_path / "state" / "instance").exists()


def test_stale_linked_worktree_reports_sanitized_action_and_never_prunes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repository = workspace / "repo"
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True, timeout=30)
    (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "fixture.txt"], check=True, capture_output=True, timeout=30)
    subprocess.run([
        "git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "fixture",
    ], check=True, capture_output=True, timeout=30)
    external = tmp_path / "outside-worktree"
    subprocess.run(["git", "-C", str(repository), "worktree", "add", "-b", "check-fixture", str(external)], check=True, capture_output=True, timeout=30)
    shutil.rmtree(external)
    before = _tree_digest(workspace)

    result = _cli(workspace, tmp_path / "state")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "workspace-invalid"
    assert "linked worktrees are unsupported" in payload["error"]
    assert "git worktree remove <workdir>" in payload["remediation"]
    assert str(external) not in json.dumps(payload)
    assert _tree_digest(workspace) == before
    records = subprocess.run(["git", "-C", str(repository), "worktree", "list", "--porcelain"], check=True, capture_output=True, text=True, timeout=30).stdout
    assert "check-fixture" in records


def test_live_linked_worktree_reports_removal_safety_and_exact_remedy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    repository.mkdir(parents=True)
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, capture_output=True, timeout=30)
    (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "fixture.txt"], check=True, capture_output=True, timeout=30)
    subprocess.run([
        "git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "fixture",
    ], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "remote", "add", "origin", str(upstream)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "push", "-u", "origin", "main"], check=True, capture_output=True, timeout=30)
    worktree = tmp_path / "live-worktree"
    subprocess.run([
        "git", "-C", str(repository), "worktree", "add", "-b", "live-review", str(worktree), "origin/main",
    ], check=True, capture_output=True, timeout=30)

    result = _cli(workspace, tmp_path / "state")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    linked = next(row["linked_worktree"] for row in payload["findings"] if row.get("linked_worktree"))
    assert linked == {"workdir": str(worktree), "safety": "safe-to-remove"}
    assert "git worktree remove" in payload["findings"][0]["message"]
    assert str(worktree) in payload["remediation"]
    assert "clean with no commits ahead" in payload["remediation"]

    (worktree / "ahead.txt").write_text("unpushed commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "ahead.txt"], check=True, capture_output=True, timeout=30)
    subprocess.run([
        "git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "unpushed",
    ], check=True, capture_output=True, timeout=30)
    ahead = _cli(workspace, tmp_path / "state-ahead")
    ahead_payload = json.loads(ahead.stdout)
    ahead_linked = next(row["linked_worktree"] for row in ahead_payload["findings"] if row.get("linked_worktree"))
    assert ahead_linked == {"workdir": str(worktree), "safety": "unique-local-work"}
    assert "contains uncommitted or unpushed work" in ahead_payload["remediation"]

    (worktree / "local.txt").write_text("keep me\n", encoding="utf-8")
    dirty = _cli(workspace, tmp_path / "state-dirty")
    dirty_payload = json.loads(dirty.stdout)
    dirty_linked = next(row["linked_worktree"] for row in dirty_payload["findings"] if row.get("linked_worktree"))
    assert dirty_linked == {"workdir": str(worktree), "safety": "unique-local-work"}
    assert "contains uncommitted or unpushed work" in dirty_payload["remediation"]


def test_linked_worktree_filter_configuration_skips_status_probe(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "commit", "--allow-empty", "-m", "initial"], check=True, capture_output=True, timeout=30)
    worktree = tmp_path / "filter-worktree"
    subprocess.run(["git", "-C", str(repository), "worktree", "add", "-b", "filter-check", str(worktree), "HEAD"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repository), "config", "filter.probe.clean", "touch must-not-run; cat"], check=True, capture_output=True, timeout=30)
    record = next((repository / ".git" / "worktrees").glob("*/gitdir"))
    relative = record.relative_to(workspace).as_posix()
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    from josh_room import workspace_report

    real_run = subprocess.run
    calls = []

    def guarded_run(args, **kwargs):
        calls.append(args)
        assert "config" in args
        assert "status" not in args
        return real_run(args, **kwargs)

    monkeypatch.setattr(workspace_report.subprocess, "run", guarded_run)
    result = workspace_report._linked_worktree_status(workspace, relative)

    assert result == {"workdir": str(worktree), "safety": "unknown"}
    assert len(calls) == 1


def test_workspace_check_uses_shared_scanner_for_non_git_failures(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "special")

    result = _cli(workspace, tmp_path / "state")

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "workspace-invalid"
    assert payload["error"] == "workspace contains a special file"
    assert payload["remediation"] == "Fix the reported workspace validation issue and run the check again."
