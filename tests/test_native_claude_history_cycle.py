"""Opt-in native Claude turn across two immutable Room-bound captures.

Real age and pinned Claude run against a deterministic loopback provider. Room
storage and host admission use the disclosed synthetic reference fixture.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_native_agent_reference import (
    SESSION,
    STAMP,
    documents,
    room,  # noqa: F401 - register synthetic Room fixture.
)
from test_native_claude_continuation import _loopback_namespace, resume_restored_claude

from josh_room import native_agent_reference as references
from josh_room import native_agents as agents
from josh_room.claude_native_history import project_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux")
        or os.environ.get("JOSH_ROOM_TEST_CLAUDE_HISTORY_CYCLE") != "1",
        reason="explicit isolated pinned Claude Room-history cycle opt-in required",
    ),
]


def test_native_claude_turn_recaptured_at_same_room_point_then_second_cold_restore(request, tmp_path):
    _loopback_namespace()
    case = request.getfixturevalue("room")
    original_catalog = copy.deepcopy(case.context.catalog.body)
    point = case.context.selected_record["snapshot_id"]
    common = {"isSidechain": False, "userType": "external", "cwd": str(case.workspace),
              "sessionId": SESSION, "version": "2.1.310", "gitBranch": "synthetic", "timestamp": STAMP}
    user = "22222222-2222-4222-8222-222222222222"
    rows = [
        {**common, "parentUuid": None, "type": "user", "uuid": user,
         "message": {"role": "user", "content": "synthetic Claude question"}},
        {**common, "parentUuid": user, "type": "assistant", "uuid": "33333333-3333-4333-8333-333333333333",
         "message": {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic-offline",
                     "content": [{"type": "text", "text": "synthetic retained Claude answer"}],
                     "stop_reason": "end_turn", "stop_sequence": None,
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    relative = Path("projects") / project_key(str(case.workspace)) / f"{SESSION}.jsonl"
    source_profile = Path(case.grant["profile_root"])
    native_file = source_profile / "claude" / relative
    native_file.parent.mkdir(mode=0o700, parents=True)
    for parent in native_file.parents:
        if parent.is_relative_to(source_profile):
            parent.chmod(0o700)
    native_file.write_bytes(payload)
    native_file.chmod(0o600)

    initial = references.capture_reference(case.context, grant=case.grant, tool="claude")
    first_id = initial["capture_id"]
    assert initial["created"] is True and initial["noop"] is False
    first_entry = copy.deepcopy(documents(case)["entries"][0])
    first_cipher = case.store.objects[first_entry["object"]["key"]]
    assert documents(case)["format"] == references.FORMAT
    assert documents(case)["binding"]["logical_jat_id"] == point

    shutil.rmtree(source_profile)
    cold_grant = case.configure(case.context, "claude-first-cold")
    restored = references.restore_reference(case.context, grant=cold_grant, tool="claude",
                                            capture_id=first_id, session_id=SESSION)
    cold_profile = Path(cold_grant["profile_root"])
    cold_file = cold_profile / "claude" / relative
    assert restored["capture_id"] == first_id and cold_file.read_bytes() == payload
    turn = resume_restored_claude(tmp_path, workspace=case.workspace, config=cold_profile / "claude",
                                  relative=relative, payload=payload)
    after = cold_file.read_bytes()
    assert turn["provider_session_id"] == SESSION
    assert after.startswith(payload) and after != payload
    assert len(after.splitlines()) == 14

    recaptured = references.capture_reference(case.context, grant=cold_grant, tool="claude")
    second_id = recaptured["capture_id"]
    assert recaptured["created"] is True and recaptured["noop"] is False and second_id != first_id
    history = documents(case)
    assert [entry["capture_id"] for entry in history["entries"]] == [first_id, second_id]
    assert history["entries"][0] == first_entry
    assert history["entries"][1]["archive_format"] == agents.CLAUDE_FORMAT
    assert history["entries"][1]["content_sha256"] != first_entry["content_sha256"]
    assert case.store.objects[first_entry["object"]["key"]] == first_cipher
    assert history["binding"]["logical_jat_id"] == point

    writes = case.store.writes.copy()
    unchanged = references.capture_reference(case.context, grant=cold_grant, tool="claude")
    assert unchanged["noop"] is True and unchanged["created"] is False
    assert unchanged["capture_id"] == second_id and unchanged["data_uploaded"] == 0
    assert case.store.writes == writes
    assert documents(case) == history
    assert case.context.catalog.body == original_catalog
    # Inspect the actual catalog fence ciphertext as well as the fixture's
    # context, so a phantom workspace recovery point cannot hide in a write.
    with references._cipher_file(case.context, case.store.catalog_body) as source:
        assert json.loads(agents._decrypt_to_memory(source, case.identities[:1])) == original_catalog

    shutil.rmtree(cold_profile)
    second_grant = case.configure(case.context, "claude-second-cold")
    second_profile = Path(second_grant["profile_root"])
    assert list((second_profile / "claude").iterdir()) == []
    restored = references.restore_reference(case.context, grant=second_grant, tool="claude",
                                            capture_id=second_id, session_id=SESSION)
    assert restored["capture_id"] == second_id
    assert (second_profile / "claude" / relative).read_bytes() == after
    verified = references.verify_reference(case.context, grant=second_grant, tool="claude", capture_id=first_id)
    assert verified["capture_id"] == first_id
    assert verified["sessions"] == [{"provider_session_id": SESSION, "tool": "claude", "cwd_matches": True}]
    original, _ = references._decode_entry(case.context, history["entries"][0])
    assert original == {str(relative): payload}
    assert len(documents(case)["entries"]) == 2
    assert case.context.catalog.body == original_catalog
    assert references._pending_objects(case.context) == (set(), [])
    print(json.dumps({"provider": "claude", "native_version": "2.1.310",
                      "reference_format": history["format"], "native_archive_format": agents.CLAUDE_FORMAT,
                      "provider_session_id": SESSION, "same_logical_point": True,
                      "immutable_captures": 2, "unchanged_recapture_writes": 0,
                      "native_turn_observed": True, "second_cold_restore": True,
                      "first_capture_still_available": True, "rows_preserved": 14,
                      "sha256": hashlib.sha256(after).hexdigest(),
                      "storage": "synthetic-memory", "admission": "synthetic-fixture",
                      "hosted_provider_acceptance": False}))
