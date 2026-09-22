"""Fail-open Codex lifecycle hook installation and runtime boundary.

The hook is intentionally a trigger-only process.  It accepts one bounded
Codex command-hook object, validates only the current upstream event schema,
checks the transcript hint through :mod:`codex_adapter`, and enqueues bounded
metadata in :mod:`pcc_outbox`.  It never opens a transcript or initializes any
credential, network, encryption, or worker subsystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, TextIO
from .adapter_contract import AdapterError
from .codex_adapter import CodexHookFacts, CodexRoots, canonicalize_hook_path
from .pcc_outbox import PccOutbox, QueueState, _exclusive_file_lock

UPSTREAM_CODEX_COMMIT = "0a73d55b80afd2aa88051848bd28524d132fd01e"
UPSTREAM_CODEX_DATE = "2026-09-22"
SUPPORTED_EVENTS = ("Stop", "SubagentStop", "SessionEnd")
MARKER_PREFIX = "# josh-room-pcc-hooks:v1:"
MARKER_SUFFIX = "# josh-room-pcc-hooks:v1:end"
MAX_HOOK_INPUT_BYTES = 64 * 1024
MAX_STRING_BYTES = 4096
_HOOK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MARKER_RE = re.compile(
    rf"(?ms)^({re.escape(MARKER_PREFIX)}(?P<event>Stop|SubagentStop|SessionEnd)\n.*?^{re.escape(MARKER_SUFFIX)}\n?)"
)
_EVENT_LABELS = {"Stop": "stop", "SubagentStop": "subagent_stop", "SessionEnd": "session_end"}
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class HookBoundaryError(ValueError):
    """Stable installation/runtime error without caller-controlled details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HookRuntimeResult:
    accepted: bool
    diagnostic: str | None = None
    event_id: str | None = None
    state: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"ok": True, "accepted": self.accepted}
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic
        if self.event_id is not None:
            result["event_id"] = self.event_id
        if self.state is not None:
            result["state"] = self.state
        return result


def _home() -> Path:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError, OSError):
        return Path.home()


