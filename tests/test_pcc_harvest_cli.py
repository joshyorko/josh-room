from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from josh_room import scheduler
from josh_room.cli import build_parser
from josh_room.harvest import HarvestController
from josh_room.pcc_enqueue import enqueue_trigger
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.scheduler import (
    SchedulerContext,
    _activate,
    _linux_content,
    _mac_content,
    _windows_command,
    install,
    launch_context,
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


def _installed_scheduler(tmp_path: Path, platform_name: str) -> tuple[Path, ...]:
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    launcher = tmp_path / ".local" / "bin" / "josh-room"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    result = install(
        platform_name=platform_name,
        home=tmp_path,
        executable=executable,
        profile="synthetic",
        codex_active_root=tmp_path / "codex-active",
        codex_archived_root=tmp_path / "codex-archived",
        policy_config=tmp_path / "policy.json",
        config_home=tmp_path / "config",
        workspace_id="workspace-synthetic",
        workspace_path=tmp_path,
        repository="https://github.com/example/repo",
        path_kind="worktree",
    )
    assert result["ok"] is True
    manifest = tmp_path / ".config" / "josh-room" / "pcc-harvest.scheduler.json"
    context_id = json.loads(manifest.read_text(encoding="utf-8"))["context_id"]
    state = tmp_path / ".local" / "state" / "josh-room" / "scheduler" / f"{context_id}.json"
    native = (
        (tmp_path / ".config" / "systemd" / "user" / "josh-room-pcc-harvest.service", tmp_path / ".config" / "systemd" / "user" / "josh-room-pcc-harvest.timer")
        if platform_name == "linux"
        else (tmp_path / "Library" / "LaunchAgents" / "dev.josh-room.pcc-harvest.plist",)
    )
    if platform_name != "linux":
        native[0].parent.mkdir(parents=True)
        native[0].write_text("plist", encoding="utf-8")
    return (*native, manifest, state)


def _native_scheduler_fixture(tmp_path: Path, platform_name: str) -> Path:
    path = tmp_path / f"{platform_name}-native"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize(
    ("platform_name", "message"),
    (
        ("linux", "Failed to stop josh-room-pcc-harvest.timer: Access denied."),
        ("darwin", "launchctl: permission denied"),
    ),
)
def test_scheduler_remove_preserves_definitions_when_deactivation_fails(tmp_path, monkeypatch, platform_name, message):
    targets = _installed_scheduler(tmp_path, platform_name)
    before = {path: path.read_bytes() for path in targets}
    monkeypatch.setattr("josh_room.scheduler._home", lambda: tmp_path)
    monkeypatch.setattr(
        scheduler,
        "_SYSTEMCTL" if platform_name == "linux" else "_LAUNCHCTL",
        _native_scheduler_fixture(tmp_path, platform_name),
    )

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=message)

    monkeypatch.setattr("josh_room.scheduler.subprocess.run", fail)
    result = remove(platform_name=platform_name, home=tmp_path)
    assert result["ok"] is False
    assert result["error"] == "scheduler-remove-failed"
    assert result["activation"] == "deactivation-failed"
    assert result["removed"] is False and result["changed"] is False
    assert {path: path.read_bytes() for path in targets} == before


@pytest.mark.parametrize(
    ("platform_name", "message"),
    (
        ("linux", "Failed to stop josh-room-pcc-harvest.timer: Unit josh-room-pcc-harvest.timer not loaded."),
        ("darwin", 'Could not find service "dev.josh-room.pcc-harvest" in domain for user.'),
    ),
)
def test_scheduler_remove_treats_verified_absence_as_idempotent(tmp_path, monkeypatch, platform_name, message):
    targets = _installed_scheduler(tmp_path, platform_name)
    monkeypatch.setattr("josh_room.scheduler._home", lambda: tmp_path)

    def absent(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=message)

    monkeypatch.setattr("josh_room.scheduler.subprocess.run", absent)
    monkeypatch.setattr(
        scheduler,
        "_SYSTEMCTL" if platform_name == "linux" else "_LAUNCHCTL",
        _native_scheduler_fixture(tmp_path, platform_name),
    )
    first = remove(platform_name=platform_name, home=tmp_path)
    second = remove(platform_name=platform_name, home=tmp_path)
    assert first["ok"] is True and first["changed"] is True
    assert first["activation"] == "inactive"
    assert second["ok"] is True and second["changed"] is False
    assert all(not path.exists() for path in targets)



