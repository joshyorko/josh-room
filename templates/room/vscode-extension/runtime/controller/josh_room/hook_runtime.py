"""Lightweight isolated Codex hook boundary.

Only the standard library is imported until the trigger is validated. Queue
state is always published through :class:`PccOutbox`; this module does not
implement a second queue or recovery ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from .pcc_enqueue import canonical_source, enqueue_trigger

_MAX = 64 * 1024
_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_ADAPTER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIELDS = {
    "Stop": {"session_id", "turn_id", "transcript_path", "cwd", "hook_event_name", "model", "permission_mode", "stop_hook_active", "last_assistant_message"},
    "SubagentStop": {"session_id", "turn_id", "transcript_path", "agent_transcript_path", "cwd", "hook_event_name", "model", "permission_mode", "stop_hook_active", "agent_id", "agent_type", "last_assistant_message"},
    "SessionEnd": {"session_id", "transcript_path", "cwd", "hook_event_name", "reason"},
}


def _home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError, OSError):
        return Path.home()


def _text(value: object, *, nullable: bool = False, identifier: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("invalid-input")
    if identifier and _ADAPTER_ID.fullmatch(value) is None:
        raise ValueError("invalid-input")
    return value


def _path(value: object, *, nullable: bool = False) -> str | None:
    result = _text(value, nullable=nullable)
    if result is not None and not Path(result).is_absolute():
        raise ValueError("invalid-input")
    return result


def _validate(payload: object) -> tuple[str, dict[str, object]]:
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in _EVENTS:
        raise ValueError("invalid-input")
    event = payload["hook_event_name"]
    if set(payload) != _FIELDS[event]:
        raise ValueError("invalid-input")
    _text(payload["session_id"], identifier=True)
    _path(payload["cwd"])
    _path(payload["transcript_path"], nullable=True)
    if event == "SubagentStop":
        _path(payload["agent_transcript_path"], nullable=True)
    if event in {"Stop", "SubagentStop"}:
        _text(payload["turn_id"], identifier=True); _text(payload["model"]); _text(payload["permission_mode"])
        if type(payload["stop_hook_active"]) is not bool: raise ValueError("invalid-input")
        _text(payload["last_assistant_message"], nullable=True)
    if event == "SubagentStop":
        _text(payload["agent_id"], identifier=True); _text(payload["agent_type"])
    if event == "SessionEnd" and payload["reason"] != "other":
        raise ValueError("invalid-input")
    return event, payload


def _roots() -> tuple[Path, Path]:
    active_value = os.environ.get("JOSH_ROOM_CODEX_ACTIVE_ROOT")
    archived_value = os.environ.get("JOSH_ROOM_CODEX_ARCHIVED_ROOT")
    if active_value and archived_value:
        active, archived = Path(active_value), Path(archived_value)
    else:
        home = Path(os.environ.get("CODEX_HOME", _home() / ".codex"))
        if not home.is_absolute(): home = _home() / ".codex"
        active, archived = home / "sessions", home / "archived_sessions"
    if not active.is_absolute() or not archived.is_absolute():
        raise ValueError("source-not-contained")
    active, archived = active.resolve(strict=False), archived.resolve(strict=False)
    if active == archived:
        raise ValueError("source-not-contained")
    return active, archived


def _source(event: str, payload: dict[str, object]) -> tuple[str, str]:
    active, archived = _roots()
    return canonical_source(event, payload, active, archived)


def _outbox() -> Path:
    value = os.environ.get("JOSH_ROOM_HOOK_OUTBOX") or os.environ.get("JOSH_ROOM_OUTBOX_ROOT")
    root = Path(value) if value else (_home() / ".local" / "state" / "josh-room" / "pcc-outbox")
    if not root.is_absolute(): raise ValueError("storage-unavailable")
    return root


def process(payload: object) -> dict[str, object]:
    try:
        event, payload = _validate(payload)
        source, representation = _source(event, payload)
        event_id = "codex-" + hashlib.sha256("|".join((event, str(payload["session_id"]), str(payload.get("turn_id", "")), str(payload.get("agent_id", "")), source)).encode()).hexdigest()[:32]
        from .pcc_outbox import PccOutbox, QueueState
        receipt = enqueue_trigger(
            PccOutbox(_outbox()),
            event_id=event_id,
            session_id=str(payload["session_id"]),
            checkpoint={"source": source, "representation": representation, "start": 0, "end": 0, "prefix_sha256": _EMPTY_DIGEST},
            is_final=event == "SessionEnd",
            metadata={"source_surface": "subagent" if event == "SubagentStop" else "unknown", "source_adapter": "codex-transcript", "source_adapter_version": "1", "object_kind": "trigger"},
            policy_decision="local-only",
        )
        return {"ok": True, "accepted": receipt.state is not QueueState.CAPTURE_GAP, "event_id": event_id, "state": receipt.state.value}
    except (ImportError, KeyError, OSError, RecursionError, RuntimeError, TypeError, UnicodeError, ValueError):
        return {"ok": True, "accepted": False, "diagnostic": "capture-gap"}


def main(stream=None) -> int:
    try:
        raw = (stream or sys.stdin.buffer).read(_MAX + 1)
        if len(raw) > _MAX:
            return 0
        process(json.loads(raw.decode("utf-8")))
    except (AttributeError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
        return 0
    return 0
