"""Adapter + real producer/age; synthetic storage/admission, no native turns."""

import dataclasses
import os
import sys
from contextlib import contextmanager

import pytest
from test_native_agent_reference import room  # noqa: F401 - shared synthetic fixture
from test_native_agent_save_producer import _new_workspace
from test_native_agent_save_producer import case as producer_case  # noqa: F401

from josh_room import native_agent_reference as references
from josh_room import native_agent_save as adapter
from josh_room import native_agent_save_producer as producer
from josh_room import room_store_bridge as bridge
from josh_room.cancellation import CLICancelled

_OPEN_SCOPE = adapter.open_scope

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux producer custody"
)


@pytest.fixture
def pipeline(request, monkeypatch):
    case = request.getfixturevalue("producer_case")

    @contextmanager
    def scope(*_args, **_kwargs):
        yield case.scope

    @contextmanager
    def exact(*_args, project_id, snapshot_id):
        assert snapshot_id != "latest"
        yield case.contexts[(project_id, snapshot_id)]

    monkeypatch.setattr(adapter, "open_scope", scope)
    monkeypatch.setattr(bridge, "open_existing_room_store", exact)
    monkeypatch.setattr(
        adapter,
        "_with_reviewed",
        lambda scope, reviewed_id: (
            scope
            if reviewed_id is None
            else dataclasses.replace(
                scope, reviewed_context=case.contexts[("demo", reviewed_id)]
            )
        ),
    )
    return case


def receipt(context):
    return adapter._workspace_receipt(context)


def run(case, plan, workspace):
    return adapter.save(
        case.context.instance,
        case.context.dimension,
        case.context.material,
        "demo",
        case.workspace,
        case.selection,
        plan,
        workspace,
    )


def test_real_producer_batch_through_adapter_keeps_workspace_point_and_a_b_a_history(
    pipeline,
):
    case = pipeline
    original = case.native_file.read_bytes()
    captures = []
    for content in (
        original,
        original,
        original.replace(b"retained answer", b"retained changed answer"),
        original,
    ):
        case.native_file.write_bytes(content)
        plan = producer.preview_batch(case.scope, case.selection)
        assert adapter.reviewed_plan(plan, case.selection) == plan
        before = len(case.store.writes)
        result = run(case, plan, lambda _attempt: receipt(case.context))
        assert result["ok"] and result["snapshot_id"] == "history-first"
        entries = {entry["tool"]: entry for entry in result["agent_result"]["entries"]}
        captures.append(entries["codex"]["capture_id"])
        if len(captures) == 2:
            assert entries["codex"]["state"] == "reused"
            assert all(entry["data_uploaded"] == 0 for entry in entries.values())
            assert len(case.store.writes) == before
        assert set(case.contexts) == {("demo", "history-first")}
    assert captures[0] == captures[1] and len(set(captures)) == 3
    document, _ = references._load(case.context)
    assert (
        len([entry for entry in document["entries"] if entry["tool"] == "codex"]) == 3
    )
    assert (
        len([entry for entry in document["entries"] if entry["tool"] == "claude"]) == 1
    )


def test_real_candidate_commit_and_later_native_change_preserve_workspace_result(
    pipeline,
):
    case = pipeline
    case.scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(case.scope, case.selection)

    def workspace(attempt):
        context = _new_workspace(case, attempt, name=plan["workspace_candidate_id"])
        attempt.workspace_outcome(
            "committed", logical_jat_id=plan["workspace_candidate_id"]
        )
        replacement = case.native_file.with_suffix(".changed")
        replacement.write_bytes(
            case.payload.replace(b"retained answer", b"retained changed answer")
        )
        replacement.chmod(0o600)
        replacement.replace(case.native_file)
        return {**receipt(context), "status": "saved"}

    result = run(case, plan, workspace)
    assert result["ok"] is False
    assert result["workspace_result"]["snapshot_id"] == plan["workspace_candidate_id"]
    assert result["candidate_logical_jat_id"] == plan["workspace_candidate_id"]
    assert (
        references._load(case.contexts[("demo", plan["workspace_candidate_id"])])[0]
        is None
    )
    retry = run(case, plan, lambda _: pytest.fail("cannot create another point"))
    assert (
        retry["ok"] is False
        and retry["workspace_result"]["snapshot_id"] == plan["workspace_candidate_id"]
    )
    assert set(case.contexts) == {
        ("demo", "history-first"),
        ("demo", plan["workspace_candidate_id"]),
    }