def test_scheduler_native_tools_ignore_path_shadowing(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted-systemctl"
    trusted.write_text("#!/bin/sh\n", encoding="utf-8")
    trusted.chmod(0o700)
    shadow = tmp_path / "shadow" / "systemctl"
    shadow.parent.mkdir()
    shadow.write_text("#!/bin/sh\n", encoding="utf-8")
    shadow.chmod(0o700)
    monkeypatch.setattr(scheduler, "_SYSTEMCTL", trusted)
    monkeypatch.setenv("PATH", str(shadow.parent))
    monkeypatch.setattr(scheduler, "_home", lambda: tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(scheduler.subprocess, "run", run)
    assert scheduler._native_scheduler_executable("linux") == str(trusted)
    assert _activate("linux", tmp_path, ()) == "active"
    assert scheduler._deactivate("linux", tmp_path) == "inactive"
    for path in scheduler._linux_paths(tmp_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("definition", encoding="utf-8")
    monkeypatch.setattr(scheduler, "_manifest_state", lambda home: (True, True))
    assert status(platform_name="linux", home=tmp_path)["active"] is True
    assert [argv[0] for argv in calls] == [str(trusted)] * 4


def test_plan_status_inspect_are_content_free(tmp_path):
    controller = HarvestController(_queued(tmp_path / "outbox"), owner_factory=lambda: "worker-1")
    assert controller.plan()["content_free"] is True
    assert controller.status()["queued"] == 1
    inspected = controller.inspect()
    assert inspected["metadata_only"] is True
    assert "path" not in str(inspected)
def test_scheduler_install_status_remove_is_idempotent(tmp_path, monkeypatch):
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    launcher = tmp_path / ".local" / "bin" / "josh-room"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
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
    conflicting = tmp_path / "conflicting"
    conflicting.write_text("#!/bin/sh\n", encoding="utf-8")
    conflicting.chmod(0o700)
    with pytest.raises(ValueError, match="scheduler executable"):
        load_context(manifest["context_id"], home=tmp_path, executable=conflicting)
    calls = []
    monkeypatch.setattr("josh_room.scheduler.os.execv", lambda path, argv: calls.append((path, argv)))
    launch_context(
        manifest["context_id"],
        home=tmp_path,
        current_executable=conflicting,
        argv=["harvest", "run", "--scheduler-context-id", manifest["context_id"]],
    )
    assert calls == [(str(executable), [str(executable), "harvest", "run", "--scheduler-context-id", manifest["context_id"]])]
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
    assert "$PROFILE" not in service and "$$PROFILE" not in service
    assert "--scheduler-context-id" in service
    assert "%h/.local/bin/josh-room" in service
    assert "josh-room" in plist
    assert "$HOME/.local/bin:$HOME/bin" in plist

def test_scheduler_windows_is_truthfully_unsupported(tmp_path):
    assert install(platform_name="windows", home=tmp_path)["error"] == "scheduler-unsupported-platform"
    assert status(platform_name="windows", home=tmp_path)["error"] == "scheduler-unsupported-platform"
    assert remove(platform_name="windows", home=tmp_path)["error"] == "scheduler-unsupported-platform"


def test_scheduler_install_rejects_missing_launcher(tmp_path):
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    result = install(platform_name="linux", home=tmp_path, executable=executable)
    assert result["error"] == "scheduler-launcher-unavailable"

def test_scheduler_install_rejects_missing_context(tmp_path):
    executable = tmp_path / "josh-room"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    launcher = tmp_path / ".local" / "bin" / "josh-room"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
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
