"""Real-age producer transactions; synthetic storage/admission, no native turns."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_native_agent_reference import DOMAIN, descriptor, mark
from test_native_agent_reference_live import _history

from josh_room import native_agent_reference as references
from josh_room import native_agent_save_producer as producer
from josh_room import native_agents as native
from josh_room.cancellation import CLICancelled
from josh_room.catalog import Catalog
from josh_room.r2 import R2PublicationError

pytest_plugins = ["test_native_agent_reference"]
pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux kernel-barrier producer")
PROFILE = "e" * 64


@pytest.fixture
def case(request, monkeypatch):
    value = request.getfixturevalue("room")
    value.context.material.keyset.binding = value.context.physical_binding
    value.context = dataclasses.replace(value.context, dimension=SimpleNamespace(dimension_id="archive", provider="minio"))
    value.contexts[("demo", "history-first")] = value.context
    value.grant.update(encryption_domain_id=DOMAIN, key_generation=1, allow_room_reference=True)
    relative, payload = _history(value.workspace, "claude")
    path = Path(value.grant["profile_root"]) / "claude" / relative
    path.parent.mkdir(mode=0o700, parents=True)
    for parent in path.parents:
        if parent.is_relative_to(Path(value.grant["profile_root"])):
            parent.chmod(0o700)
    path.write_bytes(payload)
    path.chmod(0o600)
    value.claude_file = path
    value.claude_payload = payload

    def resolve(*, dimension, room, workspace, profile_ref, tool):
        assert (dimension, room, str(workspace)) == ("archive", "demo", str(value.workspace))
        if profile_ref != PROFILE:
            raise native.NativeAgentError("native-profile-selector-stale-or-unavailable")
        assert tool in {"codex", "claude"}
        return value.grant

    monkeypatch.setattr(producer.discovery, "resolve_profile", resolve)
    value.selection = {"format": producer.SELECTION_FORMAT, "entries": [
        {"tool": "codex", "profile_ref": PROFILE}, {"tool": "claude", "profile_ref": PROFILE}]}
    value.scope = producer.TargetScope(dataclasses.replace(value.context, selected_record=None, selected_descriptor=None),
        value.workspace, value.scan.capture_policy_sha256, value.scan.signature, reviewed_context=value.context)
    return value


def _prepared(case, plan=None):
    return producer.prepare_batch(case.scope, case.selection, plan or producer.preview_batch(case.scope, case.selection))


def _existing_save(attempt, case):
    attempt.workspace_outcome("committed", logical_jat_id="history-first")
    return producer.publish_batch(case.context, attempt)


def _new_workspace(case, attempt, name="history-new", *, marker=True):
    item = descriptor("demo", name, case.scan)
    cipher = references._encrypt(item.to_json().encode(), case.recipients)
    digest = hashlib.sha256(cipher).hexdigest()
    ref = case.store.put_bytes("objects/sha256/" + digest, cipher)
    attempt.before_workspace_publication(item, ref, case.scan.signature)
    body = native._json(native._decrypt_to_memory(attempt.root / "attempt.age", case.identities[:1], producer.MAX_LEDGER))
    assert body["candidate"]["logical_jat_id"] == name
    assert body["candidate"]["object"]["sha256"] == ref.sha256
    assert ("demo", name) not in case.contexts
    # Actual descriptor ciphertext identity, rather than the convenience
    # plaintext fixture helper's object, is the workspace publication record.
    catalog = case.context.catalog.add_logical_snapshot("demo", "Synthetic Room", item, ref, case.scan.signature)
    selected = dataclasses.replace(case.context, catalog=catalog, selected_record=catalog.resolve_snapshot("demo", name),
                                   selected_descriptor=item)
    case.contexts[("demo", name)] = selected
    case.store.conditional_catalog_put(references._encrypt(references._canonical_json(catalog.body), case.recipients),
                                       case.store.catalog_etag)
    if marker:
        mark(case, selected)
    return selected


def _append(case):
    stamp = "2026-11-22T15:01:00Z"
    rows = [{"timestamp": stamp, "type": "event_msg", "payload": {
        "type": "user_message", "message": "synthetic next question", "images": None, "local_images": []}},
        {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "synthetic next answer"}]}}]
    case.native_file.write_bytes(case.payload + "".join(json.dumps(row) + "\n" for row in rows).encode())


def test_preview_is_bounded_public_and_reserves_both_result_branches(case):
    result = producer.preview_batch(case.scope, case.selection)
    assert result["format"] == producer.PLAN_FORMAT
    assert len(result["entries"]) == 2 and len(json.dumps(result)) < producer.MAX_PLAN
    assert result["reviewed_point"]["logical_jat_id"] == "history-first"
    assert result["reviewed_point"]["sidecar_sha256"] == producer._digest([None, None])
    assert all(entry["existing_point"]["outcome"] == "created" for entry in result["entries"])
    ids = [entry[key] for entry in result["entries"] for key in ("new_point_capture_id",)]
    ids += [entry["existing_point"]["capture_id"] for entry in result["entries"]]
    assert len(set(ids)) == 4
    assert str(case.root) not in json.dumps(result)
    assert "synthetic retained" not in json.dumps(result) and "recipients" not in json.dumps(result)
    assert case.store.writes == []


def test_new_room_scope_needs_no_fabricated_point(case):
    scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(scope, case.selection)
    assert plan["reviewed_point"] is None
    assert all(entry["existing_point"] is None for entry in plan["entries"])
    with producer.prepare_batch(scope, case.selection, plan) as attempt:
        result = producer.publish_batch(case.context, attempt)
        assert result["ok"] is False and result["error"] == "native-save-workspace-not-committed"
    assert case.store.writes == []


def test_two_tools_share_one_cas_and_fence_and_keep_cipher_only_attempt(case):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan) as attempt:
        assert case.store.writes == []
        assert attempt.root.is_relative_to(Path(case.grant["local_reference_root"]))
        for path in attempt.root.iterdir():
            assert path.read_bytes().startswith(b"age-encryption.org/v1")
            assert b"synthetic retained" not in path.read_bytes()
        result = _existing_save(attempt, case)
    assert result["ok"] is True and {entry["state"] for entry in result["entries"]} == {"created"}
    assert [entry["capture_id"] for entry in result["entries"]] == [entry["existing_point"]["capture_id"] for entry in plan["entries"]]
    assert [kind for kind, _ in case.store.writes].count("control") == 1
    assert [kind for kind, _ in case.store.writes].count("catalog") == 1
    assert references._pending_objects(case.context) == (set(), [])
    assert len(references._load(case.context)[0]["entries"]) == 2


def test_all_noop_exact_reviewed_ids_zero_remote_writes_and_idempotent_retry(case):
    with _prepared(case) as attempt:
        first = _existing_save(attempt, case)
    plan = producer.preview_batch(case.scope, case.selection)
    assert {entry["existing_point"]["outcome"] for entry in plan["entries"]} == {"reused"}
    expected = {entry["tool"]: entry["capture_id"] for entry in first["entries"]}
    before = case.store.writes.copy()
    with _prepared(case, plan) as attempt:
        result = _existing_save(attempt, case)
    assert result["ok"] and all(entry["state"] == "reused" and entry["data_uploaded"] == 0 for entry in result["entries"])
    assert {entry["tool"]: entry["capture_id"] for entry in result["entries"]} == expected
    assert case.store.writes == before
    again = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert again["state"] == "complete" and case.store.writes == before


def test_new_point_always_gets_its_own_event_even_with_identical_native_bytes(case):
    with _prepared(case) as attempt:
        first = _existing_save(attempt, case)
    plan = producer.preview_batch(case.scope, case.selection)
    assert all(entry["existing_point"]["outcome"] == "reused" for entry in plan["entries"])
    with _prepared(case, plan) as attempt:
        context = _new_workspace(case, attempt)
        attempt.workspace_outcome("committed", logical_jat_id="history-new")
        result = producer.publish_batch(context, attempt)
    assert result["ok"] and all(entry["state"] == "created" for entry in result["entries"])
    assert {entry["capture_id"] for entry in first["entries"]}.isdisjoint(entry["capture_id"] for entry in result["entries"])
    assert [entry["capture_id"] for entry in result["entries"]] == [entry["new_point_capture_id"] for entry in plan["entries"]]


@pytest.mark.parametrize("change", ["native", "workspace", "grant", "sidecar"])
def test_changed_review_requires_new_preview_before_any_workspace_write(case, change):
    plan = producer.preview_batch(case.scope, case.selection)
    if change == "native":
        _append(case)
    elif change == "workspace":
        (case.workspace / "hello.txt").write_text("changed synthetic content\n")
    elif change == "grant":
        case.receipt["authority_sha256"] = "c" * 64
    else:
        references.capture_reference(case.context, grant=case.grant, tool="codex")
    before = case.store.writes.copy()
    with pytest.raises(native.NativeAgentError), _prepared(case, plan):
        pytest.fail("stale plan entered workspace Save")
    assert case.store.writes == before


def test_changed_native_after_workspace_commit_is_partial_without_agent_publish(case):
    with _prepared(case) as attempt:
        context = _new_workspace(case, attempt)
        attempt.workspace_outcome("committed", logical_jat_id="history-new")
        # Replacing, rather than write-opening, deliberately exercises the
        # inventory fence alongside the kernel lease held on the old inode.
        replacement = case.native_file.with_suffix(".new")
        replacement.write_bytes(case.payload + b"\n")
        replacement.chmod(0o600)
        replacement.replace(case.native_file)
        before = case.store.writes.copy()
        result = producer.publish_batch(context, attempt)
    assert not result["ok"] and result["state"] == "blocked"
    assert case.store.writes == before and not case.store.controls


@pytest.mark.parametrize("fault", ["lost-response", "cancelled"])
def test_unknown_agent_cas_reconciles_same_ids_without_duplicate_events(case, fault, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    publish = case.store._publish

    def uncertain(key, body):
        publish(key, body)
        if fault == "cancelled":
            raise CLICancelled
        raise R2PublicationError("synthetic lost response", published=True)

    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(case.store, "_publish", uncertain)
        result = _existing_save(attempt, case)
    assert result["state"] == "agent-uncertain" and result["reconciliation_required"]
    assert all(entry["state"] != "created" for entry in result["entries"])
    assert references._pending_objects(case.context)[1]
    monkeypatch.setattr(case.store, "_publish", publish)
    result = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert result["state"] == "complete"
    assert len(references._load(case.context)[0]["entries"]) == 2
    assert [kind for kind, _ in case.store.writes].count("control") == 1


def test_unknown_workspace_cas_uses_only_journaled_candidate(case):
    scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(scope, case.selection)
    with producer.prepare_batch(scope, case.selection, plan) as attempt:
        actual = _new_workspace(case, attempt, marker=False)
        attempt.workspace_outcome("uncertain", logical_jat_id="history-new")
    result = producer.reconcile_attempt(scope, case.selection, plan)
    assert result["state"] == "workspace-committed" and result["workspace_committed"] is True
    assert result["logical_jat_id"] == "history-new" and not case.store.controls
    # Save owner repairs only the verified exact marker; there is no second
    # workspace Save or newest-point substitution.
    mark(case, actual)
    with producer.prepare_batch(scope, case.selection, plan) as attempt:
        attempt.workspace_outcome("committed", logical_jat_id="history-new")
        final = producer.publish_batch(actual, attempt)
    assert final["ok"] and len(references._load(actual)[0]["entries"]) == 2


def test_absent_unknown_workspace_candidate_does_not_authorize_another_save(case):
    scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(scope, case.selection)
    with producer.prepare_batch(scope, case.selection, plan) as attempt:
        item = descriptor("demo", "not-committed", case.scan)
        cipher = references._encrypt(item.to_json().encode(), case.recipients)
        digest = hashlib.sha256(cipher).hexdigest()
        object_ref = case.store.put_bytes("objects/sha256/" + digest, cipher)
        attempt.before_workspace_publication(item, object_ref, case.scan.signature)
        attempt.workspace_outcome("uncertain", logical_jat_id="not-committed")
    result = producer.reconcile_attempt(scope, case.selection, plan)
    assert result["state"] == "workspace-uncertain"
    assert result["workspace_committed"] is None and result["logical_jat_id"] == "not-committed"
    assert not case.store.controls


def test_second_tool_prepare_failure_stops_whole_batch_before_workspace(case):
    plan = producer.preview_batch(case.scope, case.selection)
    replacement = case.claude_file.with_suffix(".replacement")
    replacement.write_bytes(b'{"unsafe":"synthetic"}\n')
    replacement.chmod(0o600)
    replacement.replace(case.claude_file)
    with pytest.raises(native.NativeAgentError), _prepared(case, plan):
        pytest.fail("invalid native tool entered Save")
    assert case.store.writes == []


def test_capacity_preflight_happens_before_any_native_upload(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(references, "MAX_CONTROL", 2500)
        result = _existing_save(attempt, case)
    assert not result["ok"] and result["error"] == "native-reference-limit"
    assert case.store.writes == []


def test_public_plan_rejects_unreviewed_id_edit_even_if_digest_is_not_updated(case):
    plan = producer.preview_batch(case.scope, case.selection)
    plan["entries"][0]["new_point_capture_id"] = "0" * 64
    with pytest.raises(native.NativeAgentError, match="plan-invalid"), _prepared(case, plan):
        pytest.fail("modified public plan accepted")


@pytest.mark.parametrize("change", ["native", "workspace", "admission"])
def test_stale_retry_retains_independently_corroborated_exact_workspace(case, change):
    scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(scope, case.selection)
    with producer.prepare_batch(scope, case.selection, plan) as attempt:
        _new_workspace(case, attempt)
        attempt.workspace_outcome("uncertain", logical_jat_id="history-new")
    if change == "native":
        _append(case)
    elif change == "workspace":
        (case.workspace / "hello.txt").write_text("changed after committed workspace\n")
    else:
        case.receipt["authority_sha256"] = "c" * 64
    before = case.store.writes.copy()
    result = producer.reconcile_attempt(scope, case.selection, plan)
    assert result["state"] == "blocked" and result["workspace_committed"] is True
    assert result["logical_jat_id"] == "history-new"
    assert case.store.writes == before and not case.store.controls


def test_unresolvable_revoked_selector_returns_blocked_not_absent(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan):
        pass
    def revoked(**kwargs):
        raise native.NativeAgentError("native-profile-selector-stale-or-unavailable")

    monkeypatch.setattr(producer.discovery, "resolve_profile", revoked)
    result = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert result["state"] == "blocked" and result["attempt_id"] == plan["attempt_id"]
    assert result["workspace_committed"] is None


def test_second_upload_failure_keeps_both_reserved_candidates_and_no_sidecar(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    put = case.store.put_bytes
    count = 0

    def fail_second(key, body):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic provider fault")
        return put(key, body)

    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(case.store, "put_bytes", fail_second)
        result = _existing_save(attempt, case)
    assert result["state"] == "agent-uncertain" and not case.store.controls
    assert not any(entry["state"] == "created" for entry in result["entries"])
    assert len(references._pending_objects(case.context)[0]) == 2
    assert sum(isinstance(entry["data_uploaded"], int) and entry["data_uploaded"] > 0 for entry in result["entries"]) == 1
    assert sum(entry["data_uploaded"] is None for entry in result["entries"]) == 1
    assert sum(entry["upload_uncertain"] for entry in result["entries"]) == 1
    pending_before = references._pending_objects(case.context)[1]
    assert len(pending_before) == 1
    monkeypatch.setattr(case.store, "put_bytes", put)
    with _prepared(case, plan) as attempt:
        final = producer.publish_batch(case.context, attempt)
    assert final["ok"] and len(references._load(case.context)[0]["entries"]) == 2
    assert references._pending_objects(case.context) == (set(), [])


def test_cas_race_never_rebases_over_unselected_history(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    publish = references._publish

    def raced(context, document, etag, *, before_commit=None):
        other = dict(document)
        other["revision"] += 1
        # Simulate a competing publisher winning the shared ETag, not a
        # per-tool sequential publish accepted by a silent rebase.
        publish(context, other, etag)
        return publish(context, document, etag, before_commit=before_commit)

    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(references, "_publish", raced)
        result = _existing_save(attempt, case)
    assert result["state"] == "agent-uncertain"
    assert [kind for kind, _ in case.store.writes].count("control") == 1
    assert references._pending_objects(case.context)[1]


def test_cancel_without_candidate_cannot_turn_into_fresh_workspace_permission(case):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan) as attempt:
        attempt.workspace_outcome("cancelled")
    result = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert result["state"] == "workspace-uncertain" and result["logical_jat_id"] is None


def test_journal_pending_path_is_closed_not_arbitrary_unlink(case):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan) as attempt:
        attempt.ledger["pending_receipt"] = "../../outside-sensitive-file"
        attempt._persist()
    result = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert result["state"] == "blocked" and not case.store.writes


def test_accepted_upload_failed_readback_remains_nullable_through_stale_retry(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    get = case.store.get_bytes
    count = 0

    def fail_second_readback(key, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            assert key in case.store.objects  # Accepted PUT, unreadable response.
            raise OSError("synthetic accepted upload readback failure")
        return get(key, **kwargs)

    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(case.store, "get_bytes", fail_second_readback)
        result = _existing_save(attempt, case)
    assert len(case.store.objects) == 2 and not case.store.controls
    first, second = result["entries"]
    assert first["data_uploaded"] > 0 and first["upload_uncertain"] is False
    assert second["data_uploaded"] is None and second["upload_uncertain"] is True
    _append(case)
    blocked = producer.reconcile_attempt(case.scope, case.selection, plan)
    assert blocked["state"] == "blocked" and blocked["workspace_committed"] is True
    assert blocked["entries"][0]["data_uploaded"] == first["data_uploaded"]
    assert blocked["entries"][1]["data_uploaded"] is None
    case.native_file.write_bytes(case.payload)
    monkeypatch.setattr(case.store, "get_bytes", get)
    with _prepared(case, plan) as attempt:
        final = producer.publish_batch(case.context, attempt)
    assert final["ok"] and all(isinstance(entry["data_uploaded"], int) for entry in final["entries"])
    assert references._pending_objects(case.context) == (set(), [])
    assert len(case.store.objects) == 2  # Same immutable encrypted candidates.


def test_retry_never_discards_a_mismatched_pending_receipt_or_its_objects(case, monkeypatch):
    plan = producer.preview_batch(case.scope, case.selection)
    put = case.store.put_bytes
    count = 0

    def fail_second(key, body):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic upload interruption")
        return put(key, body)

    with _prepared(case, plan) as attempt:
        monkeypatch.setattr(case.store, "put_bytes", fail_second)
        assert not _existing_save(attempt, case)["ok"]
        pending = case.context.instance / "agent-reference-receipts" / attempt.ledger["pending_receipt"]
    monkeypatch.setattr(case.store, "put_bytes", put)
    unrelated = b"synthetic unrelated retained ciphertext"
    digest = hashlib.sha256(unrelated).hexdigest()
    protected = case.store.put_bytes("objects/sha256/" + digest, unrelated)
    body = json.loads(pending.read_bytes())
    body["objects"].append(dataclasses.asdict(protected))
    pending.write_text(json.dumps(body))
    retained = pending.read_bytes()
    before = case.store.writes.copy()
    with _prepared(case, plan) as attempt:
        result = producer.publish_batch(case.context, attempt)
    assert not result["ok"] and result["error"] == "native-save-pending-receipt-mismatch"
    assert pending.read_bytes() == retained and case.store.writes == before
    assert case.store.objects[protected.key] == unrelated and not case.store.controls


def test_recorded_committed_b_retries_without_removed_reviewed_a(case):
    plan = producer.preview_batch(case.scope, case.selection)
    with _prepared(case, plan) as attempt:
        actual = _new_workspace(case, attempt)
        attempt.workspace_outcome("uncertain", logical_jat_id="history-new")
    body = copy_catalog = json.loads(json.dumps(actual.catalog.body))
    del body["projects"]["demo"]["snapshots"]["history-first"]
    catalog = Catalog.from_body(copy_catalog)
    actual = dataclasses.replace(actual, catalog=catalog)
    case.contexts.pop(("demo", "history-first"))
    case.contexts[("demo", "history-new")] = actual
    case.store.conditional_catalog_put(references._encrypt(references._canonical_json(catalog.body), case.recipients),
                                       case.store.catalog_etag)
    metadata_scope = dataclasses.replace(case.scope, reviewed_context=None)
    reconciled = producer.reconcile_attempt(metadata_scope, case.selection, plan)
    assert reconciled["workspace_committed"] is True and reconciled["logical_jat_id"] == "history-new"
    with producer.prepare_batch(metadata_scope, case.selection, plan) as attempt:
        attempt.workspace_outcome("committed", logical_jat_id="history-new")
        final = producer.publish_batch(actual, attempt)
    assert final["ok"] and final["logical_jat_id"] == "history-new"
    assert len(references._load(actual)[0]["entries"]) == 2
    assert ("demo", "history-first") not in case.contexts
