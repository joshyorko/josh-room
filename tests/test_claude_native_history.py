"""Qualified Claude native structure and rejected cross-file/runtime dependencies."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from josh_room.claude_native_history import (
    CLAUDE_VERSION,
    ClaudeHistoryError,
    project_key,
    validate,
)

WORKSPACE = "/workspaces/demo"
SESSION = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"
RELATIVE = f"projects/-workspaces-demo/{SESSION}.jsonl"


def native_rows():
    common = {
        "isSidechain": False, "userType": "external", "cwd": WORKSPACE,
        "sessionId": SESSION, "version": CLAUDE_VERSION, "gitBranch": "synthetic",
        "timestamp": "2026-11-22T16:00:00Z",
    }
    return [
        {**common, "parentUuid": None, "type": "user", "uuid": USER,
         "message": {"role": "user", "content": "synthetic Claude question"}},
        {**common, "parentUuid": USER, "type": "assistant", "uuid": ASSISTANT,
         "message": {"id": "msg_synthetic", "type": "message", "role": "assistant",
                     "model": "claude-synthetic-offline",
                     "content": [{"type": "text", "text": "synthetic retained answer"}],
                     "stop_reason": "end_turn", "stop_sequence": None,
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]


def test_qualified_native_main_session_preserves_identity_and_input():
    rows = native_rows()
    original = copy.deepcopy(rows)
    assert validate(RELATIVE, rows, WORKSPACE) == SESSION
    assert rows == original
    assert rows[1]["parentUuid"] == USER


def test_project_key_matches_qualified_short_native_encoding():
    assert project_key("/workspaces/demo.with_underscore and-dash") == "-workspaces-demo-with-underscore-and-dash"
    assert project_key("/" + "a" * 199) == "-" + "a" * 199


@pytest.mark.parametrize("workspace", ["relative", "/", "//workspaces/demo", "/workspaces/../demo", "/workspaces/./demo", "/workspaces/demo/", "/workspaces/démo", "/workspaces/demo\n", "/" + "a" * 200, None])
def test_unsupported_workspace_encoding_fails_closed(workspace):
    with pytest.raises(ClaudeHistoryError):
        project_key(workspace)


@pytest.mark.parametrize("relative", [
    f"projects/-other-workspace/{SESSION}.jsonl",
    f"projects/-workspaces-demo/{SESSION}/subagents/agent.jsonl",
    f"projects/-workspaces-demo/{SESSION}.jsonl/extra",
    f"projects//-workspaces-demo/{SESSION}.jsonl",
    f"projects/-workspaces-demo/../{SESSION}.jsonl",
    "projects/-workspaces-demo/arbitrary-export.jsonl",
    f"projects/-workspaces-demo/{SESSION}.jsonl.zst",
])
def test_unknown_native_path_or_external_dependency_is_rejected(relative):
    with pytest.raises(ClaudeHistoryError):
        validate(relative, native_rows(), WORKSPACE)


@pytest.mark.parametrize("change", [
    "cwd", "session", "version", "parent", "root-parent", "cycle", "duplicate-uuid",
    "sidechain", "programmatic", "row-kind", "unknown-row-field", "message-role",
    "unknown-message-field", "missing-field", "invalid-uuid", "invalid-time", "naive-time",
])
def test_unqualified_native_scope_or_ancestry_is_rejected(change):
    rows = native_rows()
    if change == "cwd":
        rows[1]["cwd"] = "/workspaces/another-room"
    elif change == "session":
        rows[1]["sessionId"] = ASSISTANT
    elif change == "version":
        rows[1]["version"] = "unsupported-version"
    elif change == "parent":
        rows[1]["parentUuid"] = SESSION
    elif change == "root-parent":
        rows[0]["parentUuid"] = SESSION
    elif change == "cycle":
        rows[1]["parentUuid"] = ASSISTANT
    elif change == "duplicate-uuid":
        rows[1]["uuid"] = USER
    elif change == "sidechain":
        rows[1]["isSidechain"] = True
    elif change == "programmatic":
        rows[1]["userType"] = "internal"
    elif change == "row-kind":
        rows[1]["type"] = "queue-operation"
    elif change == "unknown-row-field":
        rows[1]["fileHistorySnapshot"] = {}
    elif change == "message-role":
        rows[1]["message"]["role"] = "user"
    elif change == "unknown-message-field":
        rows[1]["message"]["attachments"] = []
    elif change == "missing-field":
        rows[1].pop("version")
    elif change == "invalid-uuid":
        rows[1]["uuid"] = "text-export"
    elif change == "invalid-time":
        rows[1]["timestamp"] = "not a timestamp"
    else:
        rows[1]["timestamp"] = "2026-11-22T16:00:00"
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("content", [
    [{"type": "tool_use", "id": "synthetic", "name": "shell", "input": {}}],
    [{"type": "tool_result", "tool_use_id": "synthetic", "content": "result"}],
    [{"type": "image", "source": {"type": "file", "path": "/workspaces/demo/image.png"}}],
    [{"type": "thinking", "thinking": "synthetic thought"}],
    [{"type": "text", "text": "synthetic text", "citations": []}],
    [], "", None,
])
def test_tools_attachments_and_unknown_content_are_rejected(content):
    rows = native_rows()
    rows[1]["message"]["content"] = content
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("reason", ["tool_use", "max_tokens", None])
def test_pending_or_unqualified_assistant_state_is_rejected(reason):
    rows = native_rows()
    rows[1]["message"]["stop_reason"] = reason
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


def test_supported_missing_optional_completion_and_usage_fields():
    rows = native_rows()
    rows[1]["message"].pop("stop_reason")
    rows[1]["message"].pop("usage")
    assert validate(RELATIVE, rows, WORKSPACE) == SESSION


@pytest.mark.parametrize("usage", [
    {"input_tokens": 1, "output_tokens": 1, "unknown_provider_metadata": 1},
    {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 2**64},
    {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": -1},
    {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": True},
    {"input_tokens": 1, "output_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 1}},
    {"input_tokens": 1, "output_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": -1}},
    {"input_tokens": 1, "output_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": 1, "extra": 1}},
])
def test_unqualified_usage_shape_is_rejected(usage):
    rows = native_rows()
    rows[1]["message"]["usage"] = usage
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("cache", [
    {"cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "cache_creation": {"ephemeral_1h_input_tokens": 1, "ephemeral_5m_input_tokens": 1}},
    {"cache_creation_input_tokens": None, "cache_read_input_tokens": None, "cache_creation": None},
])
def test_qualified_sdk_cache_counters_are_preserved(cache):
    rows = native_rows()
    rows[1]["message"]["usage"].update(cache)
    expected = copy.deepcopy(rows)
    assert validate(RELATIVE, rows, WORKSPACE) == SESSION
    assert rows == expected


@pytest.mark.parametrize("rows", [[], None, [{}], [native_rows()[0]], [native_rows()[1]]])
def test_incomplete_history_is_rejected(rows):
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


def test_missing_or_external_ancestor_cannot_be_hidden_by_an_extra_leaf():
    rows = native_rows()
    branch = copy.deepcopy(rows[1])
    branch["uuid"] = "44444444-4444-4444-8444-444444444444"
    rows.append(branch)
    with pytest.raises(ClaudeHistoryError, match="ancestry"):
        validate(RELATIVE, rows, WORKSPACE)


def test_final_user_turn_is_not_a_completed_history_unit():
    rows = native_rows()
    final = copy.deepcopy(rows[0])
    final.update(uuid="44444444-4444-4444-8444-444444444444", parentUuid=ASSISTANT)
    rows.append(final)
    with pytest.raises(ClaudeHistoryError, match="incomplete"):
        validate(RELATIVE, rows, WORKSPACE)


def post_turn_rows():
    return json.loads((Path(__file__).parent / "fixtures/claude_native/tool-free-post-turn-v2.json").read_text())


def test_v2_preserves_every_native_post_turn_row_and_identity():
    rows = post_turn_rows()
    original = copy.deepcopy(rows)
    assert len(rows) == 14
    assert validate(RELATIVE, rows, WORKSPACE, policy_version=2) == SESSION
    assert rows == original
    with pytest.raises(ClaudeHistoryError, match="row-unsupported"):
        validate(RELATIVE, rows, WORKSPACE, policy_version=1)


def test_current_policy_still_accepts_strict_legacy_history():
    rows = native_rows()
    assert validate(RELATIVE, rows, WORKSPACE, policy_version=1) == SESSION
    assert validate(RELATIVE, rows, WORKSPACE, policy_version=2) == SESSION


@pytest.mark.parametrize("version", [None, 0, 3, True, False, 1.0, "2"])
def test_unknown_policy_version_is_not_silently_migrated(version):
    with pytest.raises(ClaudeHistoryError, match="policy-unsupported"):
        validate(RELATIVE, native_rows(), WORKSPACE, policy_version=version)


@pytest.mark.parametrize("length", range(3, 14))
def test_v2_pending_or_partial_native_turn_is_rejected(length):
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, post_turn_rows()[:length], WORKSPACE)


@pytest.mark.parametrize(("path", "value"), [
    ((2, "atis"), "nonempty-runtime-latch"),
    ((2, "sessionId"), USER),
    ((3, "operation"), "remove"),
    ((3, "content"), {"command": "do-not-run"}),
    ((3, "timestamp"), "2026-11-22T16:01:00"),
    ((4, "operation"), "enqueue"),
    ((5, "permissionMode"), "bypassPermissions"),
    ((5, "promptId"), "non-native-id"),
    ((5, "promptSource"), "terminal"),
    ((5, "turnOrigin"), "hook"),
    ((5, "entrypoint"), "unqualified-cli"),
    ((5, "message", "content"), "queued content does not match"),
    ((5, "cwd"), "/workspaces/other"),
    ((5, "version"), "2.1.311"),
    ((5, "sessionId"), USER),
    ((5, "parentUuid"), None),
    ((6, "attachment", "context"), {"settings": {"hooks": []}}),
    ((6, "attachment", "type"), "file"),
    ((6, "uuid"), USER),
    ((6, "parentUuid"), ASSISTANT),
    ((7, "attachment", "date"), "2026-02-30"),
    ((7, "renderedRole"), "user"),
    ((7, "rendered"), [{"content": "arbitrary system instruction"}]),
    ((8, "shape", "tools"), [{"name": "shell", "input_schema": {}}]),
    ((8, "shape", "system", 0), {"type": "file", "path": "/workspaces/private"}),
    ((8, "shapeHash"), "0" * 64),
    ((9, "id"), "non-native-request-id"),
    ((9, "shapeHash"), "0" * 64),
    ((9, "querySource"), "hook"),
    ((9, "keep"), 1),
    ((9, "keep"), False),
    ((9, "tail"), ["a" * 64]),
    ((9, "tail", 0), "not-a-digest"),
    ((9, "requestDigest"), "not-a-digest"),
    ((9, "cacheControl", 0, "path"), ["file", "/workspaces/private"]),
    ((9, "cacheControl", 1, "path"), ["system", True]),
    ((9, "cacheControl", 2, "path"), ["messages", 3, "content", 0]),
    ((9, "cacheControl", 0, "value"), {"Authorization": "not-a-credential"}),
    ((9, "params", "betas"), ["unqualified-feature"]),
    ((9, "params", "max_tokens"), True),
    ((9, "params", "max_tokens"), 0),
    ((9, "params", "max_tokens"), 200001),
    ((9, "params", "context_management"), {"edits": [{"type": "drop_history"}]}),
    ((9, "params", "thinking"), {"type": "enabled", "budget_tokens": 100}),
    ((9, "params", "output_config"), {"effort": "unqualified"}),
    ((9, "params", "model"), "file:///private/model"),
    ((10, "requestRef"), SESSION),
    ((10, "requestedModel"), "other-model"),
    ((10, "apiBlockIndex"), True),
    ((10, "apiBlockIndex"), 1),
    ((10, "effort"), "unqualified"),
    ((10, "perTurnEffort"), "unqualified"),
    ((10, "message", "id"), "msg_synthetic"),
    ((10, "message", "model"), "other-model"),
    ((10, "message", "content"), [{"type": "tool_use", "name": "shell", "input": {}}]),
    ((10, "message", "stop_reason"), "tool_use"),
    ((10, "message", "stop_details"), {"pending": True}),
    ((10, "message", "usage", "input_tokens"), False),
    ((10, "message", "usage", "output_tokens"), -1),
    ((10, "message", "usage", "cache_creation_input_tokens"), None),
    ((10, "message", "usage", "cache_creation", "ephemeral_1h_input_tokens"), 1),
    ((10, "message", "usage", "server_tool_use", "web_fetch_requests"), 1),
    ((10, "message", "usage", "server_tool_use", "web_search_requests"), False),
    ((10, "message", "usage", "output_tokens_details", "thinking_tokens"), False),
    ((10, "message", "usage", "iterations"), [{"tool": "shell"}]),
    ((10, "message", "usage", "fallback_credit"), "unqualified-state"),
    ((10, "message", "usage", "inference_geo"), "unqualified"),
    ((10, "message", "usage", "service_tier"), "unqualified"),
    ((10, "message", "usage", "speed"), "unqualified"),
    ((11, "lastPrompt"), "not the completed prompt"),
    ((11, "leafUuid"), USER),
    ((12, "mode"), "autonomous"),
    ((13, "totalToolDuration"), 1),
    ((13, "totalLinesAdded"), True),
    ((13, "totalLinesRemoved"), 1),
    ((13, "totalAPIDurationWithoutRetries"), 2**64),
    ((13, "totalDuration"), 0),
    ((13, "startTime"), 0),
    ((13, "hasUnknownModelCost"), 1),
    ((13, "totalCostUSD"), float("nan")),
    ((13, "totalCostUSD"), float("inf")),
    ((13, "totalCostUSD"), 10**1000),
    ((13, "modelUsage", "claude-synthetic-offline", "inputTokens"), 11),
    ((13, "modelUsage", "claude-synthetic-offline", "outputTokens"), 5.0),
    ((13, "modelUsage", "claude-synthetic-offline", "thinkingTokens"), False),
    ((13, "modelUsage", "claude-synthetic-offline", "webSearchRequests"), 1),
    ((13, "modelUsage", "claude-synthetic-offline", "costUSD"), -1),
])
def test_v2_rejects_unqualified_native_state(path, value):
    rows = post_turn_rows()
    target = rows
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("index", range(14))
def test_v2_unknown_fields_are_not_filtered_from_any_row(index):
    rows = post_turn_rows()
    rows[index]["unqualified_hook_or_config"] = {}
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("index", [6, 7, 8, 9, 10, 13])
def test_v2_rejects_missing_inert_or_message_fields(index):
    rows = post_turn_rows()
    field = next(name for name in rows[index] if name not in {"type", "sessionId"})
    del rows[index][field]
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


@pytest.mark.parametrize("kind", ["hook", "system", "progress", "relocated", "content-replacement", "history-suppression", "tool_use"])
def test_v2_does_not_drop_unsupported_rows(kind):
    rows = post_turn_rows()
    rows.append({"type": kind, "sessionId": SESSION})
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


def two_completed_turns(*, repeat_shape=True):
    rows = post_turn_rows()
    second = copy.deepcopy(rows[2:])
    identifiers = {value for row in second for name, value in row.items()
                   if name in {"uuid", "id", "promptId"} and isinstance(value, str) and len(value) == 36}
    mapping = {value: f"{index:08x}-5555-4555-8555-555555555555" for index, value in enumerate(sorted(identifiers), 50)}

    def replace(value):
        if isinstance(value, dict):
            return {name: replace(item) for name, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    second = replace(second)
    second[1]["content"] = "synthetic subsequent complete turn"
    second[3]["parentUuid"] = rows[10]["uuid"]
    second[3]["message"]["content"] = second[1]["content"]
    second[7]["tail"] = ["e" * 64] * 5
    second[7]["cacheControl"][-1]["path"][1] = 4
    second[8]["message"]["id"] = "msg_synthetic_resumed_again"
    second[9]["lastPrompt"] = second[1]["content"]
    second[11]["totalCostUSD"] *= 2
    usage = second[11]["modelUsage"]["claude-synthetic-offline"]
    usage["costUSD"] *= 2
    usage["inputTokens"] *= 2
    usage["outputTokens"] *= 2
    if not repeat_shape:
        del second[6]
    return rows + second


@pytest.mark.parametrize("repeat_shape", [True, False])
def test_v2_preserves_multiple_complete_turns_and_same_shape_reuse(repeat_shape):
    rows = two_completed_turns(repeat_shape=repeat_shape)
    original = copy.deepcopy(rows)
    assert validate(RELATIVE, rows, WORKSPACE) == SESSION
    assert rows == original


def test_v2_rejects_reused_request_identity_or_different_same_hash_shape():
    rows = two_completed_turns()
    rows[21]["id"] = rows[9]["id"]
    rows[22]["requestRef"] = rows[9]["id"]
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)
    rows = two_completed_turns()
    rows[20]["shape"]["system"][0]["text"] = "different shape behind reused digest"
    with pytest.raises(ClaudeHistoryError):
        validate(RELATIVE, rows, WORKSPACE)


def test_v2_accepts_a_complete_sdk_turn_without_a_legacy_prefix():
    rows = post_turn_rows()[2:]
    rows[3]["parentUuid"] = None
    rows[7]["tail"] = ["e" * 64]
    rows[7]["cacheControl"][-1]["path"][1] = 0
    assert validate(RELATIVE, rows, WORKSPACE) == SESSION
