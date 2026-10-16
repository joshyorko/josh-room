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
    assert "git worktree prune --dry-run --verbose" in payload["remediation"]
    assert str(external) not in json.dumps(payload)
    assert _tree_digest(workspace) == before
    records = subprocess.run(["git", "-C", str(repository), "worktree", "list", "--porcelain"], check=True, capture_output=True, text=True, timeout=30).stdout
    assert "check-fixture" in records


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
