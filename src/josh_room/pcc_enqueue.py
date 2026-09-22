"""Canonical lightweight trigger enqueue and source boundary.

The trigger-only hook imports this module before the full controller surface. The
queue schema, validation, lock, atomic publication, coalescing, limits, and
crash receipts remain in :mod:`pcc_outbox`; this module exposes the sole
cross-module enqueue primitive so hook producers cannot grow a second ledger.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from .pcc_outbox import PccOutbox, QueueReceipt

_COPY_SUFFIX = re.compile(r"-copy(?:-[0-9]+)?$")


def canonical_source(
    event: str,
    payload: Mapping[str, object],
    active_root: Path,
    archived_root: Path,
) -> tuple[str, str]:
    """Apply the #6 source path representation without opening transcript bytes."""

    value = payload.get("agent_transcript_path") if event == "SubagentStop" and payload.get("agent_transcript_path") is not None else payload.get("transcript_path")
    if value is None:
        return "codex-hook", "unknown"
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raw = Path(str(payload["cwd"])) / raw
    raw = Path(os.path.abspath(raw))
    roots = ((active_root, "active"), (archived_root, "archived"))
    for root, root_name in roots:
        try:
            relative = raw.relative_to(root)
        except ValueError:
            continue
        current = root
        for part in relative.parts:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError("source-not-contained")
            except OSError:
                raise ValueError("source-not-contained") from None
        try:
            canonical = raw.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ValueError("source-not-contained") from None
        if canonical.name.startswith(".") or not canonical.is_file():
            raise ValueError("source-not-contained")
        try:
            path_key = canonical.relative_to(root).as_posix()
        except ValueError:
            raise ValueError("source-not-contained") from None
        name = canonical.name
        if name.endswith(".jsonl.zst"):
            representation = "compressed-jsonl-zst" if root_name == "archived" else None
            suffix = ".jsonl.zst"
        elif name.endswith(".jsonl"):
            representation = "active-jsonl" if root_name == "active" else "archived-jsonl"
            suffix = ".jsonl"
        else:
            representation = None
            suffix = ""
        if representation is None:
            raise ValueError("source-not-contained")
        stem = name[:-len(suffix)] if suffix else ""
        token = _COPY_SUFFIX.sub("", stem.removeprefix("rollout-"))
        if not stem.startswith("rollout-") or token != str(payload["session_id"]):
            raise ValueError("source-not-contained")
        if path_key.endswith(".zst"):
            path_key = path_key.removesuffix(".zst")
        source = "codex-" + hashlib.sha256(f"session:{payload['session_id']}|path:{path_key}".encode()).hexdigest()[:32]
        return source, representation
    raise ValueError("source-not-contained")


def enqueue_trigger(
    outbox: PccOutbox,
    *,
    event_id: str,
    session_id: str,
    checkpoint: Mapping[str, object],
    is_final: bool = False,
    metadata: Mapping[str, object] | None = None,
    policy_decision: str = "allow",
    diagnostic_detail: object | None = None,
) -> QueueReceipt:
    """Enqueue through the #8 authority without duplicating queue semantics."""

    return outbox._enqueue_authority(
        event_id=event_id,
        session_id=session_id,
        checkpoint=checkpoint,
        is_final=is_final,
        metadata=metadata,
        policy_decision=policy_decision,
        diagnostic_detail=diagnostic_detail,
    )


__all__ = ["canonical_source", "enqueue_trigger"]
