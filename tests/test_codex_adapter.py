from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import zstandard

from josh_room.adapter_contract import (
    AdapterError,
    AdapterErrorCode,
    Decision,
    GateDecision,
    GateSet,
    LogicalSourceName,
    ResolveStatus,
    SourceEvent,
)
from josh_room.codex_adapter import (
    CodexHookFacts,
    CodexRoots,
    CodexTranscriptAdapter,
)


class AllowGate:
    def evaluate(self, _event, _declaration):
        return GateDecision(Decision.ALLOW, ("synthetic-allow",), "synthetic-policy")


def _gates() -> GateSet:
    return GateSet(policy=AllowGate(), material=AllowGate())


def _event(session_id: str = "session-1") -> SourceEvent:
    return SourceEvent(
        event_id="event-1",
        logical_source=LogicalSourceName.TRANSCRIPT,
        session_id=session_id,
        approved_root="codex-sessions",
    )


def _write_jsonl(path: Path, records: list[dict], *, trailing_newline: bool = True) -> None:
    body = "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
    if trailing_newline:
        body += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _adapter(tmp_path: Path, *, hook: CodexHookFacts | None = None) -> CodexTranscriptAdapter:
    return CodexTranscriptAdapter(
        roots=CodexRoots(
            active=tmp_path / "sessions",
            archived=tmp_path / "archived_sessions",
        ),
        hook_facts=hook,
    )


def _rollout(path_root: Path, session_id: str = "session-1") -> Path:
    path = path_root / "2026" / "09" / "22" / f"rollout-{session_id}.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": session_id, "surface": "cli"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "text": "hello"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "text": "hi"}},
    ])
    return path


def test_hook_facts_select_contained_source_and_keep_diagnostics_path_free(tmp_path: Path):
    path = _rollout(tmp_path / "sessions")
    adapter = _adapter(
        tmp_path,
        hook=CodexHookFacts(
            session_id="session-1",
            cwd=tmp_path,
            transcript_path=path,
            surface="cli",
        ),
    )

    result = adapter.resolve("session-1", None)
    inspection = adapter.inspect().to_json()

    assert result.status is ResolveStatus.FOUND
    assert result.representation == "active-jsonl"
    assert str(path) not in inspection
    assert str(path) not in result.to_json()


def test_fallback_quarantines_duplicate_session_candidates_without_timestamp_choice(tmp_path: Path):
    first = _rollout(tmp_path / "sessions", "session-1")
    duplicate = first.with_name("rollout-session-1-copy.jsonl")
    shutil.copyfile(first, duplicate)
    first.with_name("rollout-session-1-copy-2.jsonl").touch()
    (tmp_path / "sessions" / "session_index.jsonl").write_text(
        json.dumps({"session_id": "session-1", "rollout_path": str(first)}) + "\n",
        encoding="utf-8",
    )
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_hook_fact_is_only_a_hint_and_duplicate_candidate_quarantines(tmp_path: Path):
    first = _rollout(tmp_path / "sessions")
    duplicate = first.with_name("rollout-session-1-copy.jsonl")
    shutil.copyfile(first, duplicate)
    adapter = _adapter(
        tmp_path,
        hook=CodexHookFacts(
            session_id="session-1",
            cwd=tmp_path,
            transcript_path=first,
            surface="cli",
        ),
    )

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_hook_symlink_is_rejected_even_when_it_resolves_inside_approved_root(tmp_path: Path):
    source = _rollout(tmp_path / "outside")
    link = tmp_path / "sessions" / "2026" / "09" / "22" / source.name
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    adapter = _adapter(
        tmp_path,
        hook=CodexHookFacts(
            session_id="session-1",
            cwd=tmp_path,
            transcript_path=link,
        ),
    )

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.NOT_FOUND


