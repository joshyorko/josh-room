"""Qualified Claude native structure and rejected cross-file/runtime dependencies."""

from __future__ import annotations

import copy

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
