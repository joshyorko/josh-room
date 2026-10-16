"""Bounded diagnostics using the exact fail-closed Save traversal."""

from __future__ import annotations

import os
import time
from pathlib import Path

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
        if "exclusion_candidate" in finding:
            self.findings[-1]["exclusion_candidate"] = finding["exclusion_candidate"]


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
        if any("linked worktrees" in row["message"] for row in findings):
            result["remediation"] = (
                "Inspect linked worktrees with `git worktree prune --dry-run --verbose`; choose source-only explicitly or repair Git storage manually before saving."
            )
    return result
