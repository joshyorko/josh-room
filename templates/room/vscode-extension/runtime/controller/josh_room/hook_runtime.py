"""Lightweight isolated runtime for the installed Codex command hook.

This module intentionally imports only the standard library.  It mirrors the
#6 path-only containment rules and publishes the bounded trigger record used by
#8, leaving source reconciliation and full queue management to normal Josh
Room processes.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

_MAX = 64 * 1024
_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()
_FIELDS = {
    "Stop": {"session_id", "turn_id", "transcript_path", "cwd", "hook_event_name", "model", "permission_mode", "stop_hook_active", "last_assistant_message"},
    "SubagentStop": {"session_id", "turn_id", "transcript_path", "agent_transcript_path", "cwd", "hook_event_name", "model", "permission_mode", "stop_hook_active", "agent_id", "agent_type", "last_assistant_message"},
    "SessionEnd": {"session_id", "transcript_path", "cwd", "hook_event_name", "reason"},
}


def _home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _text(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value or len(value.encode("utf-8")) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
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
    _text(payload["session_id"])
    _path(payload["cwd"])
    _path(payload["transcript_path"], nullable=True)
    if event == "SubagentStop":
        _path(payload["agent_transcript_path"], nullable=True)
    if event in {"Stop", "SubagentStop"}:
        _text(payload["turn_id"]); _text(payload["model"]); _text(payload["permission_mode"])
        if type(payload["stop_hook_active"]) is not bool: raise ValueError("invalid-input")
        _text(payload["last_assistant_message"], nullable=True)
    if event == "SubagentStop":
        _text(payload["agent_id"]); _text(payload["agent_type"])
    if event == "SessionEnd" and payload["reason"] != "other":
        raise ValueError("invalid-input")
    return event, payload


def _roots() -> tuple[Path, Path]:
    active = os.environ.get("JOSH_ROOM_CODEX_ACTIVE_ROOT")
    archived = os.environ.get("JOSH_ROOM_CODEX_ARCHIVED_ROOT")
    if active and archived:
        return Path(active).resolve(strict=False), Path(archived).resolve(strict=False)
    home = Path(os.environ.get("CODEX_HOME", _home() / ".codex"))
    if not home.is_absolute(): home = _home() / ".codex"
    return (home / "sessions").resolve(strict=False), (home / "archived_sessions").resolve(strict=False)


def _source(event: str, payload: dict[str, object]) -> tuple[str, str]:
    value = payload.get("agent_transcript_path") if event == "SubagentStop" and payload.get("agent_transcript_path") is not None else payload.get("transcript_path")
    if value is None: return "codex-hook", "unknown"
    active, archived = _roots()
    raw = Path(value)
    if not raw.is_absolute(): raw = Path(payload["cwd"]) / raw
    raw = Path(os.path.abspath(raw))
    for root in (active, archived):
        try: relative = raw.relative_to(root)
        except ValueError: continue
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink(): raise ValueError("source-not-contained")
        canonical = raw.resolve(strict=False)
        try: path_key = canonical.relative_to(root).as_posix()
        except ValueError: raise ValueError("source-not-contained") from None
        name = canonical.name
        suffix = ".jsonl.zst" if name.endswith(".jsonl.zst") else ".jsonl" if name.endswith(".jsonl") else ""
        stem = name[:-len(suffix)] if suffix else ""
        token = stem.removeprefix("rollout-")
        for suffix_copy in ("-copy",):
            if token.endswith(suffix_copy): token = token[:-len(suffix_copy)]
        if not stem.startswith("rollout-") or token != payload["session_id"] or not canonical.is_file():
            raise ValueError("source-not-contained")
        if path_key.endswith(".zst"): path_key = path_key.removesuffix(".zst")
        return "codex-" + hashlib.sha256(f"session:{payload['session_id']}|path:{path_key}".encode()).hexdigest()[:32], ("active-jsonl" if root == active else "archived-jsonl") if suffix == ".jsonl" else ("compressed-jsonl-zst" if root == archived else "active-jsonl")
    raise ValueError("source-not-contained")


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
        root = _outbox(); queue = root / "queue"; root.mkdir(parents=True, exist_ok=True, mode=0o700); queue.mkdir(mode=0o700, exist_ok=True)
        lock_path = root / "state.lock"
        with lock_path.open("a+b") as lock:
            try:
                import fcntl; fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except ImportError: pass
            existing = sorted(queue.glob("*.json"))
            for path in existing:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record.get("event_id") == event_id or (record.get("session_id") == payload["session_id"] and record.get("checkpoint", {}).get("source") == source):
                        return {"ok": True, "accepted": True, "event_id": event_id, "state": "queued"}
                except Exception: continue
            sequence = max((json.loads(p.read_text()).get("sequence", 0) for p in existing), default=0) + 1
            record = {"event_id": event_id, "session_id": payload["session_id"], "checkpoint": {"source": source, "representation": representation, "start": 0, "end": 0, "prefix_sha256": _EMPTY_DIGEST}, "metadata": {"source_surface": "subagent" if event == "SubagentStop" else "unknown", "source_adapter": "codex-transcript", "source_adapter_version": "1", "object_kind": "trigger"}, "state": "queued", "sequence": sequence, "event_ids": [event_id], "is_final": event == "SessionEnd", "final_event_id": event_id if event == "SessionEnd" else None, "owner": None, "lease_until": None, "lease_seconds": 60.0, "resume_state": "queued", "failure_code": None, "object_key": None, "ciphertext_sha256": None, "ciphertext_size": None, "index_id": None}
            fd, temp_name = tempfile.mkstemp(prefix=".hook.", dir=queue)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":")); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp_name, queue / f"{event_id}.json")
        return {"ok": True, "accepted": True, "event_id": event_id, "state": "queued"}
    except Exception:
        return {"ok": True, "accepted": False, "diagnostic": "capture-gap"}


def main(stream=None) -> int:
    try:
        raw = (stream or sys.stdin.buffer).read(_MAX + 1)
        if len(raw) > _MAX: return 0
        process(json.loads(raw.decode("utf-8")))
    except Exception:
        pass
    return 0
