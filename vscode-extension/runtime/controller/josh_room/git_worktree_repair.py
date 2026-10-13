"""Explicit, workspace-scoped repair of stale Git worktree records.

Inspection is read-only. Mutation requires the caller to pass the exact plan
returned by :func:`inspect_workspace`; the plan is revalidated immediately
before pruning so a changed repository cannot be broadened accidentally.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class WorktreeRepairError(ValueError):
    """A repair could not be established safely."""


@dataclass(frozen=True, slots=True)
class StaleWorktreeRecord:
    repository: str
    administrative_path: str
    worktree_path: str

    def to_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "administrative_path": self.administrative_path,
            "worktree_path": self.worktree_path,
        }


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorktreeRepairError("Git worktree state could not be established safely") from error
    return result.stdout or result.stderr


def _repositories(workspace: Path) -> tuple[Path, ...]:
    workspace = workspace.resolve(strict=True)
    found: set[Path] = set()
    for directory, dirs, files in os.walk(workspace, followlinks=False):
        has_git = ".git" in dirs or ".git" in files
        dirs[:] = [name for name in dirs if name not in {".git", ".josh-room"}]
        if has_git:
            candidate = Path(directory)
            try:
                common = Path(_git(candidate, "rev-parse", "--git-common-dir").strip())
                if not common.is_absolute():
                    common = (candidate / common).resolve(strict=True)
                else:
                    common = common.resolve(strict=True)
                repository = common.parent if common.name == ".git" else common
                repository.relative_to(workspace)
                found.add(repository)
            except (WorktreeRepairError, OSError, ValueError):
                continue
    return tuple(sorted(found, key=lambda path: path.as_posix()))


def _plan(repository: Path) -> tuple[StaleWorktreeRecord, ...]:
    # Git is authoritative about eligibility; we never infer staleness from paths.
    output = _git(repository, "worktree", "prune", "--dry-run", "--verbose")
    records: list[StaleWorktreeRecord] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.lower().startswith(("removing ", "would remove ", "would prune ")):
            continue
        administrative = line.split(None, 1)[1].split(":", 1)[0].strip()
        admin_path = Path(administrative)
        if not admin_path.is_absolute():
            admin_path = (repository / ".git" / admin_path).resolve()
        target: Path | None = None
        try:
            target_text = (admin_path / "gitdir").read_text(encoding="utf-8").strip()
            if not target_text:
                continue
            target = Path(target_text).resolve()
            target.parent.stat()
            # An existing worktree path may contain user data even if its .git
            # file was removed. Leave it for manual recovery.
            if os.path.lexists(target.parent):
                continue
        except (PermissionError, UnicodeError, RuntimeError):
            continue
        except FileNotFoundError:
            if target is None:
                # Without Git's back-pointer there is no safe target to show or
                # pass to `git worktree remove`.
                continue
            try:
                target.parent.parent.stat()
            except (OSError, RuntimeError):
                continue
        records.append(StaleWorktreeRecord(repository.as_posix(), admin_path.as_posix(), target.parent.as_posix()))
    return tuple(records)


def inspect_workspace(workspace: Path) -> tuple[StaleWorktreeRecord, ...]:
    """Return Git's exact dry-run stale-record proposal for this workspace."""
    workspace = Path(workspace).resolve(strict=True)
    records: list[StaleWorktreeRecord] = []
    for repository in _repositories(workspace):
        records.extend(_plan(repository))
    return tuple(records)

def proposal_digest(records: tuple[StaleWorktreeRecord, ...]) -> str:
    body = json.dumps([record.to_dict() for record in records], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()
def repair_workspace(workspace: Path, approved_digest: str) -> tuple[StaleWorktreeRecord, ...]:
    """Prune an approved proposal after immediate exact digest revalidation."""
    current = inspect_workspace(workspace)
    if proposal_digest(current) != approved_digest:
        raise WorktreeRepairError("stale worktree proposal changed; refresh before retrying Save")
    if not current:
        return ()
    for record in current:
        # Confirm each record again immediately before touching it. Git's
        # repository-wide `worktree prune` would also remove stale records that
        # were inaccessible or outside the user's approved workspace proposal.
        if record not in inspect_workspace(Path(workspace)):
            raise WorktreeRepairError("stale worktree proposal changed; refresh before retrying Save")
        target = Path(record.worktree_path)
        if os.path.lexists(target):
            raise WorktreeRepairError("stale worktree target reappeared; refresh before retrying Save")
        try:
            target.parent.stat()
        except FileNotFoundError:
            # The parent is also gone; Git can still remove the exact admin
            # record by its recorded worktree path.
            pass
        except OSError as error:
            raise WorktreeRepairError("stale worktree target is inaccessible; refresh before retrying Save") from error
        _git(Path(record.repository), "worktree", "remove", record.worktree_path)
    return current
