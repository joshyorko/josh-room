"""Save adapter tests use synthetic custody, not native/provider acceptance."""

import copy
import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from josh_room import native_agent_save as save
from josh_room import room_store_bridge as bridge
from josh_room.cancellation import CLICancelled
from josh_room.cli import (
    _bounded_json_result,
    _local_save_preflight,
    _native_capture_options,
    build_parser,
)
from josh_room.component_selection import plan_token


def selection(tools=("claude", "codex")):
    return {
        "format": save.SELECTION_FORMAT,
        "entries": [
            {"tool": tool, "profile_ref": str(i + 1) * 64}
            for i, tool in enumerate(tools)
        ],
    }


def plan(tools=("claude", "codex")):
    request = selection(tools)
    return {
        "format": save.PLAN_FORMAT,
        "attempt_id": "a" * 64,
        "target_scope_sha256": "b" * 64,
        "workspace_content_sha256": "c" * 64,
        "capture_policy_sha256": "d" * 64,
        "workspace_signature": "e" * 64,
        "reviewed_point": None,
        "digest": "f" * 64,
        "entries": [
            {
                **entry,
                "content_sha256": "a" * 64,
                "authority_sha256": "b" * 64,
                "new_point_capture_id": str(i + 3) * 64,
                "existing_point": None,
            }
            for i, entry in enumerate(request["entries"])
        ],
    }


def components():
    return {
        "format_version": 1,
        "components": dict.fromkeys(
            ("rcc_environment", "homebrew_recovery", "hauler_content"), "keep"
        ),
    }


@pytest.mark.parametrize(
    "change", ["unknown", "version", "duplicate", "too-many", "path", "tool", "empty"]
)
def test_selection_is_bounded_closed_and_never_accepts_private_paths(change):
    request = selection()
    if change == "unknown":
        request["authority"] = "not authority"
    elif change == "version":
        request["format"] += "-unknown"
    elif change == "duplicate":
        request["entries"][1]["tool"] = request["entries"][0]["tool"]
    elif change == "too-many":
        request["entries"] *= 2
    elif change == "path":
        request["entries"][0]["profile_root"] = "/synthetic/private"
    elif change == "tool":
        request["entries"][0]["tool"] = "t3"
    else:
        request["entries"] = []
    with pytest.raises(ValueError):
        save.selection_request(request)