@pytest.mark.parametrize("surface", ["cli", "desktop", "vscode", "app-server", "subagent"])
def test_provenance_surface_and_subagent_type_are_bounded_metadata(tmp_path: Path, surface: str):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [{"type": "session_meta", "payload": {"id": "session-1"}}])
    adapter = _adapter(
        tmp_path,
        hook=CodexHookFacts(
            session_id="session-1",
            cwd=tmp_path,
            transcript_path=path,
            surface=surface,
            subagent_type="reviewer" if surface == "subagent" else None,
        ),
    )

    records = list(adapter.open(adapter.plan(_event(), _gates())))
    metadata = json.loads(records[0].content)

    assert metadata["surface"] == surface
    if surface == "subagent":
        assert metadata["subagent_type"] == "reviewer"


def test_stream_allowlists_visible_records_and_excludes_reasoning_auth_and_unknown(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "session-1", "surface": "desktop"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "text": "hello"}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "hidden"}},
        {"type": "auth_refresh", "payload": {"access_token": "secret"}},
        {"type": "event_msg", "payload": {"type": "token_count", "input_tokens": 3}},
    ])
    adapter = _adapter(tmp_path)

    plan = adapter.plan(_event(), _gates())
    stream = adapter.open(plan)
    records = list(stream)
    decoded = [json.loads(record.content) for record in records]

    assert [record["record_kind"] for record in decoded] == ["session_meta", "message", "usage"]
    assert all("hidden" not in record.content.decode() for record in records)
    assert all("secret" not in record.content.decode() for record in records)


def test_nested_reasoning_content_is_not_emitted(tmp_path: Path):
    _write_jsonl(tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl", [
        {"type": "session_meta", "payload": {"id": "session-1"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "visible"},
                    {"type": "reasoning", "text": "HIDDEN-REASONING"},
                ],
            },
        },
    ])
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE
    assert "HIDDEN-REASONING" not in result.to_json()


def test_conflicting_schema_version_fields_quarantine(tmp_path: Path):
    _write_jsonl(tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl", [
        {"type": "session_meta", "payload": {"id": "session-1"}},
        {"schema_version": 1, "version": 99, "type": "session_meta", "payload": {"id": "session-1"}},
    ])
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_issued_plan_retention_is_bounded(tmp_path: Path):
    _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)

    for index in range(80):
        event = replace(_event(), event_id=f"event-{index}")
        adapter.plan(event, _gates())

    assert len(adapter._issued) <= 64


def test_unknown_record_kind_quarantines_instead_of_being_silently_omitted(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "session-1", "surface": "cli"}},
        {"type": "future_major_kind", "payload": {"text": "unknown"}},
    ])
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_unknown_major_schema_quarantines_instead_of_being_silently_omitted(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "session-1", "surface": "cli"}},
        {"schema_version": {"major": 99, "minor": 0}, "type": "session_meta", "payload": {"id": "session-1"}},
    ])
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


