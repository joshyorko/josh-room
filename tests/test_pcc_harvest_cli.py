from __future__ import annotations

import json
from pathlib import Path

from josh_room.cli import build_parser
from josh_room.harvest import HarvestController
from josh_room.pcc_enqueue import enqueue_trigger
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.scheduler import SchedulerContext, _linux_content, install, remove, status


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
        "repository": "github.com/example/repo",
        "path_kind": "worktree",
    }
    first = install(platform_name="linux", home=tmp_path, executable=executable, **context)
    second = install(platform_name="linux", home=tmp_path, executable=executable, **context)
    assert first["ok"] is True and first["changed"] is True
    assert second["ok"] is True and second["changed"] is False
    assert "context" not in first and "argv" not in first
    assert str(tmp_path) not in json.dumps(first)
    manifest = json.loads((tmp_path / ".config" / "josh-room" / "pcc-harvest.scheduler.json").read_text(encoding="utf-8"))
    assert manifest["context"]["profile"] == "synthetic"
    parsed = build_parser().parse_args(manifest["argv"][1:])
    assert parsed.harvest_command == "run"
    assert parsed.profile == "synthetic"
    assert parsed.codex_active_root == context["codex_active_root"]
    assert parsed.codex_archived_root == context["codex_archived_root"]
    assert parsed.policy_config == context["policy_config"]
    assert parsed.config_home == context["config_home"]
    assert parsed.workspace_id == "workspace-synthetic"
    assert parsed.path_kind == "worktree"
    service = (tmp_path / ".config" / "systemd" / "user" / "josh-room-pcc-harvest.service").read_text(encoding="utf-8")
    assert "--profile synthetic" in service
    assert "--codex-active-root" in service and "--codex-archived-root" in service
    assert status(platform_name="linux", home=tmp_path)["installed"] is True
    assert remove(platform_name="linux", home=tmp_path)["changed"] is True
    assert remove(platform_name="linux", home=tmp_path)["changed"] is False


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


def test_scheduler_systemd_argv_escapes_expansion(tmp_path):
    executable = tmp_path / "room$tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    context = SchedulerContext.from_values(
        profile="$PROFILE",
        codex_active_root=tmp_path / "$active",
        codex_archived_root=tmp_path / "$archived",
    )
    service, _ = _linux_content(str(executable), 900, context)
    assert "$$PROFILE" in service
    assert "$$active" in service and "$$archived" in service
    assert "room$$tool" in service


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
