"""Opt-in actual Save CLI against disposable MinIO/native Secret Service only.

Uses the enrolled, credentials-free setup contract of the reference live tests.
Fault wrappers delegate real writes before injecting lost responses/readback errors.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import cli as controller
from josh_room import native_agent_reference as references
from josh_room.minio import MinioBackend
from josh_room.native_agent_reference_cli import _context
from josh_room.r2 import R2PublicationError

pytest_plugins = ["test_native_agent_reference_live"]
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native custody")]


def test_real_save_two_tool_history_and_exact_uncertain_retry(live, monkeypatch):
    from botocore.client import BaseClient

    value = live
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_VERSION", "0.1.52")
    selection = {"format": "josh-room.save-agent-selection.v1", "entries": [
        {"tool": tool, "profile_ref": value.profile_ref()} for tool in ("codex", "claude")]}
    components = {"format_version": 1, "components": {
        name: "omit" for name in ("rcc_environment", "homebrew_recovery", "hauler_content")}}
    common = ["--dimension", "recovery-fixture", "--backend", "minio", "--source", value.workspace,
              "--component-selection", json.dumps(components), "--agent-selection", json.dumps(selection)]

    def preview():
        result = value.cli("snapshot", "preview", value.room, *common)
        assert result["agent_plan"]["format"] == "josh-room.save-agent-plan.v2"
        return result

    def arguments(plan):
        return ["snapshot", "create", value.room, *common, "--agent-plan", json.dumps(plan["agent_plan"]),
                "--component-plan-token", plan["component_plan_token"]]

    def local(plan, *, ok):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = controller.main([*map(str, arguments(plan)), "--json"])
        result = json.loads(out.getvalue())
        assert (code == 0) is ok, result.get("error")
        return result

    mutations = []
    call = BaseClient._make_api_call

    def observe(client, operation, parameters):
        if operation.startswith(("Put", "Delete", "Copy", "Create", "Upload", "Complete", "Abort")):
            mutations.append(operation)
        return call(client, operation, parameters)

    plan = preview()
    with monkeypatch.context() as patch:
        patch.setattr(BaseClient, "_make_api_call", observe)
        noop = local(plan, ok=True)
    assert noop["snapshot_id"] == value.point and mutations == []
    assert all(entry["state"] == "reused" and entry["data_uploaded"] == 0 for entry in noop["agent_result"]["entries"])
    relative, original = value.originals["codex"]
    native_file = Path(value.grant["profile_root"]) / "codex" / relative
    ids = {value.capture_ids["codex"]}
    for payload in (original.replace(b"synthetic retained answer", b"synthetic changed answer"), original):
        native_file.write_bytes(payload)
        result = value.cli(*arguments(preview()))
        assert result["snapshot_id"] == value.point
        ids.add(next(entry["capture_id"] for entry in result["agent_result"]["entries"] if entry["tool"] == "codex"))
    assert len(ids) == 3
    (value.workspace / "synthetic.txt").write_text("synthetic workspace B\n")
    plan = preview()
    put_catalog = MinioBackend.conditional_catalog_put
    accepted = []

    def lost_ack(backend, body, etag):
        result = put_catalog(backend, body, etag)
        accepted.append(True)
        if len(accepted) == 1:
            raise R2PublicationError("synthetic lost ACK after real catalog PUT", published=None)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(MinioBackend, "conditional_catalog_put", lost_ack)
        partial = local(plan, ok=False)
    reserved = plan["agent_plan"]["workspace_candidate_id"]
    assert partial["candidate_logical_jat_id"] == reserved
    value.cli("snapshots", "remove", value.room, value.point, "--dimension", "recovery-fixture", "--backend", "minio")
    retry = value.cli(*arguments(plan))
    assert retry["snapshot_id"] == reserved
    assert {entry["capture_id"] for entry in retry["agent_result"]["entries"]} == {
        entry["new_point_capture_id"] for entry in plan["agent_plan"]["entries"]}

    # Force a real new workspace result so public reserved B can be corroborated
    # even after first-grant revocation makes the encrypted journal inaccessible.
    (value.workspace / "synthetic.txt").write_text("synthetic workspace C\n")
    plan = preview()
    put, get = MinioBackend.put_bytes, MinioBackend.get_bytes
    objects, failed = [], []

    def uploaded(backend, key, body):
        result = put(backend, key, body)
        objects.append(key)
        return result

    def unreadable(backend, key, *args, **kwargs):
        if key in objects and len(objects) == 2 and not failed:
            failed.append(key)
            raise OSError("synthetic accepted native object readback failure")
        return get(backend, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(MinioBackend, "put_bytes", uploaded)
        patch.setattr(MinioBackend, "get_bytes", unreadable)
        partial = local(plan, ok=False)
    first, second = partial["agent_result"]["entries"]
    assert first["data_uploaded"] > 0 and second["data_uploaded"] is None and second["upload_uncertain"]
    grant = value.policy["grants"].pop()
    value.save_policy()
    mutations.clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(BaseClient, "_make_api_call", observe)
            blocked = local(plan, ok=False)
        assert mutations == []
        assert blocked["workspace_result"] is None
        assert blocked["agent_result"]["logical_jat_id"] == plan["agent_plan"]["workspace_candidate_id"]
        assert blocked["agent_result"]["candidate_point_verified"] is True
        assert blocked["agent_result"]["workspace_committed"] is None
        assert all(entry["data_uploaded"] is None and entry["upload_uncertain"] for entry in blocked["agent_result"]["entries"])
    finally:
        value.policy["grants"].append(grant)
        value.save_policy()
    retry = value.cli(*arguments(plan))
    assert retry["snapshot_id"] == plan["agent_plan"]["workspace_candidate_id"]
    with _context(SimpleNamespace(dimension="recovery-fixture", room=value.room, snapshot=retry["snapshot_id"])) as context:
        assert references._pending_objects(context) == (set(), [])
        assert len(references._load(context)[0]["entries"]) == 2