def _bounded_string(value: object, *, allow_none: bool = False, identifier: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or not value or len(value.encode("utf-8", "strict")) > MAX_STRING_BYTES:
        raise HookBoundaryError("invalid-input")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise HookBoundaryError("invalid-input")
    if identifier and _HOOK_ID.fullmatch(value) is None:
        raise HookBoundaryError("invalid-input")
    return value


def _validate_path(value: object, *, allow_none: bool = False) -> str | None:
    value = _bounded_string(value, allow_none=allow_none)
    if value is not None and not Path(value).is_absolute():
        raise HookBoundaryError("invalid-input")
    return value


def _validate_event(payload: object) -> tuple[str, dict[str, object]]:
    if not isinstance(payload, dict):
        raise HookBoundaryError("invalid-input")
    event = payload.get("hook_event_name")
    if event not in SUPPORTED_EVENTS:
        raise HookBoundaryError("unsupported-event")
    fields = {
        "Stop": {
            "session_id", "turn_id", "transcript_path", "cwd", "hook_event_name",
            "model", "permission_mode", "stop_hook_active", "last_assistant_message",
        },
        "SubagentStop": {
            "session_id", "turn_id", "transcript_path", "agent_transcript_path", "cwd",
            "hook_event_name", "model", "permission_mode", "stop_hook_active", "agent_id",
            "agent_type", "last_assistant_message",
        },
        "SessionEnd": {"session_id", "transcript_path", "cwd", "hook_event_name", "reason"},
    }[event]
    if set(payload) != fields:
        raise HookBoundaryError("invalid-input")
    session_id = _bounded_string(payload.get("session_id"), identifier=True)
    _validate_path(payload.get("cwd"))
    _validate_path(payload.get("transcript_path"), allow_none=True)
    if event == "SubagentStop":
        _validate_path(payload.get("agent_transcript_path"), allow_none=True)
    if event in {"Stop", "SubagentStop"}:
        _bounded_string(payload.get("turn_id"), identifier=True)
        _bounded_string(payload.get("model"))
        _bounded_string(payload.get("permission_mode"))
        if type(payload.get("stop_hook_active")) is not bool:
            raise HookBoundaryError("invalid-input")
        _bounded_string(payload.get("last_assistant_message"), allow_none=True)
    if event == "SubagentStop":
        _bounded_string(payload.get("agent_id"), identifier=True)
        _bounded_string(payload.get("agent_type"))
    if event == "SessionEnd" and payload.get("reason") != "other":
        # The current upstream schema exposes only the literal `other` reason.
        raise HookBoundaryError("invalid-input")
    return event, payload


def _safe_roots(roots: CodexRoots | None) -> CodexRoots:
    if roots is not None:
        return roots
    active_value = os.environ.get("JOSH_ROOM_CODEX_ACTIVE_ROOT")
    archived_value = os.environ.get("JOSH_ROOM_CODEX_ARCHIVED_ROOT")
    if active_value and archived_value:
        active, archived = Path(active_value), Path(archived_value)
    else:
        codex_home_value = os.environ.get("CODEX_HOME")
        codex_home = Path(codex_home_value) if codex_home_value else _home() / ".codex"
        if not codex_home.is_absolute():
            codex_home = _home() / ".codex"
        active, archived = codex_home / "sessions", codex_home / "archived_sessions"
    if not active.is_absolute() or not archived.is_absolute():
        raise HookBoundaryError("source-not-contained")
    return CodexRoots(active, archived)


def _default_outbox_root() -> Path:
    explicit = os.environ.get("JOSH_ROOM_HOOK_OUTBOX") or os.environ.get("JOSH_ROOM_OUTBOX_ROOT")
    if explicit:
        path = Path(explicit)
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        path = (Path(state_home) if state_home else _home() / ".local" / "state") / "josh-room" / "pcc-outbox"
    if not path.is_absolute():
        raise HookBoundaryError("storage-unavailable")
    return path


def _source_hint(event: str, payload: Mapping[str, object], roots: CodexRoots | None) -> tuple[str, str]:
    transcript = payload.get("transcript_path")
    if event == "SubagentStop" and payload.get("agent_transcript_path") is not None:
        transcript = payload.get("agent_transcript_path")
    if transcript is None:
        return "codex-hook", "unknown"
    facts = CodexHookFacts(
        session_id=str(payload["session_id"]),
        cwd=str(payload["cwd"]),
        transcript_path=str(transcript),
        surface="subagent" if event == "SubagentStop" else "unknown",
        subagent_type=str(payload["agent_type"]) if event == "SubagentStop" else None,
    )
    candidate = canonicalize_hook_path(_safe_roots(roots), facts)
    if candidate is None:
        raise HookBoundaryError("source-not-contained")
    return candidate


def _event_id(event: str, payload: Mapping[str, object], source_id: str) -> str:
    stable = "|".join(
        (
            event,
            str(payload["session_id"]),
            str(payload.get("turn_id", "")),
            str(payload.get("agent_id", "")),
            source_id,
        )
    )
    return "codex-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def process_codex_hook(
    payload: object,
    *,
    outbox_root: Path | str | None = None,
    roots: CodexRoots | None = None,
) -> dict[str, object]:
    """Process one hook object and always return a fail-open public receipt."""

    try:
        event, checked = _validate_event(payload)
        source_id, representation = _source_hint(event, checked, roots)
        event_id = _event_id(event, checked, source_id)
        outbox = PccOutbox(Path(outbox_root) if outbox_root is not None else _default_outbox_root())
        receipt = outbox.enqueue(
            event_id=event_id,
            session_id=str(checked["session_id"]),
            checkpoint={
                "source": source_id,
                "representation": representation,
                "start": 0,
                "end": 0,
                "prefix_sha256": _EMPTY_DIGEST,
            },
            is_final=event == "SessionEnd",
            metadata={
                "source_surface": "subagent" if event == "SubagentStop" else "unknown",
                "source_adapter": "codex-transcript",
                "source_adapter_version": "1",
                "object_kind": "trigger",
            },
            policy_decision="local-only",
        )
        if receipt.state is QueueState.CAPTURE_GAP:
            return HookRuntimeResult(False, "capture-gap", event_id, receipt.state.value).to_dict()
        return HookRuntimeResult(True, None, event_id, receipt.state.value).to_dict()
    except (AdapterError, HookBoundaryError, OSError, RuntimeError, TypeError, ValueError):
        # Do not expose paths, JSON, exception text, or queue internals to Codex.
        return HookRuntimeResult(False, "capture-gap" if isinstance(payload, dict) else "invalid-input").to_dict()