@pytest.mark.parametrize("field", ["version", "schema_version"])
def test_scalar_schema_versions_quarantine_instead_of_being_accepted(tmp_path: Path, field: str):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "session-1"}},
        {field: 99, "type": "session_meta", "payload": {"id": "session-1"}},
    ])
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_scan_record_cap_quarantines_instead_of_treating_cap_as_eof(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    records = [{"type": "session_meta", "payload": {"id": "session-1"}}]
    records.extend(
        {"type": "response_item", "payload": {"type": "message", "role": "user", "text": str(index)}}
        for index in range(129)
    )
    _write_jsonl(path, records)
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_fallback_scan_cap_quarantines_instead_of_guessing_first_candidate(tmp_path: Path):
    target = _rollout(tmp_path / "sessions")
    for index in range(127):
        _write_jsonl(
            tmp_path / "sessions" / "2026" / "09" / "22" / f"{target.stem}-extra-{index:03d}.jsonl",
            [{"type": "session_meta", "payload": {"id": f"other-{index}"}}],
        )
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_compressed_representation_under_active_root_is_rejected(tmp_path: Path):
    source = _rollout(tmp_path / "archived_sessions")
    source_bytes = source.read_bytes()
    source.unlink()
    compressed = tmp_path / "sessions" / source.relative_to(tmp_path / "archived_sessions")
    compressed = compressed.with_suffix(compressed.suffix + ".zst")
    compressed.parent.mkdir(parents=True, exist_ok=True)
    compressed.write_bytes(zstandard.ZstdCompressor().compress(source_bytes))
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.NOT_FOUND


def test_incomplete_final_line_is_not_emitted_and_checkpoint_resumes_after_completion(tmp_path: Path):
    path = _rollout(tmp_path / "sessions")
    with path.open("ab") as handle:
        handle.write(b'{"type":"response_item","payload":{"type":"message","role":"assistant","text":"done"')
    adapter = _adapter(tmp_path)

    first = adapter.open(adapter.plan(_event(), _gates()))
    records = list(first)
    checkpoint = adapter.checkpoint(first.result)

    assert len(records) == 3
    assert checkpoint.next_record_index == 3
    assert checkpoint.next_byte_offset < path.stat().st_size

    with path.open("ab") as handle:
        handle.write(b',"role":"assistant","text":"done"}}\n')
    resumed = adapter.resolve("session-1", checkpoint)

    assert resumed.status is ResolveStatus.FOUND
    assert resumed.checkpoint is not None
    resumed_plan = adapter.plan(_event(), _gates(), prior_checkpoint=checkpoint)
    resumed_stream = adapter.open(resumed_plan)
    resumed_records = list(resumed_stream)
    assert [json.loads(record.content)["text"] for record in resumed_records] == ["done"]


def test_active_to_archived_and_plain_to_zstd_transitions_resume_without_duplicates(tmp_path: Path):
    active = _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)
    first = adapter.open(adapter.plan(_event(), _gates()))
    assert next(first).record_index == 0
    checkpoint = adapter.checkpoint(first.result)

    archived = tmp_path / "archived_sessions" / active.relative_to(tmp_path / "sessions")
    archived.parent.mkdir(parents=True, exist_ok=True)
    active.replace(archived)
    moved = adapter.resolve("session-1", checkpoint)
    assert moved.status is ResolveStatus.MOVED
    moved_plan = adapter.plan(_event(), _gates(), prior_checkpoint=checkpoint)
    assert moved.checkpoint is not None
    assert moved.checkpoint.source_id == checkpoint.source_id
    assert [record.record_index for record in adapter.open(moved_plan)] == [1, 2]

    zst_path = archived.with_suffix(archived.suffix + ".zst")
    zst_path.write_bytes(zstandard.ZstdCompressor().compress(archived.read_bytes()))
    archived.unlink()
    compressed = adapter.resolve("session-1", moved.checkpoint)

    assert compressed.status is ResolveStatus.MOVED
    assert compressed.representation == "compressed-jsonl-zst"


def test_credential_bearing_remote_is_sanitized_without_copying_raw_remote(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "payload": {"id": "session-1", "surface": "vscode"}},
        {"type": "event_msg", "payload": {"type": "repo", "remote": "https://user:secret@example.invalid/org/repo.git"}},
    ])
    adapter = _adapter(tmp_path)

    records = list(adapter.open(adapter.plan(_event(), _gates())))
    joined = b"".join(record.content for record in records)

    assert b"secret" not in joined
    assert b"user@" not in joined
    assert b"example.invalid/org/repo" in joined


def test_replaced_source_fails_closed_on_resume_without_exposing_content(tmp_path: Path):
    path = _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)
    stream = adapter.open(adapter.plan(_event(), _gates()))
    assert next(stream).record_index == 0
    checkpoint = adapter.checkpoint(stream.result)
    path.write_text('{"type":"session_meta","payload":{"id":"session-1","surface":"cli","text":"forged"}}\n', encoding="utf-8")

    result = adapter.resolve("session-1", checkpoint)

    assert result.status is ResolveStatus.CONFLICT
    assert "forged" not in result.to_json()


