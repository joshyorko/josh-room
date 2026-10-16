"""Explicit, non-overwriting local capture-policy initialization and edits."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .workspace_policy import (
    _IGNORE_LIMIT,
    _read_ignore,
    _user_patterns,
    load_capture_policy,
)

STARTER = b"""# Josh Room exclusions: excluded files stay local but are not backed up.
# Built-in defaults already exclude common environments and caches.
# Rules add exclusions; they cannot undo built-in defaults. This is not gitignore.
# Use relative paths and * / ** globs. Reinclusion (!), absolute paths and [] are unsupported.
# Uncomment only generated folders you intend to omit, for example:
# tools/ci-venv
# build/generated
# Keep source, manifests, lockfiles, configuration, databases and uploads you need.
"""


def initialize_workspace(workspace: Path) -> dict:
    root = Path(workspace).resolve(strict=True)
    load_capture_policy(root)
    _user_patterns(STARTER)
    path = root / ".josh-roomignore"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
    except FileExistsError:
        load_capture_policy(root)
        state = "already-present"
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(STARTER)
            stream.flush()
            os.fsync(stream.fileno())
        state = "created"
    policy = load_capture_policy(root)
    return {
        "ok": True,
        "state": state,
        "capture_policy_sha256": policy.sha256,
        "policy_edit_sha256": policy_edit_token(root),
        "capture_mode": policy.capture_mode,
    }


def policy_edit_token(workspace: Path) -> str:
    raw = _read_ignore(Path(workspace))
    return hashlib.sha256(b"absent" if raw is None else b"present\0" + raw).hexdigest()


def append_exclusions(
    workspace: Path, rules: list[str], *, expected_token: str, source_only: bool = False
) -> dict:
    """Append reviewed literal directories without replacing existing bytes.

    The preview token detects edits before opening. O_APPEND preserves edits by
    other writers; identity/content checks detect races before our single write.
    No lock can stop an editor that ignores it, so never replace the user's file.
    """
    root = Path(workspace).resolve(strict=True)
    load_capture_policy(root)
    if policy_edit_token(root) != expected_token:
        raise ValueError("Exclusions changed; review the fresh policy before applying")
    for rule in rules:
        if (
            not isinstance(rule, str)
            or rule != rule.strip()
            or any(c in rule for c in "*?[]!\\\r\n#")
        ):
            raise ValueError(
                "Selected directory cannot be represented as an exact exclusion"
            )
        _user_patterns(rule.encode())
        target = root / rule
        if (
            not target.is_dir()
            or target.is_symlink()
            or target.resolve().relative_to(root).as_posix() != rule
        ):
            raise ValueError(
                "Selected exclusion must be a workspace-relative directory"
            )
    before = _read_ignore(root)
    if policy_edit_token(root) != expected_token:
        raise ValueError("Exclusions changed; review again")
    if before is None:
        initialized = initialize_workspace(root)
        if initialized["state"] != "created":
            raise ValueError("Exclusions changed; review again")
        before = STARTER
    path = root / ".josh-roomignore"
    raw = _read_ignore(root)
    if raw != before:
        raise ValueError("Exclusions changed; review again")
    assert raw is not None
    existing = {line.strip() for line in raw.decode().splitlines()}
    additions = [
        f"{rule}/**" for rule in dict.fromkeys(rules) if f"{rule}/**" not in existing
    ]
    directive = "# josh-room capture-mode: source-only"
    if source_only and directive not in existing:
        if any(line.startswith("# josh-room capture-mode:") for line in existing):
            raise ValueError(
                "Edit the existing capture-mode directive explicitly before continuing"
            )
        additions.append(directive)
    if not additions:
        return {"ok": True, "state": "unchanged"}
    suffix = (
        ("" if not raw or raw.endswith(b"\n") else "\n") + "\n".join(additions) + "\n"
    )
    combined = raw + suffix.encode()
    if len(combined) > _IGNORE_LIMIT:
        raise ValueError("workspace ignore file exceeds 64 KiB")
    _user_patterns(combined)
    metadata = path.lstat()
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        opened = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or stream.read(_IGNORE_LIMIT + 1) != raw
        ):
            raise ValueError("Exclusions changed; review again")
        current = path.lstat()
        latest = os.fstat(stream.fileno())
        if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or (latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns)
                != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)):
            raise ValueError("Exclusions changed; review again")
        if os.write(stream.fileno(), suffix.encode()) != len(suffix.encode()):
            raise OSError("Exclusion append was incomplete; inspect the ignore file")
        os.fsync(stream.fileno())
        current = path.lstat()
        if ((current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or _read_ignore(root) != combined):
            raise ValueError("Exclusions changed during append; inspect the file and rescan")
    return {
        "ok": True,
        "state": "updated",
        "capture_policy_sha256": load_capture_policy(root).sha256,
    }