def _read_one_json(stream: BinaryIO | TextIO) -> object:
    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "strict")
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_HOOK_INPUT_BYTES:
        raise HookBoundaryError("oversized-input")
    try:
        return json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise HookBoundaryError("invalid-input") from None


def codex_hook_main(stream: BinaryIO | TextIO | None = None, *, roots: CodexRoots | None = None, outbox_root: Path | str | None = None) -> int:
    """Dedicated non-interactive machine entry point; every outcome is exit 0."""

    try:
        payload = _read_one_json(stream or sys.stdin.buffer)
    except (HookBoundaryError, OSError, UnicodeError, RecursionError, ValueError):
        return 0
    process_codex_hook(payload, roots=roots, outbox_root=outbox_root)
    return 0


def _config_path(config_path: Path | str | None) -> Path:
    if config_path is not None:
        path = Path(config_path)
    else:
        explicit = os.environ.get("JOSH_ROOM_CODEX_CONFIG")
        path = Path(explicit) if explicit else _home() / ".codex" / "config.toml"
    if not path.is_absolute():
        raise HookBoundaryError("config-path-invalid")
    if path.exists() and path.is_symlink():
        raise HookBoundaryError("config-path-invalid")
    _validate_config_security(path)
    return path


def _validate_config_security(path: Path) -> None:
    current = path
    for _ in range(32):
        if current.is_symlink():
            raise HookBoundaryError("config-untrusted")
        if current.exists():
            try:
                info = current.lstat()
            except OSError:
                raise HookBoundaryError("config-untrusted") from None
            if hasattr(os, "getuid") and info.st_uid != os.getuid() and not (
                info.st_uid == 0
                and (
                    not (info.st_mode & 0o022)
                    or (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX)
                )
            ):
                raise HookBoundaryError("config-untrusted")
            if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX):
                raise HookBoundaryError("config-untrusted")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _config_lock(path: Path) -> Path:
    return path.parent / f".{path.name}.josh-room.lock"


def _trusted_file(path: Path) -> tuple[Path, str]:
    try:
        if path.is_symlink() or not path.is_file():
            raise HookBoundaryError("executable-untrusted")
        info = path.stat()
        if os.name != "nt" and hasattr(os, "getuid") and info.st_uid != os.getuid():
            # Root-owned system Python is trusted; writable user files are not.
            if not (info.st_uid == 0 and not (info.st_mode & 0o022)):
                raise HookBoundaryError("executable-untrusted")
        if os.name != "nt" and info.st_mode & 0o022:
            raise HookBoundaryError("executable-untrusted")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return path, digest
    except (OSError, ValueError):
        raise HookBoundaryError("executable-untrusted") from None


