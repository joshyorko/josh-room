"""Explicit, workspace-scoped repair of stale Git worktree records.

Inspection is read-only. Mutation requires the caller to pass the exact plan
returned by :func:`inspect_workspace`; the plan is revalidated immediately
before pruning so a changed repository cannot be broadened accidentally.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


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
        try:
            target = Path((admin_path / "gitdir").read_text(encoding="utf-8").strip()).resolve()
        except (OSError, UnicodeError, RuntimeError):
            target = Path("")
        records.append(StaleWorktreeRecord(repository.as_posix(), admin_path.as_posix(), target.as_posix()))
    return tuple(records)


def inspect_workspace(workspace: Path) -> tuple[StaleWorktreeRecord, ...]:
    """Return Git's exact dry-run stale-record proposal for this workspace."""
    workspace = Path(workspace).resolve(strict=True)
    records: list[StaleWorktreeRecord] = []
    for repository in _repositories(workspace):
        records.extend(_plan(repository))
    return tuple(records)


def repair_workspace(workspace: Path, approved: tuple[StaleWorktreeRecord, ...]) -> tuple[StaleWorktreeRecord, ...]:
    """Prune exactly an approved proposal after an immediate revalidation."""
    current = inspect_workspace(workspace)
    if current != tuple(approved):
        raise WorktreeRepairError("stale worktree proposal changed; refresh before retrying Save")
    if not current:
        return ()
    for repository in sorted({record.repository for record in current}):
        _git(Path(repository), "worktree", "prune")
    return current
