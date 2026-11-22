"""Immutable native captures at one Room point, using real age and synthetic authority."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from test_native_agent_reference import (
    DOMAIN,
    SESSION,
    STAMP,
    MemoryStore,
    add_record,
    descriptor,
)

from josh_room import native_agent_reference as reference
from josh_room import native_agents as native
from josh_room.cancellation import CLICancelled
from josh_room.catalog import Catalog
from josh_room.r2 import R2Conflict, R2PublicationError
from josh_room.room_store_operations import scan_workspace_for_status
from josh_room.room_store_references import LogicalJatIdentity
from josh_room.workspace_state import write_stat_workspace_marker

pytest_plugins = ["test_native_agent_reference"]


def capture(room):
    return reference.capture_reference(room.context, grant=room.grant, tool="codex")


def document(room, context=None):
    return reference._load(context or room.context)[0]


def new_turn(room):
    rows = [
        {"timestamp": STAMP, "type": "event_msg", "payload": {
            "type": "user_message", "message": "synthetic second turn", "images": None, "local_images": [],
        }},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic second answer"}],
        }},
    ]
    payload = room.payload + ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    room.native_file.write_bytes(payload)
    return payload


def hydrate_marker(room):
    """Represent an exact-content same-cwd hydration with a different stat signature."""
    path = room.workspace / "hello.txt"
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    scan = scan_workspace_for_status(room.workspace)
    assert scan.signature != room.scan.signature
    write_stat_workspace_marker(room.workspace, dimension_id="archive", project_id="demo",
        snapshot_id="history-first", encryption_domain_id=DOMAIN, display_name="Synthetic Room",
        workspace_signature=scan.signature, signature_algorithm=scan.signature_algorithm,
        capture_policy_sha256=scan.capture_policy_sha256)
    return scan


def legacy_document(room):
    """Publish the original closed v1 shape; no invented v2 baseline survives."""
    body, etag = reference._load(room.context)
    body["format"] = "josh-room.agent-references.v1"
    body.pop("workspace_content_sha256")
    for entry in body["entries"]:
        entry.pop("capture_id")
        entry.pop("content_sha256")
    reference._publish(room.context, body, etag)
    return body


def copy_context(room):
    item = descriptor("copy", "copy-point", room.scan)
    catalog = add_record(room.context.catalog, item, room.scan)
    store = MemoryStore()
    destination = dataclasses.replace(room.context, backend=store, catalog=catalog, project_id="copy",
        selected_record=catalog.resolve_snapshot("copy", "copy-point"), selected_descriptor=item)
    room.contexts[("copy", "copy-point")] = destination
    grant = room.configure(destination, "copied-profile")
    return destination, grant, store


def test_new_capture_reports_a_separate_immutable_identity_and_no_native_text(room):
    result = capture(room)
    body = document(room)
    entry = body["entries"][0]
    assert result["created"] is True and result["noop"] is False
    assert result["data_uploaded"] == entry["object"]["size"] > 0
    assert result["capture_id"] == entry["capture_id"]
    assert re.fullmatch("[0-9a-f]{64}", result["capture_id"])
    assert result["capture_id"] != entry["object"]["sha256"]
    assert result["capture_id"] != SESSION.replace("-", "")
    assert body["format"] == "josh-room.agent-references.v2"
    assert re.fullmatch("[0-9a-f]{64}", body["workspace_content_sha256"])
    assert re.fullmatch("[0-9a-f]{64}", entry["content_sha256"])
    assert result["contract_version"] == 2
    assert result["history_only"] is True and result["native_resume_verified"] is False
    assert "synthetic retained" not in json.dumps(result)
    assert str(room.workspace) not in json.dumps(result)
    assert room.native_file.read_bytes() == room.payload


def test_identical_latest_capture_is_noop_without_remote_or_pending_writes(room):
    first = capture(room)
    before = list(room.store.writes)
    controls = room.store.controls.copy()
    result = capture(room)
    assert result["capture_id"] == first["capture_id"]
    assert result["created"] is False and result["noop"] is True and result["data_uploaded"] == 0
    assert len(document(room)["entries"]) == 1
    assert room.store.writes == before and room.store.controls == controls
    assert reference._pending_objects(room.context) == (set(), [])


def test_new_turn_is_durable_at_unchanged_workspace_point_and_explicitly_restorable(room):
    point = room.context.selected_record.copy()
    catalog = room.context.catalog.body
    marker = (room.workspace / ".josh-room.json").read_bytes()
    first = capture(room)
    first_entry = document(room)["entries"][0]
    updated = new_turn(room)
    second = capture(room)
    assert second["capture_id"] != first["capture_id"] and second["created"] is True
    body = document(room)
    assert len(body["entries"]) == 2 and body["entries"][0] == first_entry
    assert body["entries"][0]["content_sha256"] != body["entries"][1]["content_sha256"]
    assert room.context.selected_record == point and room.context.catalog.body == catalog
    assert (room.workspace / ".josh-room.json").read_bytes() == marker
    assert len(room.context.catalog.body["projects"]["demo"]["snapshots"]) == 1
    room.native_file.unlink()
    for receipt, expected in [(first, room.payload), (second, updated)]:
        checked = reference.verify_reference(room.context, grant=room.grant, tool="codex", capture_id=receipt["capture_id"])
        assert checked["capture_id"] == receipt["capture_id"]
        assert checked["sessions"][0]["provider_session_id"] == SESSION
        restored = reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION,
                                               capture_id=receipt["capture_id"])
        assert restored["capture_id"] == receipt["capture_id"]
        assert room.native_file.read_bytes() == expected
        room.native_file.unlink()


def test_returning_to_old_native_bytes_is_a_new_event_not_noop(room):
    first = capture(room)
    new_turn(room)
    second = capture(room)
    room.native_file.write_bytes(room.payload)
    third = capture(room)
    entries = document(room)["entries"]
    assert len({first["capture_id"], second["capture_id"], third["capture_id"]}) == 3
    assert len(entries) == 3
    assert entries[0]["content_sha256"] == entries[2]["content_sha256"]
    assert third["created"] is True and third["noop"] is False


@pytest.mark.parametrize("operation", ["verify", "restore"])
@pytest.mark.parametrize("identity", [None, "f" * 64, "latest", SESSION])
def test_v2_selection_never_falls_back_to_latest(room, operation, identity):
    capture(room)
    room.native_file.unlink()
    kwargs = {"grant": room.grant, "tool": "codex", "capture_id": identity}
    if operation == "restore":
        kwargs["session_id"] = SESSION
    with pytest.raises(native.NativeAgentError, match="capture-id-required|capture-id-invalid|unavailable"):
        getattr(reference, operation + "_reference")(room.context, **kwargs)
    assert not room.native_file.exists()


def test_status_is_versioned_metadata_with_explicit_historical_ids(room):
    first = capture(room)
    new_turn(room)
    second = capture(room)
    status = reference.inspect_reference(room.context, profile="personal", tool="codex")
    assert status["contract_version"] == 2 and status["format"] == "josh-room.agent-references.v2"
    assert [entry["capture_id"] for entry in status["entries"]] == [first["capture_id"], second["capture_id"]]
    assert all(entry["legacy"] is False and entry["capture_closed"] is False for entry in status["entries"])
    assert all(set(entry) == {"profile", "tool", "scope", "capture_closed", "capture_id", "created_at", "content_sha256", "legacy"}
               for entry in status["entries"])
    assert reference.inspect_reference(room.context, profile="other", tool="codex")["entries"] == []
    assert reference.inspect_reference(room.context, profile="personal", tool="claude")["entries"] == []
    assert str(room.workspace) not in json.dumps(status) and "synthetic retained" not in json.dumps(status)


def test_same_content_cold_workspace_can_recapture_at_original_point(room):
    first = capture(room)
    old_digest = document(room)["workspace_content_sha256"]
    hydrate_marker(room)
    noop = capture(room)
    assert noop["noop"] is True and noop["capture_id"] == first["capture_id"]
    new_turn(room)
    second = capture(room)
    assert second["created"] is True and second["capture_id"] != first["capture_id"]
    assert document(room)["workspace_content_sha256"] == old_digest
    assert len(room.context.catalog.body["projects"]["demo"]["snapshots"]) == 1


@pytest.mark.parametrize("change", ["bytes", "mode"])
def test_clean_but_different_workspace_cannot_reuse_authenticated_content_baseline(room, change):
    capture(room)
    path = room.workspace / "hello.txt"
    if change == "bytes":
        path.write_text("different synthetic workspace\n")
    else:
        path.chmod(0o700)
    hydrate_marker(room)
    new_turn(room)
    before = list(room.store.writes)
    with pytest.raises(native.NativeAgentError, match="workspace.*(changed|mismatch)|content.*mismatch"):
        capture(room)
    assert room.store.writes == before and len(document(room)["entries"]) == 1


def test_v1_single_capture_stays_readable_without_rewriting_and_stable_id_survives_promotion(room):
    capture(room)
    legacy_document(room)
    status = reference.inspect_reference(room.context)
    identity = status["entries"][0]["capture_id"]
    assert status["format"] == "josh-room.agent-references.v1"
    assert status["entries"][0]["legacy"] is True and status["entries"][0]["content_sha256"] is None
    before = list(room.store.writes)
    assert reference.verify_reference(room.context, grant=room.grant, tool="codex")["capture_id"] == identity
    assert reference.verify_reference(room.context, grant=room.grant, tool="codex", capture_id=identity)["capture_id"] == identity
    noop = capture(room)
    assert noop["capture_id"] == identity and noop["noop"] is True
    assert room.store.writes == before and document(room)["format"] == "josh-room.agent-references.v1"
    new_turn(room)
    capture(room)
    promoted = document(room)
    assert promoted["format"] == "josh-room.agent-references.v2"
    assert promoted["entries"][0]["capture_id"] == identity
    assert re.fullmatch("[0-9a-f]{64}", promoted["entries"][0]["content_sha256"])
    assert reference.verify_reference(room.context, grant=room.grant, tool="codex", capture_id=identity)["sessions"][0]["provider_session_id"] == SESSION


def test_v1_without_original_stat_authority_cannot_initialize_baseline_from_hydrated_marker(room):
    capture(room)
    legacy_document(room)
    hydrate_marker(room)
    new_turn(room)
    before = list(room.store.writes)
    with pytest.raises(native.NativeAgentError, match="workspace-binding-mismatch"):
        capture(room)
    assert room.store.writes == before and document(room)["format"] == "josh-room.agent-references.v1"


@pytest.mark.parametrize("damage", ["missing", "bytes"])
def test_noop_verifies_retained_ciphertext_before_claiming_durable(room, damage):
    capture(room)
    key = document(room)["entries"][0]["object"]["key"]
    if damage == "missing":
        del room.store.objects[key]
    else:
        room.store.objects[key] += b"corrupt"
    before = list(room.store.writes)
    with pytest.raises(native.NativeAgentError, match="unavailable|invalid"):
        capture(room)
    assert room.store.writes == before


def test_authenticated_content_hash_is_checked_on_verify_restore_and_noop(room):
    first = capture(room)
    body, etag = reference._load(room.context)
    body["entries"][0]["content_sha256"] = "0" * 64
    reference._publish(room.context, body, etag)
    room.native_file.unlink()
    for operation in ("verify", "restore"):
        kwargs = {"grant": room.grant, "tool": "codex", "capture_id": first["capture_id"]}
        if operation == "restore":
            kwargs["session_id"] = SESSION
        with pytest.raises(native.NativeAgentError, match="content-mismatch"):
            getattr(reference, operation + "_reference")(room.context, **kwargs)
        assert not room.native_file.exists()
    room.native_file.write_bytes(room.payload)
    room.native_file.chmod(0o600)
    with pytest.raises(native.NativeAgentError, match="content-mismatch"):
        capture(room)


def test_noop_does_not_clear_an_older_uncertain_receipt(room):
    first = capture(room)
    retained = document(room)["entries"][0]["object"]
    receipt = reference._pending(room.context, [retained], "capture")
    before = reference._pending_objects(room.context)
    result = capture(room)
    assert result["noop"] is True and result["capture_id"] == first["capture_id"]
    assert receipt.exists() and reference._pending_objects(room.context) == before


def test_closed_reference_refuses_even_identical_native_content(room):
    capture(room)
    reference.close_for_records(room.context, [LogicalJatIdentity("demo", "history-first")],
                                {("demo", "history-first"): room.context.selected_descriptor})
    before = list(room.store.writes)
    with pytest.raises(native.NativeAgentError, match="capture-closed"):
        capture(room)
    assert room.store.writes == before


def test_copy_export_reachability_and_removal_keep_every_historical_capture(room):
    first = capture(room)
    updated = new_turn(room)
    second = capture(room)
    source = document(room)
    source_keys = {entry["object"]["key"] for entry in source["entries"]}
    copied, copied_grant, copied_store = copy_context(room)
    result = reference.copy_reference(room.context, copied, "copy", "copy-point")
    destination = document(room, copied)
    assert destination["workspace_content_sha256"] == source["workspace_content_sha256"]
    assert result["data_uploaded"] == sum(entry["object"]["size"] for entry in destination["entries"])
    for old, new in zip(source["entries"], destination["entries"], strict=True):
        assert {key: value for key, value in old.items() if key != "object"} == {key: value for key, value in new.items() if key != "object"}
        assert old["object"] != new["object"]
    exported = reference.export_reference(room.context, room.root / "history.haul")
    companion = Path(exported["output"])
    report = reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor})
    assert set(report["reachable_objects"]) == source_keys
    for receipt, expected in [(first, room.payload), (second, updated)]:
        room.native_file.unlink(missing_ok=True)
        verified = reference.verify_reference(room.context, grant=room.grant, tool="codex", capture_id=receipt["capture_id"], export_path=companion)
        assert verified["capture_id"] == receipt["capture_id"]
        reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION,
                                    capture_id=receipt["capture_id"], export_path=companion)
        assert room.native_file.read_bytes() == expected
    closed = reference.close_for_records(room.context, [LogicalJatIdentity("demo", "history-first")],
                                        {("demo", "history-first"): room.context.selected_descriptor})
    assert {entry["key"] for entry in closed[0]["objects"]} == source_keys
    removed = dataclasses.replace(room.context, catalog=Catalog.empty("archive", DOMAIN))
    cleanup = reference.complete_removed_references(removed, closed, descriptors={})
    assert set(cleanup["removed_objects"]) == source_keys and cleanup["pending_objects"] == []
    assert not room.store.objects
    assert len(copied_store.objects) == 2
    for receipt in (first, second):
        verified = reference.verify_reference(copied, grant=copied_grant, tool="codex", capture_id=receipt["capture_id"])
        assert verified["capture_id"] == receipt["capture_id"]


def test_export_aggregate_limit_is_checked_before_collecting_blobs(room, monkeypatch):
    capture(room)
    new_turn(room)
    capture(room)
    monkeypatch.setattr(reference, "MAX_EXPORT", 100)
    monkeypatch.setattr(reference, "_blob", lambda *args, **kwargs: pytest.fail("over-limit export must not fetch blobs"))
    with pytest.raises(native.NativeAgentError, match="export-limit"):
        reference.preflight_export(room.context, room.root / "over-limit.haul")
    assert not (room.root / "over-limit.haul.agents.age").exists()


@pytest.mark.parametrize("fault", ["lost-response", "cancelled"])
def test_second_capture_lost_response_keeps_both_history_and_pending_accounting(room, fault):
    first = capture(room)
    first_key = document(room)["entries"][0]["object"]["key"]
    new_turn(room)
    publish = room.store._publish

    def failed(key, body):
        publish(key, body)
        if fault == "lost-response":
            raise R2PublicationError("synthetic lost response", published=True)
        raise CLICancelled

    room.store._publish = failed
    with pytest.raises(reference.NativeReferenceError, match="publication-uncertain") as caught:
        capture(room)
    assert caught.value.committed is True
    entries = document(room)["entries"]
    assert len(entries) == 2 and entries[0]["capture_id"] == first["capture_id"]
    pending, receipt_ids = reference._pending_objects(room.context)
    assert receipt_ids and pending == {entries[1]["object"]["key"]}
    assert first_key not in pending
    report = reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor})
    assert set(report["reachable_objects"]) == {entry["object"]["key"] for entry in entries}
    assert report["unreachable_candidates"] == []


def test_changed_capture_retains_previous_history_when_new_cas_is_rejected(room, monkeypatch):
    first = capture(room)
    before = document(room)
    new_turn(room)
    def rejected(*args):
        raise R2Conflict("synthetic conflicting append")

    monkeypatch.setattr(room.store, "replace_control", rejected)
    with pytest.raises(reference.NativeReferenceError, match="conflict") as caught:
        capture(room)
    assert caught.value.sidecar_published is False
    assert document(room) == before and document(room)["entries"][0]["capture_id"] == first["capture_id"]
    report = reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor})
    assert len(report["reachable_objects"]) == 1 and len(report["unreachable_candidates"]) == 1
    assert report["pending_receipts"]


def test_duplicate_capture_identity_is_rejected_even_for_distinct_contents(room):
    capture(room)
    new_turn(room)
    capture(room)
    body = document(room)
    body["entries"][1]["capture_id"] = body["entries"][0]["capture_id"]
    with pytest.raises(native.NativeAgentError, match="identity-ambiguous"):
        reference._validate(body, reference._target(room.context))


def test_raw_manifest_content_digest_matches_without_ciphertext_or_archive_identity(room):
    capture(room)
    entry = document(room)["entries"][0]
    manifest = [(room.relative.as_posix(), len(room.payload), hashlib.sha256(room.payload).hexdigest())]
    raw = json.dumps(["josh-room.native-capture-content.v1", "codex", manifest], sort_keys=True, separators=(",", ":")).encode()
    assert entry["content_sha256"] == hashlib.sha256(raw).hexdigest()


def test_noop_rechecks_concurrent_closure_after_retained_content_verification(room, monkeypatch):
    capture(room)
    decode = reference._decode_entry
    closed = False
    writes_after_close = None

    def close_during_verify(*args, **kwargs):
        nonlocal closed, writes_after_close
        result = decode(*args, **kwargs)
        if not closed:
            closed = True
            reference.close_for_records(room.context, [LogicalJatIdentity("demo", "history-first")],
                                        {("demo", "history-first"): room.context.selected_descriptor})
            writes_after_close = list(room.store.writes)
        return result

    monkeypatch.setattr(reference, "_decode_entry", close_during_verify)
    with pytest.raises(native.NativeAgentError, match="conflict|capture-closed"):
        capture(room)
    assert closed is True and document(room)["captures_closed"] is True
    assert room.store.writes == writes_after_close
    assert reference._pending_objects(room.context) == (set(), [])


def test_noop_rechecks_current_authority_after_retained_content_verification(room, monkeypatch):
    capture(room)
    before = list(room.store.writes)
    decode = reference._decode_entry

    def revoke_after_verify(*args, **kwargs):
        result = decode(*args, **kwargs)
        room.receipt["authority_sha256"] = "e" * 64
        return result

    monkeypatch.setattr(reference, "_decode_entry", revoke_after_verify)
    with pytest.raises(native.NativeAgentError, match="admission-changed"):
        capture(room)
    assert room.store.writes == before
    assert reference._pending_objects(room.context) == (set(), [])


def test_control_limit_refuses_new_history_before_upload_or_pending_receipt(room, monkeypatch):
    capture(room)
    new_turn(room)
    before = list(room.store.writes)
    # The existing encrypted control fits; the additional immutable entry does
    # not. This exercises preflight without constructing a huge native history.
    monkeypatch.setattr(reference, "MAX_CONTROL", max(map(len, room.store.controls.values())) + 1)
    with pytest.raises(native.NativeAgentError, match="native-reference-limit"):
        capture(room)
    assert room.store.writes == before and len(document(room)["entries"]) == 1
    assert reference._pending_objects(room.context) == (set(), [])


def test_generated_capture_id_collision_is_refused_before_upload(room, monkeypatch):
    first = capture(room)
    new_turn(room)
    before = list(room.store.writes)
    monkeypatch.setattr(reference.secrets, "token_hex", lambda size: first["capture_id"])
    with pytest.raises(native.NativeAgentError, match="identity-ambiguous"):
        capture(room)
    assert room.store.writes == before and len(document(room)["entries"]) == 1
    assert reference._pending_objects(room.context) == (set(), [])


def test_semantic_workspace_is_rechecked_after_append_control_encryption(room, monkeypatch):
    first = capture(room)
    new_turn(room)
    encrypt = reference._encrypt

    def changed_after_encryption(data, recipients):
        result = encrypt(data, recipients)
        if json.loads(data).get("format") == "josh-room.agent-references.v2":
            (room.workspace / "hello.txt").write_text("synthetic content changed during encryption\n")
            hydrate_marker(room)
        return result

    monkeypatch.setattr(reference, "_encrypt", changed_after_encryption)
    with pytest.raises(reference.NativeReferenceError, match="publication-uncertain"):
        capture(room)
    assert [entry["capture_id"] for entry in document(room)["entries"]] == [first["capture_id"]]
    report = reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor})
    assert len(report["reachable_objects"]) == 1 and len(report["unreachable_candidates"]) == 1
    assert report["pending_receipts"]