def _commands() -> dict[str, object]:
    interpreter, interpreter_digest = _trusted_file(Path(sys.executable).resolve())
    script, script_digest = _trusted_file(Path(__file__).with_name("hook_entrypoint.py").resolve())
    if any(any(ord(char) < 0x20 or ord(char) == 0x7F for char in str(path)) for path in (interpreter, script)):
        raise HookBoundaryError("executable-untrusted")
    posix = f"{shlex.quote(str(interpreter))} -I -S {shlex.quote(str(script))}"
    windows = f'"{str(interpreter).replace(chr(34), chr(92) + chr(34))}" -I -S "{str(script).replace(chr(34), chr(92) + chr(34))}"'
    return {
        "interpreter": str(interpreter),
        "interpreter_sha256": interpreter_digest,
        "script": str(script),
        "script_sha256": script_digest,
        "command": posix,
        "commandWindows": windows,
    }


def _render_block(event: str, commands: Mapping[str, object]) -> str:
    async_value = "true" if event != "SessionEnd" else "false"
    return (
        f"{MARKER_PREFIX}{event}\n"
        f"[[hooks.{event}]]\n"
        'matcher = "*"\n'
        f"[[hooks.{event}.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(str(commands['command']))}\n"
        f"commandWindows = {json.dumps(str(commands['commandWindows']))}\n"
        "timeout = 1\n"
        f"async = {async_value}\n"
        'statusMessage = "Josh Room local trigger"\n'
        f"{MARKER_SUFFIX}\n"
    )


def _read_config(path: Path) -> tuple[str, dict[str, object], bool]:
    if not path.exists():
        return "", {}, False
    try:
        text = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise HookBoundaryError("config-invalid") from None
    if not isinstance(parsed, dict):
        raise HookBoundaryError("config-invalid")
    return text, parsed, True


def _blocks(text: str) -> tuple[list[tuple[str, int, int]], bool]:
    found = [(match.group("event"), match.start(), match.end()) for match in _MARKER_RE.finditer(text)]
    marker_lines = [
        line for line in text.splitlines()
        if line.startswith(MARKER_PREFIX) and line != MARKER_SUFFIX
    ]
    return found, len(marker_lines) == len(found)


def _event_values(parsed: Mapping[str, object], event: str) -> list[dict[str, object]]:
    hooks = parsed.get("hooks", {})
    if not isinstance(hooks, dict):
        return []
    values = hooks.get(event, [])
    return values if isinstance(values, list) and all(isinstance(item, dict) for item in values) else []


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hook_hash(event: str, group: Mapping[str, object]) -> str:
    value = {"event_name": _EVENT_LABELS[event], **dict(group)}
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _state_for(parsed: Mapping[str, object], config_path: Path, event: str) -> tuple[str, str | None]:
    hooks = parsed.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("state"), dict):
        return "untrusted", None
    state = hooks["state"]
    groups = _event_values(parsed, event)
    for group_index, group in enumerate(groups):
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            continue
        for handler_index, handler in enumerate(handlers):
            if not isinstance(handler, dict) or handler.get("type") != "command":
                continue
            command = handler.get("command")
            if not isinstance(command, str):
                continue
            keys = (
                f"{config_path}:{_EVENT_LABELS[event]}:{group_index}:{handler_index}",
                f"{config_path.as_posix()}:{_EVENT_LABELS[event]}:{group_index}:{handler_index}",
            )
            entry = next((state[key] for key in keys if isinstance(state.get(key), dict)), None)
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is False:
                return "disabled", None
            trusted_hash = entry.get("trusted_hash")
            if isinstance(trusted_hash, str):
                current = _hook_hash(event, group)
                return ("trusted" if trusted_hash == current else "modified"), trusted_hash
    return "untrusted", None


def _owned_commands(text: str, blocks: list[tuple[str, int, int]]) -> set[str]:
    owned: set[str] = set()
    for _event, start, end in blocks:
        for line in text[start:end].splitlines():
            if line.startswith("command = "):
                try:
                    value = json.loads(line.removeprefix("command = "))
                except json.JSONDecodeError:
                    continue
                if isinstance(value, str):
                    owned.add(value)
    return owned