def test_full_reviewed_plan_survives_json_and_binds_every_reserved_identity():
    reviewed = plan()
    result = _bounded_json_result(
        {"ok": True, "agent_plan": reviewed, "session_token": "synthetic-sensitive"}
    )
    wire = json.loads(json.dumps(result))
    assert save.reviewed_plan(wire["agent_plan"], selection()) == reviewed
    assert wire["session_token"] == "<redacted>"
    options = {
        "dimension": "synthetic",
        "project": "room",
        "workspace": "/synthetic/workspace",
        "policy_sha256": "a" * 64,
    }
    legacy = plan_token(components(), **options)
    import hashlib

    old_bytes = json.dumps(
        [components(), "synthetic", "room", "/synthetic/workspace", "a" * 64, [], None],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert legacy == hashlib.sha256(old_bytes).hexdigest()
    assert legacy == plan_token(components(), **options, agent_plan=None)
    token = plan_token(components(), **options, agent_plan=reviewed)
    changed = copy.deepcopy(reviewed)
    changed["entries"][0]["new_point_capture_id"] = "9" * 64
    assert token != legacy and token != plan_token(
        components(), **options, agent_plan=changed
    )
    assert save.reviewed_plan(json.dumps(reviewed), selection()) == reviewed


@pytest.mark.parametrize(
    "change", ["extra", "oversize", "selector", "reserved", "existing", "format"]
)
def test_review_envelope_never_accepts_unreviewed_or_unknown_content(change):
    reviewed = plan()
    if change == "extra":
        reviewed["native_text"] = "not public"
    elif change == "oversize":
        reviewed["workspace_signature"] = "x" * save.MAX_PLAN_BYTES
    elif change == "selector":
        reviewed["entries"][0]["profile_ref"] = "9" * 64
    elif change == "reserved":
        reviewed["entries"][0]["new_point_capture_id"] = "not-an-id"
    elif change == "existing":
        reviewed["entries"][0]["existing_point"] = {
            "capture_id": "a" * 64,
            "outcome": "reused",
        }
    else:
        reviewed["format"] = "unknown"
    with pytest.raises(ValueError):
        save.reviewed_plan(reviewed, selection())


def test_cli_requires_actual_review_envelope_not_digest_and_bypasses_local_fastpath(
    tmp_path,
):
    args = build_parser().parse_args(
        [
            "snapshot",
            "create",
            "room",
            "--agent-selection",
            json.dumps(selection()),
            "--component-selection",
            json.dumps(components()),
            "--component-plan-token",
            "a" * 64,
        ]
    )
    assert _local_save_preflight(args, tmp_path) is None
    with pytest.raises(ValueError, match="reviewed plan"):
        _native_capture_options(args)
    args.agent_plan = json.dumps(plan())
    output = _native_capture_options(args)
    assert output["agent_selection"] == selection() and output["agent_plan"] == plan()
    assert set(output["component_selection"]["components"]) == set(
        components()["components"]
    )


def test_cli_preview_json_create_roundtrip_retains_reviewed_plan_and_stale_ids_refuse(
    tmp_path, monkeypatch
):
    from josh_room import cli

    calls = []
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("room", "Room"))
    monkeypatch.setattr(
        cli, "_effective_dimension", lambda _args: SimpleNamespace(provider="minio")
    )
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "synthetic-jat")
    reviewed = plan()
    token_options = {
        "dimension": "synthetic",
        "project": "room",
        "workspace": tmp_path,
        "policy_sha256": "a" * 64,
    }
    token = plan_token(components(), **token_options, agent_plan=reviewed)
    monkeypatch.setattr(
        cli,
        "preview_room_store",
        lambda *a, **k: {
            "ok": True,
            "agent_plan": reviewed,
            "component_plan_token": token,
        },
    )

    def create(*_args, **options):
        assert options["agent_selection"] == selection()
        actual = plan_token(
            components(), **token_options, agent_plan=options["agent_plan"]
        )
        if actual != options["component_plan_token"]:
            raise ValueError("stale review")
        calls.append(options["agent_plan"])
        return {"ok": True, "snapshot_id": "exact-point"}

    monkeypatch.setattr(cli, "save_room_store", create)
    base = [
        "room",
        "--source",
        str(tmp_path),
        "--backend",
        "minio",
        "--agent-selection",
        json.dumps(selection()),
        "--component-selection",
        json.dumps(components()),
    ]
    preview_args = build_parser().parse_args(["snapshot", "preview", *base])
    preview_args._selected_encryption_material = object()
    wire = json.loads(
        json.dumps(cli._bounded_json_result(cli.dispatch(preview_args, tmp_path)))
    )
    create_args = build_parser().parse_args(
        [
            "snapshot",
            "create",
            *base,
            "--agent-plan",
            json.dumps(wire["agent_plan"]),
            "--component-plan-token",
            wire["component_plan_token"],
        ]
    )
    create_args._selected_encryption_material = object()
    assert cli.dispatch(create_args, tmp_path)["snapshot_id"] == "exact-point"
    assert calls == [reviewed]
    changed = copy.deepcopy(reviewed)
    changed["entries"][0]["new_point_capture_id"] = "9" * 64
    create_args.agent_plan = json.dumps(changed)
    with pytest.raises(ValueError, match="stale review"):
        cli.dispatch(create_args, tmp_path)


