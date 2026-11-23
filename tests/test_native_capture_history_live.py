"""Opt-in v2 capture history against a fresh real MinIO/native Secret Service."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import uuid
from pathlib import Path

import pytest
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from test_native_agent_reference_live import SESSION, _json, _selected
from test_native_agent_reference_live import live as reference_live

from josh_room import native_agent_reference as references
from josh_room.workspace_state import context_status, local_status

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native custody/materialization"),
]


@pytest.fixture
def live_history(tmp_path, monkeypatch):
    return reference_live.__wrapped__(tmp_path, monkeypatch)


def _append_turn(payload, tool, revision):
    stamp = f"2026-11-22T16:0{revision}:00Z"
    if tool == "codex":
        added = [
            {"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "user_message", "message": f"synthetic next question {revision}", "images": None, "local_images": [],
            }},
            {"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"synthetic next answer {revision}"}],
            }},
        ]
    else:
        previous = json.loads(payload.splitlines()[-1])
        common = {key: previous[key] for key in ("isSidechain", "userType", "cwd", "sessionId", "version", "gitBranch")}
        user = str(uuid.uuid4())
        added = [
            {**common, "timestamp": stamp, "type": "user", "parentUuid": previous["uuid"], "uuid": user,
             "message": {"role": "user", "content": f"synthetic next question {revision}"}},
            {**common, "timestamp": stamp, "type": "assistant", "parentUuid": user, "uuid": str(uuid.uuid4()),
             "message": {**previous["message"], "id": f"msg_synthetic_{revision}",
                         "content": [{"type": "text", "text": f"synthetic next answer {revision}"}]}},
        ]
    return payload + ("".join(json.dumps(row) + "\n" for row in added)).encode()


def _remote_state(context):
    document, sidecar_etag = references._load(context)
    _catalog, catalog_etag = context.backend.read_catalog()
    inventory = {}
    pages = context.backend.client.get_paginator("list_objects_v2").paginate(
        Bucket=context.backend.config.bucket, Prefix="objects/sha256/",
    )
    for page in pages:
        for item in page.get("Contents", []):
            inventory[item["Key"]] = (item["ETag"], item["Size"])
    return hashlib.sha256(references._canonical_json(document)).hexdigest(), sidecar_etag, catalog_etag, inventory


def _assert_noop(value, tool, expected_id, monkeypatch):
    with _selected(value) as context:
        before = _remote_state(context)
        writes = []
        actual_call = BaseClient._make_api_call

        def observe(client, operation, parameters):
            if operation.startswith(("Put", "Delete", "Copy", "Create", "Upload", "Complete", "Abort", "Restore")):
                writes.append(operation)
            return actual_call(client, operation, parameters)

        with monkeypatch.context() as recording:
            recording.setattr(BaseClient, "_make_api_call", observe)
            result = references.capture_reference(context, grant=value.grant, tool=tool)
        assert result["created"] is False and result["noop"] is True
        assert result["capture_id"] == expected_id and result["data_uploaded"] == 0
        assert writes == []
        assert _remote_state(context) == before
    return len(writes)


def _points(value):
    result = value.cli("snapshots", "list", value.room, "--dimension", "recovery-fixture", "--backend", "minio")
    return {record["snapshot_id"] for record in result["snapshots"]}


def test_real_same_point_history_noop_cold_hydrate_copy_export_remove(live_history, monkeypatch):
    value = live_history
    source_root = Path(value.grant["profile_root"])
    captures = {tool: [(value.capture_ids[tool], value.originals[tool][1])] for tool in ("codex", "claude")}
    assert _points(value) == {value.point}
    with _selected(value) as context:
        initial, _ = references._load(context)
        assert initial["format"] == references.FORMAT
        original_binding = initial["binding"]
        workspace_digest = initial["workspace_content_sha256"]

    for tool, items in captures.items():
        relative, _ = value.originals[tool]
        payload = _append_turn(items[-1][1], tool, 2)
        (source_root / tool / relative).write_bytes(payload)
        result = value.cli("agents", "recovery-capture", *value.common, "--workspace", value.workspace,
                           "--profile-ref", value.profile_ref(), "--tool", tool)
        assert result["created"] is True and result["noop"] is False and result["data_uploaded"] > 0
        assert result["capture_id"] != items[0][0]
        items.append((result["capture_id"], payload))
        _assert_noop(value, tool, result["capture_id"], monkeypatch)

    before = value.workspace / "synthetic.txt"
    original_bytes = before.read_bytes()
    original_mode = stat.S_IMODE(before.stat().st_mode)
    original_stat = (before.stat().st_ino, before.stat().st_ctime_ns)
    value.workspace.rename(value.tmp / "before-cold-hydrate")
    value.cli("hydrate", value.room, "--snapshot", value.point, "--dimension", "recovery-fixture", "--backend", "minio",
              "--destination", value.workspace, "--ide", "terminal")
    after = value.workspace / "synthetic.txt"
    assert after.read_bytes() == original_bytes and stat.S_IMODE(after.stat().st_mode) == original_mode
    assert (after.stat().st_ino, after.stat().st_ctime_ns) != original_stat
    marker = context_status(value.workspace)
    clean = local_status(value.workspace)
    assert marker["path_matches"] is True and marker["snapshot_id"] == value.point
    assert clean["state"] == "clean" and clean["signature_matches"] is True
    assert marker["workspace_signature"] != original_binding["workspace_signature"]
    cold_signature = marker["workspace_signature"]

    noop_writes = 0
    for tool, items in captures.items():
        noop_writes += _assert_noop(value, tool, items[-1][0], monkeypatch)
        relative, _ = value.originals[tool]
        payload = _append_turn(items[-1][1], tool, 3)
        (source_root / tool / relative).write_bytes(payload)
        result = value.cli("agents", "recovery-capture", *value.common, "--workspace", value.workspace,
                           "--profile-ref", value.profile_ref(), "--tool", tool)
        assert result["created"] is True and result["noop"] is False
        items.append((result["capture_id"], payload))
    assert _points(value) == {value.point}
    assert context_status(value.workspace)["workspace_signature"] == cold_signature
    assert local_status(value.workspace)["state"] == "clean"

    with _selected(value) as context:
        document, _ = references._load(context)
        assert len(document["entries"]) == 6
        assert document["binding"] == original_binding
        assert document["workspace_content_sha256"] == workspace_digest
        source_objects = {entry["object"]["key"] for entry in document["entries"]}
        assert len(source_objects) == 6
        exported = references.export_reference(context, value.tmp / "all-history.haul.tar.zst")
        companion = Path(exported["output"])
        assert exported["history_only"] is True and b"synthetic next answer" not in companion.read_bytes()
        bundle, blobs = references._load_export(context, companion)
        assert {entry["capture_id"] for entry in bundle["entries"]} == {capture for items in captures.values() for capture, _ in items}
        assert set(blobs) == source_objects
        for entry in bundle["entries"]:
            decoded, _ = references._decode_entry(context, entry, blobs[entry["object"]["key"]])
            expected = dict(captures[entry["tool"]])[entry["capture_id"]]
            relative, _ = value.originals[entry["tool"]]
            assert decoded == {relative.as_posix(): expected}

    for tool, items in captures.items():
        for revision, (capture_id, payload) in enumerate(items):
            target = value.tmp / f"restore-{tool}-{revision}"
            value.grant["profile_root"] = str(target)
            value.save_policy()
            value.configure(value.room, target)
            flags = [*value.common, "--workspace", value.workspace, "--profile-ref", value.profile_ref(),
                     "--tool", tool, "--capture-id", capture_id]
            verified = value.cli("agents", "recovery-verify", *flags)
            assert verified["capture_id"] == capture_id and verified["sessions"][0]["provider_session_id"] == SESSION
            from_export = ["--export", companion] if revision else []
            restored = value.cli("agents", "recovery-restore", *flags, "--session-id", SESSION, *from_export)
            relative, _ = value.originals[tool]
            assert (target / tool / relative).read_bytes() == payload
            assert restored["capture_id"] == capture_id and restored["native_resume_verified"] is False

    copied_room = value.room + "-history-copy"
    copied_root = value.tmp / "copied-profile"
    value.policy["grants"].append({**value.grant, "room": copied_room, "profile_root": str(copied_root)})
    value.save_policy()
    value.configure(copied_room, copied_root)
    copied = value.cli("snapshot", "copy", value.room, "--source-dimension", "recovery-fixture", "--snapshot", value.point,
                       "--destination-dimension", "recovery-fixture", "--destination-room", copied_room)
    copied_point = copied["logical_jat_id"]
    with _selected(value, copied_room, copied_point) as context:
        copied_document, _ = references._load(context)
        copied_objects = {entry["object"]["key"] for entry in copied_document["entries"]}
        assert len(copied_objects) == 6 and source_objects.isdisjoint(copied_objects)
        assert {entry["capture_id"] for entry in copied_document["entries"]} == {capture for items in captures.values() for capture, _ in items}
        assert all(entry["origin"] == original_binding for entry in copied_document["entries"])
    removed = value.cli("rooms", "remove", value.room, "--dimension", "recovery-fixture", "--backend", "minio")
    assert set(removed["agent_recovery"]["removed_objects"]) == source_objects
    assert removed["agent_recovery"]["pending_objects"] == []
    copied_common = ["--dimension", "recovery-fixture", "--room", copied_room, "--snapshot", copied_point,
                     "--workspace", value.workspace, "--profile-ref", value.profile_ref(copied_room)]
    for tool, items in captures.items():
        for capture_id, _ in items:
            result = value.cli("agents", "recovery-verify", *copied_common, "--tool", tool, "--capture-id", capture_id)
            assert result["capture_id"] == capture_id and result["sessions"][0]["provider_session_id"] == SESSION
    with _selected(value, copied_room, copied_point) as context:
        reachability = references.reconcile_references(context)
        assert copied_objects <= set(reachability["reachable_objects"])
        assert source_objects.isdisjoint(reachability["reachable_objects"])
        assert reachability["unreachable_candidates"] == []
        removed = value.cli("rooms", "remove", copied_room, "--dimension", "recovery-fixture", "--backend", "minio")
        assert set(removed["agent_recovery"]["removed_objects"]) == copied_objects
        assert removed["agent_recovery"]["pending_objects"] == []
        for key in source_objects | copied_objects:
            with pytest.raises(ClientError) as missing:
                context.backend.client.head_object(Bucket=context.backend.config.bucket, Key=key)
            assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
    _json(value.tmp / "history-evidence.json", {
        "providers": ["codex", "claude"], "captures": 6, "revisions_per_provider": 3,
        "noop_botocore_writes": noop_writes, "no_phantom_workspace_points": True,
        "cold_marker_clean_new_stats": True, "all_historical_payloads_restored": True,
        "copy_entries": 6, "export_entries": 6, "removed_source_objects": 6, "removed_copy_objects": 6,
        "native_resume_verified": False,
    })
