"""Closed policy for qualified main-session Claude native history files.

This policy preserves native message bytes for filesystem discovery only. It
does not qualify runtime resumption, tools, sidechains, or cross-file history.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import PurePosixPath

CLAUDE_VERSION = "2.1.310"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_REQUIRED = {
    "parentUuid", "isSidechain", "userType", "cwd", "sessionId", "version",
    "timestamp", "type", "message", "uuid",
}
_TOP_FIELDS = _REQUIRED | {"gitBranch"}
_ASSISTANT_REQUIRED = {"id", "type", "role", "model", "content"}
_ASSISTANT_FIELDS = _ASSISTANT_REQUIRED | {"stop_reason", "stop_sequence", "usage"}


class ClaudeHistoryError(ValueError):
    """A bounded reason code without workspace or conversation contents."""


def _fail(code):
    raise ClaudeHistoryError(code)


def project_key(workspace: str) -> str:
    """Return the qualified short ASCII native project key without filesystem IO."""
    if not isinstance(workspace, str) or not workspace or any(not 32 <= ord(char) < 127 for char in workspace):
        _fail("claude-workspace-unsupported")
    path = PurePosixPath(workspace)
    if not path.is_absolute() or workspace.startswith("//") or workspace == "/" or ".." in path.parts or str(path) != workspace:
        _fail("claude-workspace-unsupported")
    key = re.sub(r"[^A-Za-z0-9]", "-", workspace)
    if len(key) > 200:
        _fail("claude-workspace-key-unqualified")
    return key


def _uuid(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value) or str(uuid.UUID(value)) != value:
        _fail("claude-native-identity-invalid")
    return value


def _timestamp(value):
    if not isinstance(value, str):
        _fail("claude-native-timestamp-invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ClaudeHistoryError("claude-native-timestamp-invalid") from None
    if parsed.utcoffset() is None:
        _fail("claude-native-timestamp-invalid")


def _content(value):
    if isinstance(value, str):
        if not value:
            _fail("claude-native-message-unsupported")
        return
    if not isinstance(value, list) or not value:
        _fail("claude-native-message-unsupported")
    for block in value:
        if not isinstance(block, dict) or set(block) != {"type", "text"} or block["type"] != "text" or not isinstance(block["text"], str) or not block["text"]:
            _fail("claude-native-message-unsupported")


def _message(message, role):
    if not isinstance(message, dict) or message.get("role") != role:
        _fail("claude-native-message-unsupported")
    if role == "user":
        if set(message) != {"role", "content"}:
            _fail("claude-native-message-unsupported")
    else:
        if not _ASSISTANT_REQUIRED <= set(message) <= _ASSISTANT_FIELDS or message["type"] != "message":
            _fail("claude-native-message-unsupported")
        if any(not isinstance(message[field], str) or not 1 <= len(message[field]) <= 256 for field in ("id", "model")):
            _fail("claude-native-message-unsupported")
        if "stop_reason" in message and message["stop_reason"] != "end_turn":
            _fail("claude-native-message-incomplete")
        if message.get("stop_sequence") is not None:
            _fail("claude-native-message-unsupported")
        if "usage" in message:
            usage = message["usage"]
            required = {"input_tokens", "output_tokens"}
            optional_counters = {"cache_creation_input_tokens", "cache_read_input_tokens"}
            if not isinstance(usage, dict) or not required <= set(usage) <= required | optional_counters | {"cache_creation"}:
                _fail("claude-native-message-unsupported")
            for name in required | (optional_counters & set(usage)):
                value = usage[name]
                if name in optional_counters and value is None:
                    continue
                if type(value) is not int or not 0 <= value < 2**64:
                    _fail("claude-native-message-unsupported")
            cache = usage.get("cache_creation")
            if cache is not None and (not isinstance(cache, dict) or set(cache) != {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"} or any(type(value) is not int or not 0 <= value < 2**64 for value in cache.values())):
                _fail("claude-native-message-unsupported")
    _content(message["content"])


def validate(relative: str, rows: list[dict], workspace: str) -> str:
    """Validate the qualified linear main-session subset and return its native ID."""
    key = project_key(workspace)
    if not isinstance(relative, str):
        _fail("claude-native-path-unsupported")
    path = PurePosixPath(relative)
    if len(path.parts) != 3 or path.parts[:2] != ("projects", key) or not path.name.endswith(".jsonl") or str(path) != relative:
        _fail("claude-native-path-unsupported")
    session_id = _uuid(path.name[:-6])
    if not isinstance(rows, list) or len(rows) < 2:
        _fail("claude-native-history-incomplete")
    previous = None
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not _REQUIRED <= set(row) <= _TOP_FIELDS:
            _fail("claude-native-row-unsupported")
        if row["version"] != CLAUDE_VERSION or row["type"] not in {"user", "assistant"}:
            _fail("claude-native-row-unsupported")
        if row["sessionId"] != session_id or row["cwd"] != workspace:
            _fail("claude-native-binding-mismatch")
        if row["isSidechain"] is not False or row["userType"] != "external":
            _fail("claude-native-row-unsupported")
        if "gitBranch" in row and (not isinstance(row["gitBranch"], str) or len(row["gitBranch"]) > 512):
            _fail("claude-native-row-unsupported")
        identity = _uuid(row["uuid"])
        if identity in seen or row["parentUuid"] != previous:
            _fail("claude-native-ancestry-unsupported")
        if not index and row["type"] != "user":
            _fail("claude-native-history-incomplete")
        _timestamp(row["timestamp"])
        _message(row["message"], row["type"])
        seen.add(identity)
        previous = identity
    if rows[-1]["type"] != "assistant":
        _fail("claude-native-history-incomplete")
    return session_id