def test_real_batch_bound_to_new_candidate_and_retry_reuses_exact_ids(pipeline):
    case = pipeline
    case.scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(case.scope, case.selection)

    def workspace(attempt):
        context = _new_workspace(case, attempt, name=plan["workspace_candidate_id"])
        return {**receipt(context), "status": "saved"}

    first = run(case, plan, workspace)
    assert first["ok"] and first["snapshot_id"] == plan["workspace_candidate_id"]
    ids = {entry["capture_id"] for entry in first["agent_result"]["entries"]}
    assert ids == {entry["new_point_capture_id"] for entry in plan["entries"]}
    before = len(case.store.writes)
    retry = run(case, plan, lambda _: pytest.fail("cannot create another point"))
    assert retry["ok"] and retry["snapshot_id"] == plan["workspace_candidate_id"]
    assert {entry["capture_id"] for entry in retry["agent_result"]["entries"]} == ids
    assert len(case.store.writes) == before


@pytest.mark.parametrize("lost_reply", [False, True])
def test_retry_committed_candidate_after_reviewed_parent_removal_never_requires_parent_or_new_save(
    pipeline, monkeypatch, lost_reply
):
    case = pipeline
    plan = producer.preview_batch(case.scope, case.selection)
    assert plan["reviewed_point"]["logical_jat_id"] == "history-first"
    with (
        pytest.raises(CLICancelled),
        producer.prepare_batch(case.scope, case.selection, plan) as attempt,
    ):
        exact = _new_workspace(
            case, attempt, name=plan["workspace_candidate_id"], marker=not lost_reply
        )
        attempt.workspace_outcome(
            "uncertain" if lost_reply else "committed",
            logical_jat_id=plan["workspace_candidate_id"],
        )
        raise CLICancelled()
    catalog, _, _ = exact.catalog.remove_snapshot("demo", "history-first")
    exact = dataclasses.replace(exact, catalog=catalog)
    case.contexts[("demo", plan["workspace_candidate_id"])] = exact
    del case.contexts[("demo", "history-first")]
    case.store.conditional_catalog_put(
        references._encrypt(references._canonical_json(catalog.body), case.recipients),
        case.store.catalog_etag,
    )

    @contextmanager
    def opened(*_args, project_id=None, snapshot_id="latest"):
        if project_id is None:
            yield dataclasses.replace(
                exact, project_id=None, selected_record=None, selected_descriptor=None
            )
        else:
            assert snapshot_id == plan["workspace_candidate_id"]
            yield exact

    monkeypatch.setattr(adapter, "open_scope", _OPEN_SCOPE)
    monkeypatch.setattr(bridge, "open_existing_room_store", opened)
    monkeypatch.setattr(
        adapter,
        "_with_reviewed",
        lambda scope, reviewed_id: (
            scope
            if reviewed_id is None
            else pytest.fail("removed parent must not be loaded")
        ),
    )
    result = run(
        case, plan, lambda _: pytest.fail("must not create a second workspace point")
    )
    assert (
        result["ok"] is True and result["snapshot_id"] == plan["workspace_candidate_id"]
    )
    assert {entry["capture_id"] for entry in result["agent_result"]["entries"]} == {
        entry["new_point_capture_id"] for entry in plan["entries"]
    }
    assert not list((case.context.instance / "agent-reference-receipts").glob("*"))
    assert set(case.contexts) == {("demo", plan["workspace_candidate_id"])}


@pytest.mark.parametrize(
    "change", ["newer-marker", "other-room", "workspace", "revoked", "marker-symlink"]
)
def test_lost_reply_retry_refuses_changed_binding_or_authority_without_marker_overwrite(
    pipeline, change, monkeypatch
):
    from test_native_agent_reference import mark

    from josh_room.workspace_state import MARKER_NAME

    case = pipeline
    plan = producer.preview_batch(case.scope, case.selection)
    with producer.prepare_batch(case.scope, case.selection, plan) as attempt:
        exact = _new_workspace(
            case, attempt, name=plan["workspace_candidate_id"], marker=False
        )
        attempt.workspace_outcome(
            "uncertain", logical_jat_id=plan["workspace_candidate_id"]
        )
    if change in {"newer-marker", "other-room"}:
        body = exact.selected_descriptor.to_dict()
        if change == "newer-marker":
            body["logical_jat_id"] = "unrelated-newer"
        else:
            body["room_id"] = "other-room"
        from josh_room.logical_jat import LogicalJat

        marker_context = dataclasses.replace(
            exact,
            project_id=body["room_id"],
            selected_record={
                **exact.selected_record,
                "snapshot_id": body["logical_jat_id"],
            },
            selected_descriptor=LogicalJat.from_dict(body),
        )
        mark(case, marker_context)
    elif change == "workspace":
        (case.workspace / "hello.txt").write_text("synthetic changed workspace\n")
    elif change == "revoked":
        from josh_room.native_agents import NativeAgentError

        def revoked(*_args, **_kwargs):
            raise NativeAgentError("native-admission-not-authorized")

        monkeypatch.setattr(references, "_grant", revoked)
    else:
        outside = case.root / "unrelated-marker"
        outside.write_bytes((case.workspace / MARKER_NAME).read_bytes())
        (case.workspace / MARKER_NAME).unlink()
        (case.workspace / MARKER_NAME).symlink_to(outside)
    marker = case.workspace / MARKER_NAME
    before = marker.read_bytes()
    writes = case.store.writes.copy()
    result = run(case, plan, lambda _: pytest.fail("must not create a workspace point"))
    assert (
        result["ok"] is False
        and result["workspace_result"]["snapshot_id"] == plan["workspace_candidate_id"]
    )
    assert result["workspace_result"]["marker_state"] == "unfinalized"
    assert marker.read_bytes() == before and case.store.writes == writes
    assert references._load(exact)[0] is None