def _unowned_conflict(parsed: Mapping[str, object], expected_commands: set[str]) -> bool:
    for event in SUPPORTED_EVENTS:
        for group in _event_values(parsed, event):
            for handler in group.get("hooks", []) if isinstance(group.get("hooks", []), list) else []:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or command in expected_commands:
                    continue
                lowered = command.lower()
                if "josh-room" in lowered or "josh_room" in lowered:
                    return True
    return False


def _write_config(path: Path, text: str, *, existed: bool, mode: int | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise HookBoundaryError("config-path-invalid")
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, mode if mode is not None else (0o600 if not existed else stat.S_IMODE(path.stat().st_mode)))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except HookBoundaryError:
        raise
    except (OSError, ValueError):
        raise HookBoundaryError("config-write-failed") from None


def _receipt_path() -> Path:
    explicit = os.environ.get("JOSH_ROOM_HOOK_RECEIPT")
    if explicit:
        path = Path(explicit)
    else:
        state_home = os.environ.get("XDG_STATE_HOME")
        path = (Path(state_home) if state_home else _home() / ".local" / "state") / "josh-room" / "hooks-codex.json"
    if not path.is_absolute():
        raise HookBoundaryError("receipt-path-invalid")
    return path


def _write_receipt(receipt: Mapping[str, object]) -> None:
    path = _receipt_path()
    _write_config(path, json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", existed=path.exists(), mode=0o600)


def _read_receipt() -> dict[str, object] | None:
    path = _receipt_path()
    if not path.exists() or path.is_symlink():
        return None
    try:
        info = path.lstat()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        if info.st_mode & 0o022:
            return None
    except OSError:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _remove_receipt() -> None:
    try:
        _receipt_path().unlink(missing_ok=True)
    except OSError:
        raise HookBoundaryError("receipt-write-failed") from None


def _codex_hook_status_locked(config_path: Path | str | None = None) -> dict[str, object]:
    try:
        path = _config_path(config_path)
        text, parsed, exists = _read_config(path)
        commands = _commands()
        expected = {event: _render_block(event, commands) for event in SUPPORTED_EVENTS}
        blocks, markers_well_formed = _blocks(text)
        expected_commands = {str(commands["command"])} | _owned_commands(text, blocks)
        conflicts = _unowned_conflict(parsed, expected_commands)
        receipt = _read_receipt()
        receipt_commands = receipt.get("commands") if isinstance(receipt, dict) else None
        executable_stale = (
            not isinstance(receipt_commands, dict)
            or receipt_commands.get("interpreter_sha256") != commands.get("interpreter_sha256")
            or receipt_commands.get("script_sha256") != commands.get("script_sha256")
        ) if receipt is not None else False
        states: list[str] = []
        events: dict[str, object] = {}
        for event in SUPPORTED_EVENTS:
            matching = [block for block_event, start, end in blocks if block_event == event for block in [text[start:end]]]
            if not matching:
                state = "missing"
            elif len(matching) != 1 or matching[0] != expected[event]:
                state = "stale"
            else:
                state, _trusted_hash = _state_for(parsed, path, event)
            states.append(state)
            events[event] = {"state": state, "count": len(matching)}
        if not markers_well_formed:
            overall = "partial"
        elif conflicts:
            overall = "conflict"
        elif executable_stale:
            overall = "stale"
        elif any(state == "disabled" for state in states):
            overall = "disabled"
        elif any(state in {"modified", "stale"} for state in states):
            overall = "stale"
        elif all(state == "missing" for state in states):
            overall = "missing"
        elif any(state == "missing" for state in states):
            overall = "partial"
        elif all(state in {"trusted", "untrusted"} for state in states):
            # Configuration is healthy; Codex trust remains a separate gate.
            overall = "healthy"
        else:
            overall = "unsupported"
        result = {
            "ok": True,
            "tool": "codex",
            "state": overall,
            "trust": "trusted" if all(state == "trusted" for state in states) else "untrusted",
            "config_exists": exists,
            "events": events,
            "upstream_commit": UPSTREAM_CODEX_COMMIT,
            "supported_events": list(SUPPORTED_EVENTS),
            "receipt": "present" if receipt is not None else "missing",
            "diagnostics": [],
        }
        if conflicts:
            result["diagnostics"] = ["conflicting-josh-room-hook"]
        elif executable_stale:
            result["diagnostics"] = ["stale-executable"]
        elif not markers_well_formed:
            result["diagnostics"] = ["partial-installation"]
        elif any(state == "untrusted" for state in states):
            result["diagnostics"] = ["codex-trust-required"]
        return result
    except HookBoundaryError as error:
        return {"ok": False, "tool": "codex", "state": error.code, "diagnostics": [error.code]}


def codex_hook_status(config_path: Path | str | None = None) -> dict[str, object]:
    try:
        path = _config_path(config_path)
        with _exclusive_file_lock(_config_lock(path)):
            return _codex_hook_status_locked(path)
    except HookBoundaryError as error:
        return {"ok": False, "tool": "codex", "state": error.code, "diagnostics": [error.code]}


def _install_locked(config_path: Path | str | None, *, repair: bool) -> dict[str, object]:
    path = _config_path(config_path)
    text, parsed, existed = _read_config(path)
    original_text = text
    commands = _commands()
    expected = {event: _render_block(event, commands) for event in SUPPORTED_EVENTS}
    blocks, markers_well_formed = _blocks(text)
    if not markers_well_formed:
        raise HookBoundaryError("partial-installation")
    expected_commands = {str(commands["command"])} | _owned_commands(text, blocks)
    if _unowned_conflict(parsed, expected_commands):
        raise HookBoundaryError("conflicting-josh-room-hook")
    if blocks and not repair:
        if all(sum(1 for block_event, _start, _end in blocks if block_event == event) == 1 and text[next(start for block_event, start, _end in blocks if block_event == event):next(end for block_event, _start, end in blocks if block_event == event)] == expected[event] for event in SUPPORTED_EVENTS):
            return _codex_hook_status_locked(path)
        raise HookBoundaryError("stale-installation")
    if repair and blocks:
        for _event, start, end in reversed(blocks):
            text = text[:start] + text[end:]
        parsed = tomllib.loads(text) if text.strip() else {}
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    for event in SUPPORTED_EVENTS:
        text += expected[event]
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise HookBoundaryError("config-merge-failed") from None
    current_text, _current_parsed, current_exists = _read_config(path)
    if current_exists != existed or current_text != original_text:
        raise HookBoundaryError("config-conflict")
    original_sha = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
    original_mode = stat.S_IMODE(path.stat().st_mode) if existed else 0o600
    _write_config(path, text, existed=existed, mode=original_mode)
    receipt = {
        "version": 1,
        "config_sha256": original_sha,
        "config_path": str(path),
        "original_exists": existed,
        "original_mode": original_mode,
        "original_size": len(original_text.encode("utf-8")),
        "original_final_newline": original_text.endswith("\n"),
        "blocks": {event: hashlib.sha256(expected[event].encode("utf-8")).hexdigest() for event in SUPPORTED_EVENTS},
        "commands": {key: value for key, value in commands.items() if key.endswith("sha256")},
        "upstream_commit": UPSTREAM_CODEX_COMMIT,
    }
    try:
        _write_receipt(receipt)
    except Exception as error:
        try:
            if existed:
                _write_config(path, original_text, existed=True, mode=original_mode)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(error, HookBoundaryError):
            raise
        raise HookBoundaryError("receipt-write-failed") from None
    return _codex_hook_status_locked(path)


def _install(config_path: Path | str | None, *, repair: bool) -> dict[str, object]:
    path = _config_path(config_path)
    try:
        with _exclusive_file_lock(_config_lock(path)):
            return _install_locked(path, repair=repair)
    except HookBoundaryError:
        raise
    except Exception:
        raise HookBoundaryError("config-write-failed") from None


def install_codex_hooks(config_path: Path | str | None = None, *, repair: bool = False) -> dict[str, object]:
    try:
        return _install(config_path, repair=repair)
    except HookBoundaryError as error:
        return {"ok": False, "tool": "codex", "state": error.code, "diagnostics": [error.code]}


def repair_codex_hooks(config_path: Path | str | None = None) -> dict[str, object]:
    return install_codex_hooks(config_path, repair=True)


def _remove_codex_hooks_locked(config_path: Path | str | None = None) -> dict[str, object]:
    try:
        path = _config_path(config_path)
        text, parsed, exists = _read_config(path)
        blocks, well_formed = _blocks(text)
        receipt = _read_receipt()
        if not well_formed:
            raise HookBoundaryError("partial-installation")
        if not blocks:
            return _codex_hook_status_locked(path)
        if not isinstance(receipt, dict):
            raise HookBoundaryError("ownership-uncertain")
        required = {
            "version", "config_path", "original_exists", "original_mode",
            "original_size", "original_final_newline", "blocks", "commands",
        }
        if (
            receipt.get("version") != 1
            or set(receipt) < required
            or receipt.get("config_path") != str(path)
            or type(receipt.get("original_exists")) is not bool
            or type(receipt.get("original_mode")) is not int
            or type(receipt.get("original_size")) is not int
            or type(receipt.get("original_final_newline")) is not bool
        ):
            raise HookBoundaryError("ownership-uncertain")
        receipt_blocks = receipt.get("blocks")
        if not isinstance(receipt_blocks, dict) or set(receipt_blocks) != set(SUPPORTED_EVENTS):
            raise HookBoundaryError("ownership-uncertain")
        if len(blocks) != len(SUPPORTED_EVENTS) or {event for event, _start, _end in blocks} != set(SUPPORTED_EVENTS):
            raise HookBoundaryError("ownership-uncertain")
        for event, start, end in reversed(blocks):
            expected_hash = receipt_blocks.get(event)
            if (
                not isinstance(expected_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
                or hashlib.sha256(text[start:end].encode("utf-8")).hexdigest() != expected_hash
            ):
                raise HookBoundaryError("owned-hook-modified")
            text = text[:start] + text[end:]
        original_size = receipt["original_size"]
        if (
            exists
            and receipt.get("original_exists") is True
            and isinstance(original_size, int)
            and len(text.encode("utf-8")) > original_size
        ):
            extra = len(text.encode("utf-8")) - original_size
            if text.endswith("\n" * extra):
                text = text[:-extra]
        if exists and not text.strip() and receipt.get("original_exists") is False:
            path.unlink(missing_ok=True)
        else:
            tomllib.loads(text) if text.strip() else None
            _write_config(path, text, existed=exists, mode=stat.S_IMODE(path.stat().st_mode) if exists else 0o600)
        _remove_receipt()
        return _codex_hook_status_locked(path)
    except (HookBoundaryError, OSError, tomllib.TOMLDecodeError):
        return {"ok": False, "tool": "codex", "state": "remove-failed", "diagnostics": ["remove-failed"]}


def remove_codex_hooks(config_path: Path | str | None = None) -> dict[str, object]:
    try:
        path = _config_path(config_path)
        with _exclusive_file_lock(_config_lock(path)):
            return _remove_codex_hooks_locked(path)
    except HookBoundaryError as error:
        return {"ok": False, "tool": "codex", "state": error.code, "diagnostics": [error.code]}


__all__ = [
    "HookBoundaryError",
    "HookRuntimeResult",
    "MAX_HOOK_INPUT_BYTES",
    "SUPPORTED_EVENTS",
    "UPSTREAM_CODEX_COMMIT",
    "UPSTREAM_CODEX_DATE",
    "codex_hook_main",
    "codex_hook_status",
    "install_codex_hooks",
    "process_codex_hook",
    "remove_codex_hooks",
    "repair_codex_hooks",
]