def test_same_prefix_suffix_replacement_fails_closed_on_resume(tmp_path: Path):
    path = _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)
    stream = adapter.open(adapter.plan(_event(), _gates()))
    assert next(stream).record_index == 0
    checkpoint = adapter.checkpoint(stream.result)
    original = path.read_bytes()
    suffix = original[checkpoint.next_byte_offset:]
    path.write_bytes(original[:checkpoint.next_byte_offset] + suffix.replace(b'"hi"', b'"by"', 1))

    result = adapter.resolve("session-1", checkpoint)

    assert result.status is ResolveStatus.CONFLICT


def test_resume_rejects_cursor_index_offset_and_observed_size_forgery(tmp_path: Path):
    _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)
    stream = adapter.open(adapter.plan(_event(), _gates()))
    assert next(stream).record_index == 0
    checkpoint = adapter.checkpoint(stream.result)

    bad_offset = replace(checkpoint, next_record_index=0)
    bad_size = replace(checkpoint, observed_size=checkpoint.next_byte_offset)

    assert adapter.resolve("session-1", bad_offset).status is ResolveStatus.CONFLICT
    assert adapter.resolve("session-1", bad_size).status is ResolveStatus.CONFLICT


def test_checkpoint_publication_rejects_regression_but_allows_exact_repeat(tmp_path: Path):
    adapter = _adapter(tmp_path)
    _rollout(tmp_path / "sessions")
    stream = adapter.open(adapter.plan(_event(), _gates()))
    initial = stream.result
    assert next(stream).record_index == 0
    published = adapter.checkpoint(stream.result)

    assert adapter.checkpoint(stream.result) == published
    assert adapter.plan(_event(), _gates()).checkpoint.next_record_index == 0
    with pytest.raises(AdapterError) as error:
        adapter.checkpoint(initial)
    assert error.value.code is AdapterErrorCode.SOURCE_CURSOR_INCONSISTENT


def test_open_rejects_mutation_of_any_issued_plan_field(tmp_path: Path):
    _rollout(tmp_path / "sessions")
    adapter = _adapter(tmp_path)
    plan = adapter.plan(_event(), _gates())
    object.__setattr__(plan, "estimated_records", plan.estimated_records + 1)

    with pytest.raises(AdapterError) as error:
        adapter.open(plan)

    assert error.value.code is AdapterErrorCode.INVALID_REQUEST


def test_plain_and_compressed_duplicates_quarantine_even_with_same_prefix(tmp_path: Path):
    plain = _rollout(tmp_path / "archived_sessions")
    compressed = plain.with_suffix(plain.suffix + ".zst")
    compressed.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE


def test_oversized_line_is_bounded_and_diagnostic_is_opaque(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [{"type": "session_meta", "payload": {"id": "session-1", "surface": "cli"}}])
    with path.open("ab") as handle:
        handle.write(b'{"type":"response_item","payload":{"type":"asset","data":"' + b"x" * (600 * 1024) + b'"}}\n')
    adapter = CodexTranscriptAdapter(
        CodexRoots(tmp_path / "sessions", tmp_path / "archived_sessions"),
        max_record_bytes=128,
    )

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE
    assert "x" not in result.to_json()


def test_symlink_outside_explicit_root_is_not_a_source_and_error_has_no_path(tmp_path: Path):
    outside = _rollout(tmp_path / "outside")
    link = tmp_path / "sessions" / "2026" / "09" / "22" / outside.name
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.NOT_FOUND
    assert str(outside) not in result.to_json()


def test_invalid_compressed_source_becomes_typed_quarantine_without_path_or_error_text(tmp_path: Path):
    path = tmp_path / "archived_sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a zstd stream")
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE
    assert str(path) not in result.to_json()


def test_malformed_complete_record_quarantines_without_returning_raw_bytes(tmp_path: Path):
    path = tmp_path / "sessions" / "2026" / "09" / "22" / "rollout-session-1.jsonl"
    _write_jsonl(path, [{"type": "session_meta", "payload": {"id": "session-1", "surface": "cli"}}])
    with path.open("ab") as handle:
        handle.write(b"not-json\n")
    adapter = _adapter(tmp_path)

    result = adapter.resolve("session-1", None)

    assert result.status is ResolveStatus.QUARANTINE
    assert "not-json" not in result.to_json()