@pytest.mark.parametrize("fail_ledger", [False, True])
def test_actual_bridge_calls_durable_hook_after_object_and_before_catalog_cas(
    tmp_path, monkeypatch, fail_ledger
):
    if not os.environ.get("JOSH_ROOM_TEST_RESTIC"):
        pytest.skip("explicit pinned Restic runtime required")
    from test_room_store_bridge import _fixture

    dimension, material, backend, *_ = _fixture(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("synthetic\n")
    observed = []

    class Hook:
        def before_workspace_publication(self, descriptor, object_ref, signature):
            assert len(signature) == 64
            assert any(call[0] == "put_file" for call in backend.calls)
            assert not any(
                call[0] == "conditional_catalog_put" for call in backend.calls
            )
            observed.append(descriptor.to_dict()["logical_jat_id"])
            assert object_ref.sha256 in object_ref.key
            if fail_ledger:
                raise OSError("synthetic ledger write refused")

        def workspace_outcome(self, phase, *, logical_jat_id):
            assert logical_jat_id == observed[0]
            observed.append(phase)

    arguments = (
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
    )
    options = {
        "components": dict.fromkeys(
            ("rcc_environment", "homebrew_recovery", "hauler_content")
        ),
        "publication_hook": Hook(),
    }
    if fail_ledger:
        with pytest.raises(bridge.RoomStoreBridgeError) as caught:
            bridge._save_room_store_workspace(*arguments, **options)
        assert caught.value.result["publication_state"] == "rejected"
        assert caught.value.result["candidate_logical_jat_id"] == observed[0]
        assert not any(call[0] == "conditional_catalog_put" for call in backend.calls)
        assert not (workspace / ".josh-room.json").exists()
    else:
        result = bridge._save_room_store_workspace(*arguments, **options)
        assert result["ok"] and result["snapshot_id"] == observed[0]
        assert observed[1] == "committed"
        assert sum(call[0] == "conditional_catalog_put" for call in backend.calls) == 1


def test_reserved_workspace_id_collision_never_replaces_immutable_room_history(
    tmp_path, monkeypatch
):
    if not os.environ.get("JOSH_ROOM_TEST_RESTIC"):
        pytest.skip("explicit pinned Restic runtime required")
    from test_room_store_bridge import _fixture

    dimension, material, backend, *_ = _fixture(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("synthetic first\n")
    arguments = (
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
    )
    components = dict.fromkeys(
        ("rcc_environment", "homebrew_recovery", "hauler_content")
    )
    first = bridge._save_room_store_workspace(*arguments, components=components)
    before = backend.catalog
    writes = [
        call
        for call in backend.calls
        if call[0] in {"put_file", "conditional_catalog_put"}
    ]
    marker = (workspace / ".josh-room.json").read_bytes()
    (workspace / "file.txt").write_text("synthetic changed\n")
    with pytest.raises(bridge.RoomStoreBridgeError) as caught:
        bridge._save_room_store_workspace(
            *arguments,
            components=components,
            agent_plan={"workspace_candidate_id": first["snapshot_id"]},
        )
    assert caught.value.result["publication_state"] == "rejected"
    assert backend.catalog == before
    assert [
        call
        for call in backend.calls
        if call[0] in {"put_file", "conditional_catalog_put"}
    ] == writes
    assert (workspace / ".josh-room.json").read_bytes() == marker
