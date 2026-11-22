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
        context = _new_workspace(case, attempt)
        attempt.workspace_outcome("committed", logical_jat_id="history-new")
        replacement = case.native_file.with_suffix(".changed")
        replacement.write_bytes(
            case.payload.replace(b"retained answer", b"retained changed answer")
        )
        replacement.chmod(0o600)
        replacement.replace(case.native_file)
        return {**receipt(context), "status": "saved"}

    result = run(case, plan, workspace)
    assert result["ok"] is False
    assert result["workspace_result"]["snapshot_id"] == "history-new"
    assert result["candidate_logical_jat_id"] == "history-new"
    assert references._load(case.contexts[("demo", "history-new")])[0] is None
    retry = run(case, plan, lambda _: pytest.fail("cannot create another point"))
    assert (
        retry["ok"] is False
        and retry["workspace_result"]["snapshot_id"] == "history-new"
    )
    assert set(case.contexts) == {("demo", "history-first"), ("demo", "history-new")}


def test_real_batch_bound_to_new_candidate_and_retry_reuses_exact_ids(pipeline):
    case = pipeline
    case.scope = dataclasses.replace(case.scope, reviewed_context=None)
    plan = producer.preview_batch(case.scope, case.selection)

    def workspace(attempt):
        context = _new_workspace(case, attempt)
        return {**receipt(context), "status": "saved"}

    first = run(case, plan, workspace)
    assert first["ok"] and first["snapshot_id"] == "history-new"
    ids = {entry["capture_id"] for entry in first["agent_result"]["entries"]}
    assert ids == {entry["new_point_capture_id"] for entry in plan["entries"]}
    before = len(case.store.writes)
    retry = run(case, plan, lambda _: pytest.fail("cannot create another point"))
    assert retry["ok"] and retry["snapshot_id"] == "history-new"
    assert {entry["capture_id"] for entry in retry["agent_result"]["entries"]} == ids
    assert len(case.store.writes) == before


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
