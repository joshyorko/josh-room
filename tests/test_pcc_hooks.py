from __future__ import annotations

import json
from pathlib import Path

from josh_room.codex_adapter import CodexRoots
from josh_room.pcc_hooks import (
    codex_hook_status,
    install_codex_hooks,
    process_codex_hook,
    remove_codex_hooks,
)


def _fixture(tmp_path: Path) -> tuple[dict, CodexRoots]:
    active = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    source = active / "2026" / "09" / "22"
    source.mkdir(parents=True)
    archived.mkdir()
    transcript = source / "rollout-session-1.jsonl"
    transcript.write_text('{"session_id":"session-1"}\n', encoding="utf-8")
    return (
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "transcript_path": str(transcript),
            "cwd": str(tmp_path),
            "hook_event_name": "Stop",
            "model": "gpt",
            "permission_mode": "default",
            "stop_hook_active": False,
            "last_assistant_message": None,
        },
        CodexRoots(active, archived),
    )


def test_runtime_is_bounded_and_deduplicates_without_transcript_read(tmp_path):
    payload, roots = _fixture(tmp_path)
    first = process_codex_hook(payload, outbox_root=tmp_path / "outbox", roots=roots)
    second = process_codex_hook(payload, outbox_root=tmp_path / "outbox", roots=roots)
    assert first["accepted"] is True
    assert second["event_id"] == first["event_id"]
    assert len(list((tmp_path / "outbox" / "queue").glob("*.json"))) == 1


def test_runtime_rejects_path_escape_and_malformed_closed_input(tmp_path):
    payload, roots = _fixture(tmp_path)
    payload["transcript_path"] = str(tmp_path / "outside" / "rollout-session-1.jsonl")
    assert process_codex_hook(payload, outbox_root=tmp_path / "outbox", roots=roots)["accepted"] is False
    assert process_codex_hook({"hook_event_name": "Stop"}, outbox_root=tmp_path / "outbox")["accepted"] is False


def test_install_preserves_unrelated_hooks_and_remove_rolls_back(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    wrong_config = tmp_path / "wrong.toml"
    original = 'model = "synthetic"\n\n[[hooks.PreToolUse]]\nmatcher = "Bash"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "echo unrelated"\n'
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("JOSH_ROOM_CODEX_CONFIG", str(wrong_config))
    monkeypatch.setenv("JOSH_ROOM_HOOK_RECEIPT", str(tmp_path / "receipt.json"))
    assert install_codex_hooks(config)["state"] == "healthy"
    assert not wrong_config.exists()
    assert "echo unrelated" in config.read_text(encoding="utf-8")
    assert all(event["count"] == 1 for event in codex_hook_status(config)["events"].values())
    assert remove_codex_hooks(config)["state"] == "missing"
    assert config.read_text(encoding="utf-8") == original
