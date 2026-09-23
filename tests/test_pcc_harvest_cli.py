from __future__ import annotations

import json
from pathlib import Path

from josh_room.cli import build_parser
from josh_room.harvest import HarvestController
from josh_room.pcc_enqueue import enqueue_trigger
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.scheduler import (
    SchedulerContext,
    _linux_content,
    _mac_content,
    _windows_command,
    install,
    load_context,
    remove,
    status,
)


def _queued(root: Path) -> PccOutbox:
    outbox = PccOutbox(root)
    enqueue_trigger(
        outbox,
        event_id="event-1",
        session_id="session-1",
        checkpoint={"source": "codex-hook", "representation": "unknown", "start": 0, "end": 0, "prefix_sha256": "0" * 64},
    )
    return outbox


def test_plan_status_inspect_are_content_free(tmp_path):
    controller = HarvestController(_queued(tmp_path / "outbox"), owner_factory=lambda: "worker-1")
    assert controller.plan()["content_free"] is True
    assert controller.status()["queued"] == 1
    inspected = controller.inspect()
    assert inspected["metadata_only"] is True
    assert "path" not in str(inspected)
def test_scheduler_install_status_remove_is_idempotent(tmp_path):
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    context = {
        "profile": "synthetic",
        "codex_active_root": tmp_path / "codex-active",
        "codex_archived_root": tmp_path / "codex-archived",
        "policy_config": tmp_path / "policy.json",
        "config_home": tmp_path / "config",
        "workspace_id": "workspace-synthetic",
        "workspace_path": tmp_path,
        "repository": "https://github.com/example/repo",
        "path_kind": "worktree",
    }
    first = install(platform_name="linux", home=tmp_path, executable=executable, **context)
    second = install(platform_name="linux", home=tmp_path, executable=executable, **context)
    assert first["ok"] is True and first["changed"] is True
    assert second["ok"] is True and second["changed"] is False
    assert "context" not in first and "argv" not in first
    assert str(tmp_path) not in json.dumps(first)
    manifest_path = tmp_path / ".config" / "josh-room" / "pcc-harvest.scheduler.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "context" not in manifest
    assert str(tmp_path) not in manifest_path.read_text(encoding="utf-8")
    assert manifest["context_id"].startswith("ctx-")
    state_path = tmp_path / ".local" / "state" / "josh-room" / "scheduler" / f"{manifest['context_id']}.json"
    state_text = state_path.read_text(encoding="utf-8")
    assert state_path.stat().st_mode & 0o777 == 0o600
    parsed = build_parser().parse_args(manifest["argv"][1:])
    assert parsed.harvest_command == "run"
    assert parsed.scheduler_context_id == manifest["context_id"]
    assert str(tmp_path) not in (tmp_path / ".config" / "systemd" / "user" / "josh-room-pcc-harvest.service").read_text(encoding="utf-8")
    loaded = load_context(manifest["context_id"], home=tmp_path)
    assert loaded.profile == "synthetic"
    assert loaded.codex_active_root == str(context["codex_active_root"])
    assert loaded.codex_archived_root == str(context["codex_archived_root"])
    assert loaded.policy_config == str(context["policy_config"])
    assert loaded.config_home == str(context["config_home"])
    assert loaded.workspace_id == "workspace-synthetic"
    assert loaded.path_kind == "worktree"
    assert str(tmp_path) in state_text
    service = (tmp_path / ".config" / "systemd" / "user" / "josh-room-pcc-harvest.service").read_text(encoding="utf-8")
    assert "--scheduler-context-id" in service
    assert "--codex-active-root" not in service and "--codex-archived-root" not in service
    assert status(platform_name="linux", home=tmp_path)["installed"] is True
    assert remove(platform_name="linux", home=tmp_path)["changed"] is True
    assert not state_path.exists()
    assert remove(platform_name="linux", home=tmp_path)["changed"] is False

def test_scheduler_native_definitions_are_path_free(tmp_path):
    executable = tmp_path / "room$tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    context = SchedulerContext.from_values(
        profile="$PROFILE",
        codex_active_root=tmp_path / "$active",
        codex_archived_root=tmp_path / "$archived",
    )
    service, _ = _linux_content(str(executable), 900, context)
    plist = _mac_content(str(executable), 900, tmp_path, context)
    command = _windows_command(str(executable), 900, context)
    for definition in (service, plist, " ".join(command)):
        assert str(tmp_path) not in definition
    assert "$$PROFILE" not in service
    assert "--scheduler-context-id" in service
    assert "room$$tool" in service


def test_scheduler_install_rejects_missing_context(tmp_path):
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    result = install(platform_name="linux", home=tmp_path, executable=executable)
    assert result == {
        "schema": "josh-room.scheduler",
        "schema_version": {"major": 1, "minor": 0},
        "ok": False,
        "action": "install",
        "platform": "linux",
        "error": "scheduler-context-invalid",
    }
    context = {
        "profile": "synthetic",
        "codex_active_root": tmp_path / "active",
        "codex_archived_root": tmp_path / "archived",
        "repository": "https://user:password@example.com/org/repo",
    }
    credential_result = install(platform_name="linux", home=tmp_path, executable=executable, **context)
    assert credential_result["error"] == "scheduler-context-invalid"
    assert remove(platform_name="linux", home=tmp_path)["changed"] is False




def test_prepare_and_drain_never_prepare_in_drain(tmp_path):
    outbox = _queued(tmp_path / "outbox")

    def prepare(box, record, owner):
        box.transition(record.event_id, owner, QueueState.SOURCE_SNAPSHOTTED)
        return box.prepare_encrypted(record.event_id, owner, b"cipher", metadata={"object_kind": "session-segment"})

    calls = []

    def publish(box, record, owner):
        calls.append(record.event_id)
        box.mark_uploaded(record.event_id, owner, object_key="evidence/objects/sha256/" + record.ciphertext_sha256, ciphertext_size=record.ciphertext_size)
        box.publish_index(record.event_id, owner, index_id="a" * 64)
        return box.commit(record.event_id, owner)

    controller = HarvestController(outbox, prepare=prepare, publish=publish, owner_factory=lambda: "worker-1")
    result = controller.run(offline=True)
    assert result["ok"] is True
    # The #8 release seam makes prepared records immediately drainable.
    assert result["prepared"][0]["resume_state"] == QueueState.PREPARED_ENCRYPTED.value
    assert outbox.inspect_record("event-1").owner is None
    assert controller.status()["prepared"] == 1
    assert calls == []
