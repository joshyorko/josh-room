"""Closed policy for qualified main-session Claude native history files.

This policy preserves native message bytes for filesystem discovery only. It
does not qualify runtime resumption, tools, sidechains, or cross-file history.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime
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


def _validate_v1(relative: str, rows: list[dict], workspace: str) -> str:
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


_CHAIN_FIELDS = (_REQUIRED - {"message"}) | {"entrypoint", "gitBranch"}
_SDK_USER_FIELDS = _CHAIN_FIELDS | {"message", "permissionMode", "promptId", "promptSource", "turnOrigin"}
_SDK_ASSISTANT_FIELDS = _CHAIN_FIELDS | {
    "message", "apiBlockIndex", "effort", "perTurnEffort", "requestRef", "requestedModel",
}
_API_BETAS = [
    "claude-code-20250219", "interleaved-thinking-2025-05-14",
    "thinking-token-count-2026-05-13", "context-management-2025-06-27", "effort-2025-11-24",
]
_USAGE_COUNTERS = {
    "input_tokens": "inputTokens", "output_tokens": "outputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens", "cache_read_input_tokens": "cacheReadInputTokens",
}
_COST_FIELDS = {
    "type", "sessionId", "totalCostUSD", "totalAPIDuration", "totalAPIDurationWithoutRetries",
    "totalDuration", "totalToolDuration", "totalLinesAdded", "totalLinesRemoved", "modelUsage",
    "hasUnknownModelCost", "startTime",
}
_MODEL_USAGE_FIELDS = set(_USAGE_COUNTERS.values()) | {"costUSD", "thinkingTokens", "webSearchRequests"}


def _exact(value, fields):
    if not isinstance(value, dict) or set(value) != fields:
        _fail("claude-native-row-unsupported")


def _counter(value):
    if type(value) is not int or not 0 <= value < 2**64:
        _fail("claude-native-metadata-invalid")
    return value


def _amount(value):
    if type(value) not in {int, float} or not 0 <= value <= 10**9 or not math.isfinite(value):
        _fail("claude-native-metadata-invalid")
    return value


def _digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail("claude-native-metadata-invalid")
    return value


def _zeroes(value, fields):
    _exact(value, fields)
    if any(_counter(item) != 0 for item in value.values()):
        _fail("claude-native-metadata-invalid")


def _model(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value) is None:
        _fail("claude-native-metadata-invalid")
    return value


def _take(rows, index, kind):
    if index >= len(rows):
        _fail("claude-native-history-incomplete")
    row = rows[index]
    if not isinstance(row, dict) or row.get("type") != kind:
        _fail("claude-native-row-unsupported")
    return row


def _metadata(row, fields, session_id):
    _exact(row, fields)
    if row["sessionId"] != session_id:
        _fail("claude-native-binding-mismatch")
    if "version" in fields and row["version"] != CLAUDE_VERSION:
        _fail("claude-native-row-unsupported")
    if "timestamp" in fields:
        _timestamp(row["timestamp"])


def _chain(row, session_id, workspace, previous, seen):
    if row["version"] != CLAUDE_VERSION or row["sessionId"] != session_id or row["cwd"] != workspace:
        _fail("claude-native-binding-mismatch")
    if row["isSidechain"] is not False or row["userType"] != "external" or row["entrypoint"] != "sdk-cli":
        _fail("claude-native-row-unsupported")
    if not isinstance(row["gitBranch"], str) or len(row["gitBranch"]) > 512:
        _fail("claude-native-row-unsupported")
    identity = _uuid(row["uuid"])
    if identity in seen or row["parentUuid"] != previous:
        _fail("claude-native-ancestry-unsupported")
    _timestamp(row["timestamp"])
    seen.add(identity)
    return identity


def _shape(row, session_id, shapes):
    _metadata(row, {"type", "sessionId", "version", "timestamp", "shapeHash", "shape"}, session_id)
    shape = row["shape"]
    _exact(shape, {"system", "tools"})
    if shape["tools"] != [] or not isinstance(shape["system"], list) or not 1 <= len(shape["system"]) <= 8:
        _fail("claude-native-metadata-invalid")
    for block in shape["system"]:
        _exact(block, {"type", "text"})
        if block["type"] != "text" or not isinstance(block["text"], str) or not 1 <= len(block["text"]) <= 32768:
            _fail("claude-native-metadata-invalid")
    digest = _digest(row["shapeHash"])
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != digest or digest in shapes and shapes[digest] != shape:
        _fail("claude-native-metadata-invalid")
    shapes[digest] = shape


def _request(row, session_id, shapes, requests, message_count):
    _metadata(row, {"type", "sessionId", "version", "timestamp", "id", "shapeHash", "querySource",
                    "params", "keep", "tail", "cacheControl", "requestDigest"}, session_id)
    identity = _uuid(row["id"])
    if identity in requests or row["querySource"] != "sdk" or type(row["keep"]) is not int or row["keep"] != 0:
        _fail("claude-native-metadata-invalid")
    requests.add(identity)
    shape = shapes.get(_digest(row["shapeHash"]))
    if shape is None:
        _fail("claude-native-metadata-invalid")
    _digest(row["requestDigest"])
    if not isinstance(row["tail"], list) or len(row["tail"]) != message_count:
        _fail("claude-native-metadata-invalid")
    for digest in row["tail"]:
        _digest(digest)
    expected_cache = [{"path": ["system", index], "value": {"type": "ephemeral"}}
                      for index in range(len(shape["system"]))]
    expected_cache.append({"path": ["messages", message_count - 1, "content", 0], "value": {"type": "ephemeral"}})
    if json.dumps(row["cacheControl"], sort_keys=True) != json.dumps(expected_cache, sort_keys=True):
        _fail("claude-native-metadata-invalid")
    params = row["params"]
    _exact(params, {"model", "max_tokens", "betas", "context_management", "thinking", "output_config"})
    if (not 1 <= _counter(params["max_tokens"]) <= 200000 or params["betas"] != _API_BETAS
            or params["context_management"] != {"edits": [{"keep": "all", "type": "clear_thinking_20251015"}]}
            or params["thinking"] != {"display": "omitted", "type": "adaptive"}
            or params["output_config"] != {"effort": "high"}):
        _fail("claude-native-metadata-invalid")
    return _model(params["model"])


def _sdk_message(row, model, assistant_ids, totals):
    message = row["message"]
    _exact(message, _ASSISTANT_REQUIRED | {"stop_reason", "stop_sequence", "stop_details", "usage"})
    if (message["type"] != "message" or message["role"] != "assistant" or message["model"] != model
            or message["stop_reason"] != "end_turn" or message["stop_sequence"] is not None
            or message["stop_details"] is not None):
        _fail("claude-native-message-unsupported")
    if not isinstance(message["id"], str) or not 1 <= len(message["id"]) <= 256 or message["id"] in assistant_ids:
        _fail("claude-native-message-unsupported")
    assistant_ids.add(message["id"])
    _content(message["content"])
    usage = message["usage"]
    _exact(usage, set(_USAGE_COUNTERS) | {"cache_creation", "fallback_credit", "inference_geo", "iterations",
                                        "output_tokens_details", "server_tool_use", "service_tier", "speed"})
    if (usage["fallback_credit"] is not None or usage["inference_geo"] != "" or usage["iterations"] != []
            or usage["service_tier"] != "standard" or usage["speed"] != "standard"):
        _fail("claude-native-message-unsupported")
    _zeroes(usage["output_tokens_details"], {"thinking_tokens"})
    _zeroes(usage["server_tool_use"], {"web_fetch_requests", "web_search_requests"})
    _exact(usage["cache_creation"], {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"})
    for value in usage["cache_creation"].values():
        _counter(value)
    if sum(usage["cache_creation"].values()) != usage["cache_creation_input_tokens"]:
        _fail("claude-native-message-unsupported")
    counts = totals.setdefault(model, dict.fromkeys(_USAGE_COUNTERS.values(), 0))
    for native_name, cost_name in _USAGE_COUNTERS.items():
        counts[cost_name] += _counter(usage[native_name])
        _counter(counts[cost_name])


def _cost(row, session_id, totals):
    _metadata(row, _COST_FIELDS, session_id)
    if (type(row["hasUnknownModelCost"]) is not bool or not isinstance(row["modelUsage"], dict)
            or set(row["modelUsage"]) != set(totals)):
        _fail("claude-native-metadata-invalid")
    for name in ("startTime", "totalAPIDuration", "totalAPIDurationWithoutRetries", "totalDuration",
                 "totalToolDuration", "totalLinesAdded", "totalLinesRemoved"):
        _counter(row[name])
    if (not row["startTime"] or row["totalToolDuration"] != 0 or row["totalLinesAdded"] != 0
            or row["totalLinesRemoved"] != 0
            or not row["totalAPIDurationWithoutRetries"] <= row["totalAPIDuration"] <= row["totalDuration"]):
        _fail("claude-native-metadata-invalid")
    total = 0
    for model, values in row["modelUsage"].items():
        _model(model)
        _exact(values, _MODEL_USAGE_FIELDS)
        if (_counter(values["thinkingTokens"]) != 0 or _counter(values["webSearchRequests"]) != 0
                or any(type(values[name]) is not int or values[name] != expected for name, expected in totals[model].items())):
            _fail("claude-native-metadata-invalid")
        total += _amount(values["costUSD"])
    if not math.isclose(_amount(row["totalCostUSD"]), total, rel_tol=1e-9, abs_tol=1e-12):
        _fail("claude-native-metadata-invalid")


def _validate_v2(relative, rows, workspace):
    # Only complete native tool-free SDK turns are admitted. The grammar keeps
    # queue journals and diagnostic metadata intact rather than filtering rows.
    if not isinstance(rows, list) or len(rows) < 2:
        _fail("claude-native-history-incomplete")
    prefix = 0
    while prefix < len(rows):
        row = rows[prefix]
        if not isinstance(row, dict) or row.get("type") not in {"user", "assistant"} or not _REQUIRED <= set(row) <= _TOP_FIELDS:
            break
        prefix += 1
    if prefix:
        session_id = _validate_v1(relative, rows[:prefix], workspace)
    else:
        key = project_key(workspace)
        path = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath()
        if len(path.parts) != 3 or path.parts[:2] != ("projects", key) or str(path) != relative or not path.name.endswith(".jsonl"):
            _fail("claude-native-path-unsupported")
        session_id = _uuid(path.name[:-6])
    if prefix == len(rows):
        return session_id
    seen = {row["uuid"] for row in rows[:prefix]}
    previous = rows[prefix - 1]["uuid"] if prefix else None
    assistant_ids = {row["message"]["id"] for row in rows[:prefix] if row["type"] == "assistant"}
    if len(assistant_ids) != sum(row["type"] == "assistant" for row in rows[:prefix]):
        _fail("claude-native-message-unsupported")
    shapes, requests, totals = {}, set(), {}
    index, message_count = prefix, prefix
    while index < len(rows):
        if isinstance(rows[index], dict) and rows[index].get("type") == "atis-latch":
            row = rows[index]
            _metadata(row, {"type", "sessionId", "atis"}, session_id)
            if row["atis"] != "":
                _fail("claude-native-metadata-invalid")
            index += 1
        queued = _take(rows, index, "queue-operation")
        _metadata(queued, {"type", "operation", "sessionId", "timestamp", "content"}, session_id)
        if queued["operation"] != "enqueue" or not isinstance(queued["content"], str) or not queued["content"]:
            _fail("claude-native-pending-work-unsupported")
        dequeued = _take(rows, index + 1, "queue-operation")
        _metadata(dequeued, {"type", "operation", "sessionId", "timestamp"}, session_id)
        if dequeued["operation"] != "dequeue":
            _fail("claude-native-pending-work-unsupported")
        user = _take(rows, index + 2, "user")
        _exact(user, _SDK_USER_FIELDS)
        previous = _chain(user, session_id, workspace, previous, seen)
        _uuid(user["promptId"])
        if user["permissionMode"] != "dontAsk" or user["promptSource"] != "sdk" or user["turnOrigin"] != "sdk":
            _fail("claude-native-row-unsupported")
        _message(user["message"], "user")
        if user["message"]["content"] != queued["content"]:
            _fail("claude-native-pending-work-unsupported")
        context = _take(rows, index + 3, "attachment")
        _exact(context, _CHAIN_FIELDS | {"attachment"})
        previous = _chain(context, session_id, workspace, previous, seen)
        if context["attachment"] != {"type": "session_context", "context": {}}:
            _fail("claude-native-row-unsupported")
        reminder = _take(rows, index + 4, "attachment")
        _exact(reminder, _CHAIN_FIELDS | {"attachment", "rendered", "renderedRole"})
        previous = _chain(reminder, session_id, workspace, previous, seen)
        _exact(reminder["attachment"], {"type", "date"})
        day = reminder["attachment"]["date"]
        if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
            _fail("claude-native-row-unsupported")
        try:
            date.fromisoformat(day)
        except ValueError:
            _fail("claude-native-row-unsupported")
        if (reminder["attachment"]["type"] != "date" or reminder["renderedRole"] != "system"
                or reminder["rendered"] != [{"content": f"<system-reminder>\nToday's date is {day}.\n</system-reminder>"}]):
            _fail("claude-native-row-unsupported")
        index += 5
        published_shape = None
        if index < len(rows) and isinstance(rows[index], dict) and rows[index].get("type") == "api-request-shape":
            _shape(rows[index], session_id, shapes)
            published_shape = rows[index]["shapeHash"]
            index += 1
        request = _take(rows, index, "api-request")
        model = _request(request, session_id, shapes, requests, message_count + 1)
        if published_shape is not None and request["shapeHash"] != published_shape:
            _fail("claude-native-metadata-invalid")
        assistant = _take(rows, index + 1, "assistant")
        _exact(assistant, _SDK_ASSISTANT_FIELDS)
        previous = _chain(assistant, session_id, workspace, previous, seen)
        if (type(assistant["apiBlockIndex"]) is not int or assistant["apiBlockIndex"] != 0
                or assistant["effort"] != "high" or assistant["perTurnEffort"] is not None
                or assistant["requestRef"] != request["id"] or assistant["requestedModel"] != model):
            _fail("claude-native-metadata-invalid")
        _sdk_message(assistant, model, assistant_ids, totals)
        last = _take(rows, index + 2, "last-prompt")
        _metadata(last, {"type", "sessionId", "lastPrompt", "leafUuid"}, session_id)
        if last["lastPrompt"] != queued["content"] or last["leafUuid"] != previous:
            _fail("claude-native-metadata-invalid")
        mode = _take(rows, index + 3, "mode")
        _metadata(mode, {"type", "sessionId", "mode"}, session_id)
        if mode["mode"] != "normal":
            _fail("claude-native-metadata-invalid")
        _cost(_take(rows, index + 4, "cost-state"), session_id, totals)
        message_count += 2
        index += 5
    return session_id


def validate(relative: str, rows: list[dict], workspace: str, *, policy_version: int = 2) -> str:
    """Validate a closed versioned policy without changing any native row."""
    if type(policy_version) is not int or policy_version not in {1, 2}:
        _fail("claude-native-policy-unsupported")
    if policy_version == 1:
        return _validate_v1(relative, rows, workspace)
    return _validate_v2(relative, rows, workspace)