@pytest.fixture
def pipeline(monkeypatch):
    calls = []
    state = {
        "reconciliation": {"state": "absent", "workspace_committed": False},
        "fail_check": None,
        "checks": 0,
        "exit_cancel": False,
    }
    scope = SimpleNamespace(context=object())

    @contextmanager
    def opened_scope(*_args, **_kwargs):
        yield scope

    monkeypatch.setattr(save, "open_scope", opened_scope)

    class Attempt:
        def check(self):
            state["checks"] += 1
            calls.append("check")
            if state["fail_check"] == state["checks"]:
                raise ValueError("source changed")

        def workspace_outcome(self, phase, *, logical_jat_id=None):
            calls.append((phase, logical_jat_id))

    attempt = Attempt()

    @contextmanager
    def prepared(*_args):
        calls.append("prepare")
        yield attempt
        if state["exit_cancel"]:
            raise CLICancelled()

    producer = ModuleType("josh_room.native_agent_save_producer")
    producer.prepare_batch = prepared
    producer.reconcile_attempt = lambda *_args: state["reconciliation"]

    def publish(context, _attempt):
        calls.append(("publish", context.logical_id))
        return {
            "ok": True,
            "state": "complete",
            "entries": [
                {
                    "tool": e["tool"],
                    "profile_ref": e["profile_ref"],
                    "capture_id": e["new_point_capture_id"],
                    "content_sha256": e["content_sha256"],
                    "state": "created",
                    "data_uploaded": 5,
                }
                for e in plan()["entries"]
            ],
        }

    producer.publish_batch = publish
    monkeypatch.setitem(sys.modules, producer.__name__, producer)

    @contextmanager
    def exact(_instance, _dimension, _material, *, project_id, snapshot_id):
        assert snapshot_id and snapshot_id != "latest"
        calls.append(("open-exact", snapshot_id))
        descriptor = SimpleNamespace(
            to_dict=lambda: {
                "dimension_id": "synthetic",
                "encryption_domain_id": "domain",
                "room_id": "room",
                "logical_jat_id": snapshot_id,
                "workspace": {"snapshot_id": "restic"},
                "capture_policy_sha256": "d" * 64,
            }
        )
        yield SimpleNamespace(
            logical_id=snapshot_id,
            selected_descriptor=descriptor,
            selected_record={"workspace_signature": "e" * 64},
        )

    monkeypatch.setattr(bridge, "open_existing_room_store", exact)

    def workspace(_attempt):
        calls.append("workspace")
        return {"ok": True, "status": "saved", "snapshot_id": "exact-new-point"}

    return state, calls, producer, workspace


def run(pipeline, workspace=None):
    return save.save(
        "instance",
        object(),
        object(),
        "room",
        "workspace",
        selection(),
        plan(),
        workspace or pipeline[3],
    )


def test_two_tool_batch_prepares_once_then_binds_the_actual_new_or_component_only_point(
    pipeline,
):
    result = run(pipeline)
    assert result["ok"] and result["snapshot_id"] == "exact-new-point"
    calls = pipeline[1]
    assert (
        calls.index("prepare")
        < calls.index("workspace")
        < calls.index(("publish", "exact-new-point"))
    )
    assert (
        calls.count("prepare") == 1 and calls.count(("publish", "exact-new-point")) == 1
    )


def test_workspace_noop_still_executes_agent_batch(pipeline):
    def unchanged(_attempt):
        return {"ok": True, "status": "already-saved", "snapshot_id": "reviewed-point"}

    result = run(pipeline, unchanged)
    assert (
        result["status"] == "already-saved"
        and result["agent_result"]["entries"][0]["data_uploaded"] == 5
    )
    assert ("publish", "reviewed-point") in pipeline[1]


def test_unknown_workspace_commit_never_publishes_or_substitutes_latest_on_retry(
    pipeline,
):
    def unknown(_attempt):
        raise bridge.RoomStoreBridgeError(
            "unknown",
            code="catalog-publication-uncertain",
            result={
                "publication_state": "uncertain",
                "candidate_logical_jat_id": "candidate-only",
            },
        )

    result = run(pipeline, unknown)
    assert result["ok"] is False and result["workspace_result"] is None
    assert (
        result["candidate_logical_jat_id"] == "candidate-only"
        and result["reconciliation_required"]
    )
    assert not any(isinstance(v, tuple) and v[0] == "publish" for v in pipeline[1])
    pipeline[0]["reconciliation"] = {
        "state": "workspace-uncertain",
        "workspace_committed": None,
        "logical_jat_id": "candidate-only",
    }
    result = run(
        pipeline, lambda *_: pytest.fail("must not create another workspace point")
    )
    assert result["candidate_logical_jat_id"] == "candidate-only"


