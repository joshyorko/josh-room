"""Bounded diagnostics using the exact fail-closed Save traversal."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from .git_workspace import _text as _git_text
from .workspace_init import policy_edit_token
from .workspace_policy import load_capture_policy


class ScanStopped(Exception):
    pass


class Diagnostics:
    def __init__(
        self, *, max_entries=100000, max_findings=200, max_seconds=15, cancelled=None
    ):
        if min(max_entries, max_findings, max_seconds) <= 0:
            raise ValueError("Scan limits must be positive")
        self.max_entries, self.max_findings = max_entries, max_findings
        self.deadline = time.monotonic() + max_seconds
        self.cancelled = cancelled or (lambda: False)
        self.entries = 0
        self.findings = []
        self.complete = True
        self.stop_reason = None

    def check_budget(self, *, entry=False):
        self.entries += int(entry)
        reason = (
            "cancelled"
            if self.cancelled()
            else "entry-limit"
            if self.entries > self.max_entries
            else "time-limit"
            if time.monotonic() >= self.deadline
            else None
        )
        if reason:
            self.complete = False
            self.stop_reason = reason
            raise ScanStopped(reason)

    def add(self, code, paths, message, *, incomplete=False):
        if len(self.findings) >= self.max_findings:
            self.complete = False
            self.stop_reason = "finding-limit"
            raise ScanStopped(self.stop_reason)
        self.complete &= not incomplete
        self.findings.append({"code": code, "paths": paths, "message": message})

    def git_error(self, message, relative):
        finding = {"code": "git-storage", "paths": [relative], "message": message}
        linked_worktree = _linked_worktree_status(self.root, relative)
        if linked_worktree:
            finding["linked_worktree"] = linked_worktree
            finding["message"] = (
                "Linked worktree Git administrative record blocks saving; inspect its safety "
                "before removing it with `git worktree remove <workdir>`."
            )
        if (
            relative.endswith("/.git")
            and (self.root / relative).is_file()
            and not (self.root / relative).is_symlink()
        ):
            finding["exclusion_candidate"] = relative[:-5]
        self.add(
            finding["code"],
            finding["paths"],
            finding["message"],
            incomplete="cannot be read safely" in message,
        )
        for key in ("exclusion_candidate", "linked_worktree"):
            if key in finding:
                self.findings[-1][key] = finding[key]


def _linked_worktree_status(root: Path, relative: str) -> dict | None:
    record = root / relative
    parts = Path(relative).parts
    if len(parts) < 4 or parts[-3] != "worktrees" or parts[-1] != "gitdir":
        return None
    workdir = None
    try:
        if record.is_symlink() or not record.is_file():
            return None
        pointer = _git_text(record, 64 * 1024).strip()
        if not pointer:
            return None
        linked_gitfile = Path(pointer)
        if not linked_gitfile.is_absolute():
            linked_gitfile = record.parent / linked_gitfile
        if linked_gitfile.is_symlink() or not linked_gitfile.is_file():
            return None
        linked_pointer = _git_text(linked_gitfile, 64 * 1024).strip()
        linked_gitfile = linked_gitfile.resolve(strict=True)
        if linked_gitfile.name != ".git":
            return None
        workdir = linked_gitfile.parent
        if linked_gitfile != workdir / ".git":
            return None
        if not linked_pointer.startswith("gitdir: "):
            return None
        linked_target = Path(linked_pointer[8:])
        if not linked_target.is_absolute():
            linked_target = workdir / linked_target
        if linked_target.resolve(strict=True) != record.parent.resolve(strict=True):
            return None
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("GIT_"):
                environment.pop(key, None)
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
        )

        def git(*args):
            return subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-C", str(workdir), *args],
                capture_output=True,
                timeout=5,
                check=False,
                env=environment,
            )

        filters = git("config", "--null", "--get-regexp", r"^filter\.")
        if filters.returncode not in {0, 1} or filters.stdout:
            return {"workdir": str(workdir), "safety": "unknown"}
        submodules = git("ls-files", "--stage")
        if submodules.returncode or any(
            line.startswith(b"160000 ") for line in submodules.stdout.splitlines()
        ):
            return {"workdir": str(workdir), "safety": "unknown"}
        index_flags = git("ls-files", "-v")
        if index_flags.returncode or any(
            line[:1].islower() or line.startswith(b"S")
            for line in index_flags.stdout.splitlines()
        ):
            return {"workdir": str(workdir), "safety": "unknown"}
        status = git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=all",
        )
        if status.returncode:
            return {"workdir": str(workdir), "safety": "unknown"}
        if status.stdout:
            return {"workdir": str(workdir), "safety": "unique-local-work"}
        ahead = git("rev-list", "--count", "@{upstream}..HEAD")
        if ahead.returncode:
            return {"workdir": str(workdir), "safety": "unknown"}
        if ahead.stdout.strip() == b"0":
            return {"workdir": str(workdir), "safety": "safe-to-remove"}
        return {"workdir": str(workdir), "safety": "unique-local-work"}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        if workdir is None:
            return None
        return {"workdir": str(workdir), "safety": "unknown"}




def workspace_report(workspace: Path, **limits) -> dict:
    from .room_store_operations import _scan_workspace

    diagnostics = Diagnostics(**limits)
    scan = policy = token = None
    try:
        root = Path(workspace).resolve(strict=True)
        runtime = os.environ.get("ROBOCORP_HOME")
        policy = load_capture_policy(
            root,
            active_runtime_root=Path(runtime)
            if runtime and Path(runtime).is_dir()
            else None,
        )
        diagnostics.root = root
        token = policy_edit_token(root)
        scan = _scan_workspace(root, policy, diagnostics=diagnostics)
    except ScanStopped:
        pass
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        diagnostics.complete = False
        if len(diagnostics.findings) < diagnostics.max_findings:
            diagnostics.add("workspace-invalid", ["."], str(error), incomplete=True)
    findings = sorted(diagnostics.findings, key=lambda row: (row["paths"], row["code"]))
    ok = diagnostics.complete and not findings
    result = {
        "ok": ok,
        "scope": "local-workspace-validation",
        "complete": diagnostics.complete,
        "truncated": diagnostics.stop_reason
        in {"entry-limit", "finding-limit", "time-limit"},
        "cancelled": diagnostics.stop_reason == "cancelled",
        "stop_reason": diagnostics.stop_reason,
        "findings": findings,
        "examined_entries": diagnostics.entries,
        "policy_edit_sha256": token,
        "capture_mode": getattr(policy, "capture_mode", "full-repository"),
    }
    if policy:
        result["capture_policy_sha256"] = policy.sha256
    if scan:
        result.update(entry_count=len(scan.paths), logical_bytes=scan.logical_bytes)
        if ok:
            result.update(
                workspace_signature=scan.signature,
                signature_algorithm=scan.signature_algorithm,
            )
    if not ok:
        result.update(
            code="workspace-invalid",
            error=findings[0]["message"]
            if findings
            else "Workspace scan did not complete.",
            remediation="Fix the reported workspace validation issue and run the check again.",
        )
        linked = [row["linked_worktree"] for row in findings if row.get("linked_worktree")]
        if linked:
            guidance = []
            for worktree in linked:
                path = worktree.get("workdir")
                safety = worktree["safety"]
                command = (
                    f"`git worktree remove -- {shlex.quote(path)}`"
                    if path
                    else "`git worktree remove <workdir>`"
                )
                if safety == "safe-to-remove":
                    note = "clean with no commits ahead of its configured upstream"
                elif safety == "unique-local-work":
                    note = "contains uncommitted or unpushed work; preserve it before removal"
                else:
                    note = "safety could not be determined; inspect it before removal"
                guidance.append(f"{path or '<workdir>'}: {note}; remove with {command}")
            result["remediation"] = "Linked worktrees block Save. " + " ".join(guidance)
        elif any("linked worktrees" in row["message"] for row in findings):
            result["remediation"] = (
                "Inspect linked worktrees with `git worktree list`; remove a worktree with "
                "`git worktree remove <workdir>` only after preserving any local work."
            )
    return result