def test_reconciled_exact_candidate_retries_agents_without_saving_workspace(pipeline):
    pipeline[0]["reconciliation"] = {
        "state": "workspace-committed",
        "workspace_committed": True,
        "logical_jat_id": "candidate-only",
    }
    result = run(
        pipeline, lambda *_: pytest.fail("must not create another workspace point")
    )
    assert result["ok"] and ("publish", "candidate-only") in pipeline[1]


def test_stale_retry_preserves_corroborated_workspace_without_native_publication(
    pipeline,
):
    pipeline[0]["reconciliation"] = {
        "state": "blocked",
        "workspace_committed": True,
        "logical_jat_id": "candidate-only",
    }
    result = run(pipeline, lambda *_: pytest.fail("must not create a new point"))
    assert result["ok"] is False
    assert result["workspace_result"]["snapshot_id"] == "candidate-only"
    assert "prepare" not in pipeline[1]


@pytest.mark.parametrize("failure", [None, "ledger", "unknown", "cancel"])
def test_publication_hook_records_exact_descriptor_before_cas_and_retains_uncertainty(
    tmp_path, failure
):
    from test_room_store_operations import _Catalog, _operations, _Store

    from josh_room.local_store import ObjectRef
    from josh_room.room_store_operations import RoomStorePublicationError

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello")
    catalog = _Catalog()
    events = []
    object_ref = ObjectRef("objects/sha256/" + "a" * 64, "a" * 64, 23)

    class Hook:
        def before_workspace_publication(self, descriptor, reference, signature):
            assert reference is object_ref and len(signature) == 64
            events.append(("durable-candidate", descriptor.to_dict()["logical_jat_id"]))
            if failure == "ledger":
                raise OSError("synthetic durable-write refusal")

        def workspace_outcome(self, phase, *, logical_jat_id):
            events.append((phase, logical_jat_id))

    operations = _operations(tmp_path, workspace, _Store(), catalog)
    operations.publication_hook = Hook()

    def publish(descriptor, *, before_catalog_cas, **kwargs):
        try:
            before_catalog_cas(descriptor, object_ref, kwargs["workspace_signature"])
        except OSError:
            raise bridge._CatalogPublicationError(False) from None
        events.append("catalog-cas")
        if failure == "unknown":
            raise bridge._CatalogPublicationError(None)
        if failure == "cancel":
            error = CLICancelled()
            error.result["publication_state"] = "uncertain"
            raise error
        catalog.publish(descriptor, **kwargs)

    operations.publish_descriptor = publish
    if failure:
        with pytest.raises((RoomStorePublicationError, CLICancelled)) as caught:
            operations.save()
        result = caught.value.result
        assert result["candidate_logical_jat_id"] == events[0][1]
        assert result["candidate_descriptor"]["sha256"] == object_ref.sha256
        assert result["publication_state"] == (
            "rejected" if failure == "ledger" else "uncertain"
        )
        assert catalog.marker_rows == []
        assert ("catalog-cas" in events) is (failure != "ledger")
    else:
        result = operations.save()
        assert result.status == "saved"
        assert events == [
            ("durable-candidate", result.descriptor.to_dict()["logical_jat_id"]),
            "catalog-cas",
            ("committed", result.descriptor.to_dict()["logical_jat_id"]),
        ]


@pytest.mark.parametrize("failure", [1, 2])
def test_content_or_authority_change_before_or_after_workspace_is_not_false_success(
    pipeline, failure
):
    pipeline[0]["fail_check"] = failure
    if failure == 1:
        with pytest.raises(ValueError):
            run(pipeline)
        assert "workspace" not in pipeline[1]
    else:
        result = run(pipeline)
        assert (
            result["ok"] is False
            and result["workspace_result"]["snapshot_id"] == "exact-new-point"
        )
        assert not any(isinstance(v, tuple) and v[0] == "publish" for v in pipeline[1])


def test_cancellation_during_batch_cleanup_preserves_committed_workspace(pipeline):
    pipeline[0]["exit_cancel"] = True
    result = run(pipeline)
    assert (
        result["ok"] is False
        and result["workspace_result"]["snapshot_id"] == "exact-new-point"
    )
    assert result["reconciliation_required"]
