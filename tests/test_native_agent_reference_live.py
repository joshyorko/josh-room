"""Opt-in durable Room references against a disposable MinIO/native Secret Service.

Never point this test at an existing user bucket, profile, home or keyring.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import keyring
from josh_room import native_agent_reference as references
from josh_room.native_agent_reference_cli import _context

SESSION = "11111111-1111-4111-8111-111111111111"
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native custody/materialization")]


def _json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _history(workspace, tool):
    stamp = "2026-11-22T16:00:00Z"
    if tool == "codex":
        rows = [
            {"timestamp": stamp, "type": "session_meta", "payload": {"id": SESSION, "timestamp": stamp, "cwd": str(workspace), "originator": "codex_cli_rs", "cli_version": "0.171.0", "source": "cli", "model_provider": "offline"}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "user_message", "message": "synthetic native question", "images": None, "local_images": []}},
            {"timestamp": stamp, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic retained answer"}]}},
        ]
        relative = Path(f"sessions/2026/11/22/rollout-2026-11-22T16-00-00-{SESSION}.jsonl")
    else:
        from josh_room.claude_native_history import project_key

        common = {"isSidechain": False, "userType": "external", "cwd": str(workspace), "sessionId": SESSION, "version": "2.1.310", "gitBranch": "synthetic", "timestamp": stamp}
        user = "22222222-2222-4222-8222-222222222222"
        rows = [
            {**common, "parentUuid": None, "type": "user", "uuid": user, "message": {"role": "user", "content": "synthetic native question"}},
            {**common, "parentUuid": user, "type": "assistant", "uuid": "33333333-3333-4333-8333-333333333333", "message": {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic-offline", "content": [{"type": "text", "text": "synthetic retained answer"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]
        relative = Path("projects") / project_key(str(workspace)) / f"{SESSION}.jsonl"
    return relative, ("".join(json.dumps(row) + "\n" for row in rows)).encode()


@pytest.fixture
def live(tmp_path, monkeypatch):
    selected = os.environ.get("JOSH_ROOM_TEST_REFERENCE_ROOT")
    if not selected:
        pytest.skip("explicit disposable enrolled MinIO/native Secret Service fixture required")
    base = Path(selected)
    assert base.is_absolute() and base.resolve() == base and base.stat().st_mode & 0o077 == 0
    setup = json.loads((base / "provider-setup-receipt.json").read_text())
    assert setup["isolated_secret_service"] is True
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + str(base / "keyring-runtime/bus"))
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_ENCRYPTION", "JOSH_ROOM_ENCRYPTION_HANDOFF", "JOSH_ROOM_ENCRYPTION_MATERIAL", "JOSH_ROOM_IDENTITY", "JOSH_ROOM_EXTENSION_MODE", "JOSH_ROOM_JAT_ARTIFACT", "JOSH_ROOM_JAT_RCC_EXE", "JOSH_ROOM_RCC_HOME", "JOSH_ROOM_INSTANCE", "JOSH_ROOM_SELECTED_DOMAIN", "JOSH_ROOM_SELECTED_RECIPIENTS"):
        monkeypatch.delenv(name, raising=False)
    assert keyring.backend_status().available and not keyring.backend_status().locked
    tmp_path.chmod(0o700)
    directory = tmp_path / "host-config" / "josh-room"
    directory.mkdir(mode=0o700, parents=True)
    configuration = json.loads((base / "native-config/josh-room/config.json").read_text())
    _json(directory / "config.json", configuration)
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(directory))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(directory.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(base / "provider-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(base / "provider-cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(base / "provider-runtime"))
    monkeypatch.setenv("JOSH_ROOM_JAT_ROOT", str(tmp_path / "jat-runtime"))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "synthetic.txt").write_text("synthetic original workspace\n")
    git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    for args in (["git", "init", "-q", str(workspace)], ["git", "-C", str(workspace), "remote", "add", "origin", "https://example.invalid/synthetic/reference.git"]):
        subprocess.run(args, check=True, env=git_env, capture_output=True)
    room = "agent-ref-" + uuid.uuid4().hex[:12]
    source_policy = json.loads((base / "native-config/josh-room/native-agent-admission.json").read_text())
    original = next(grant for grant in source_policy["grants"] if grant["room"] == "native-room")
    root = tmp_path / "profile"
    local_refs = tmp_path / "references"
    local_refs.mkdir(mode=0o700)
    grant = {**original, "room": room, "profile": "personal", "workspace": str(workspace), "profile_root": str(root), "local_reference_root": str(local_refs), "repository": {"host": "example.invalid", "namespace": "synthetic", "name": "reference"}, "allow_room_reference": True}
    policy = {"format": "josh-room.native-agent-admission.v3", "grants": [grant]}
    _json(directory / "native-agent-admission.json", policy)
    repo = Path(__file__).resolve().parents[1]

    def cli(*arguments, ok=True):
        result = subprocess.run([sys.executable, "-m", "josh_room", *map(str, arguments), "--json"], env={**os.environ, "PYTHONPATH": str(repo / "src")}, capture_output=True, text=True, timeout=180, check=False)
        try:
            receipt = json.loads(result.stdout)
        except ValueError:
            pytest.fail("controller returned a non-JSON result")
        assert (result.returncode == 0) is ok, {"operation": list(map(str, arguments[:3])), "error": receipt.get("error")}
        return receipt

    def save_policy():
        _json(directory / "native-agent-admission.json", policy)

    def configure(target_room, target_root):
        arguments = []
        for recipient in grant["recipients"]:
            arguments.extend(["--recipient", recipient])
        return cli("agents", "configure", "--dimension", "recovery-fixture", "--room", target_room, "--profile", "personal", "--workspace", workspace, "--profile-root", target_root, *arguments)

    def profile_ref(target_room=room):
        return cli("agents", "room-status", "--dimension", "recovery-fixture", "--room", target_room, "--workspace", workspace)["profiles"][0]["profile_ref"]

    configure(room, root)
    originals = {}
    for tool in ("codex", "claude"):
        relative, data = _history(workspace, tool)
        target = root / tool / relative
        target.parent.mkdir(parents=True, mode=0o700)
        for parent in target.parents:
            if parent.is_relative_to(root):
                parent.chmod(0o700)
        target.write_bytes(data)
        target.chmod(0o600)
        originals[tool] = (relative, data)
    point = cli("snapshot", "create", room, "--dimension", "recovery-fixture", "--backend", "minio", "--source", workspace)["snapshot_id"]
    common = ["--dimension", "recovery-fixture", "--room", room, "--snapshot", point]
    assert cli("agents", "recovery-status", *common)["state"] == "reference-absent"
    selector = profile_ref()
    for tool in ("codex", "claude"):
        receipt = cli("agents", "recovery-capture", *common, "--workspace", workspace, "--profile-ref", selector, "--tool", tool)
        assert receipt["history_only"] and not receipt["native_resume_verified"]
    return SimpleNamespace(base=base, tmp=tmp_path, cli=cli, room=room, workspace=workspace, point=point, common=common, policy=policy, grant=grant, save_policy=save_policy, configure=configure, profile_ref=profile_ref, originals=originals)


def _selected(live, room=None, point=None):
    return _context(SimpleNamespace(dimension="recovery-fixture", room=room or live.room, snapshot=point or live.point))


def test_real_capture_cold_room_copy_and_reference_removal(live):
    value = live
    with _selected(value) as context:
        original, _ = references._load(context)
        source_objects = {entry["object"]["key"] for entry in original["entries"]}
        assert set(context.selected_descriptor.to_dict()["components"]) == {"rcc_environment", "homebrew_recovery", "hauler_content"}
        for entry in original["entries"]:
            assert b"synthetic native question" not in references._blob(context, entry)
    for tool in ("codex", "claude"):
        receipt = value.cli("agents", "recovery-verify", *value.common, "--workspace", value.workspace, "--profile-ref", value.profile_ref(), "--tool", tool)
        assert receipt["sessions"] == [{"provider_session_id": SESSION, "tool": tool, "cwd_matches": True}]
    # A newer point must not silently replace the selected older reference.
    (value.workspace / "synthetic.txt").write_text("synthetic newer workspace\n")
    newer = value.cli("snapshot", "create", value.room, "--dimension", "recovery-fixture", "--backend", "minio", "--source", value.workspace)["snapshot_id"]
    assert newer != value.point
    result = value.cli("agents", "recovery-restore", *value.common, "--workspace", value.workspace, "--profile-ref", value.profile_ref(), "--tool", "codex", "--session-id", SESSION, ok=False)
    assert result["state"] == "blocked"
    value.workspace.rename(value.tmp / "before-hydrate")
    value.cli("hydrate", value.room, "--snapshot", value.point, "--dimension", "recovery-fixture", "--backend", "minio", "--destination", value.workspace, "--ide", "terminal")
    for tool in ("codex", "claude"):
        target = value.tmp / ("cold-" + tool)
        value.grant["profile_root"] = str(target)
        value.save_policy()
        value.configure(value.room, target)
        flags = [*value.common, "--workspace", value.workspace, "--profile-ref", value.profile_ref(), "--tool", tool, "--session-id", SESSION]
        receipt = value.cli("agents", "recovery-restore", *flags)
        relative, original_bytes = value.originals[tool]
        assert (target / tool / relative).read_bytes() == original_bytes
        assert receipt["history_only"] and receipt["native_resume_verified"] is False
        assert value.cli("agents", "recovery-restore", *flags)["recovered_commit"] is True
    copied_room = value.room + "-copy"
    copied_root = value.tmp / "copied-profile"
    copied_grant = {**value.grant, "room": copied_room, "profile_root": str(copied_root)}
    value.policy["grants"].append(copied_grant)
    value.save_policy()
    value.configure(copied_room, copied_root)
    copied = value.cli("snapshot", "copy", value.room, "--source-dimension", "recovery-fixture", "--snapshot", value.point, "--destination-dimension", "recovery-fixture", "--destination-room", copied_room)
    assert copied["data_uploaded"] > 0 and copied["agent_recovery"]["history_only"] is True
    copied_point = copied["logical_jat_id"]
    with _selected(value, copied_room, copied_point) as context:
        document, _ = references._load(context)
        copied_objects = {entry["object"]["key"] for entry in document["entries"]}
        assert source_objects.isdisjoint(copied_objects)
        assert document["binding"]["room_id"] == copied_room
        assert all(entry["origin"]["room_id"] == value.room for entry in document["entries"])
    value.cli("rooms", "remove", value.room, "--dimension", "recovery-fixture", "--backend", "minio")
    copied_common = ["--dimension", "recovery-fixture", "--room", copied_room, "--snapshot", copied_point]
    for tool in ("codex", "claude"):
        result = value.cli("agents", "recovery-verify", *copied_common, "--workspace", value.workspace, "--profile-ref", value.profile_ref(copied_room), "--tool", tool)
        assert result["sessions"][0]["provider_session_id"] == SESSION
    report = value.cli("room-store", "reconcile", "--dimension", "recovery-fixture")
    assert "agent_recovery" in report
    value.cli("rooms", "remove", copied_room, "--dimension", "recovery-fixture", "--backend", "minio")


@pytest.mark.skipif(not os.environ.get("JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME"), reason="explicit privately acquired pinned JAT runtime receipt required")
def test_real_normal_export_companion_and_native_restore(live, monkeypatch):
    configured = os.environ.get("JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME")
    if not configured:
        pytest.skip("explicit privately acquired pinned JAT runtime receipt required")
    runtime = json.loads(Path(configured).read_text())
    artifact = runtime["jat"]["artifact"]
    assert artifact == "sha256:c4ba60ca02c0cfc3e73c07e420039c713d0e6a9d1d42a8bfaeac14388f5c5424"
    for name, value in {
        "JOSH_ROOM_EXTENSION_MODE": "1", "JOSH_ROOM_RCC_EXE": runtime["rcc"]["executable"],
        "JOSH_ROOM_JAT_RCC_EXE": runtime["rcc"]["executable"], "JOSH_ROOM_JAT_ARTIFACT": artifact,
        "JOSH_ROOM_RCC_HOME": runtime["paths"]["rccHome"], "ROBOCORP_HOME": runtime["paths"]["rccHome"],
        "JOSH_ROOM_JAT_ROOT": runtime["jat"]["jatRoot"],
    }.items():
        monkeypatch.setenv(name, value)
    output = live.tmp / "native-history.haul.tar.zst"
    result = live.cli("snapshot", "export", live.room, "--snapshot", live.point, "--dimension", "recovery-fixture", "--backend", "minio", "--output", output)
    companion = Path(str(output) + ".agents.age")
    assert output.is_file() and companion.is_file()
    assert result["status"] == "exported" and result["agent_recovery"]["history_only"] is True
    assert result["agent_recovery"]["output"] == str(companion)
    assert b"synthetic native question" not in companion.read_bytes()
    for tool in ("codex", "claude"):
        target = live.tmp / ("export-cold-" + tool)
        live.grant["profile_root"] = str(target)
        live.save_policy()
        live.configure(live.room, target)
        flags = [*live.common, "--workspace", live.workspace, "--profile-ref", live.profile_ref(), "--tool", tool, "--export", companion]
        verified = live.cli("agents", "recovery-verify", *flags)
        assert verified["sessions"][0]["provider_session_id"] == SESSION
        restored = live.cli("agents", "recovery-restore", *flags, "--session-id", SESSION)
        relative, data = live.originals[tool]
        assert (target / tool / relative).read_bytes() == data
        assert restored["native_resume_verified"] is False
    live.cli("rooms", "remove", live.room, "--dimension", "recovery-fixture", "--backend", "minio")


@pytest.mark.skipif(os.environ.get("JOSH_ROOM_TEST_REFERENCE_CROSS_DIMENSION") != "1", reason="explicit disposable bucket creation/copy acceptance required")
def test_real_cross_dimension_rewrap_and_cold_restore(live):
    from josh_room import native_agent_admission as authority
    from josh_room.config import DimensionRegistry

    dimension = "agent-copy-" + uuid.uuid4().hex[:12]
    live.cli("buckets", "create", "--connection", "recovery-fixture-connection", "--bucket", dimension)
    live.cli("dimensions", "add", dimension, "--display-name", "Synthetic Copy", "--provider", "minio", "--connection", "recovery-fixture-connection", "--bucket", dimension)
    identity = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True).stdout
    keyring.store_value(dimension + "-recovery", "age-identity", identity)
    handoff = live.tmp / "recovery.identity"
    handoff.write_text(identity)
    handoff.chmod(0o600)
    del identity
    try:
        live.cli("encryption", "initialize", "--dimension", dimension, "--recovery-handoff", handoff)
    finally:
        handoff.unlink(missing_ok=True)
    directory = Path(os.environ["JOSH_ROOM_CONFIG_DIR"])
    selected = DimensionRegistry(json.loads((directory / "config.json").read_text())).select(dimension)
    keyset = authority._current_keyset(selected)
    destination_room = live.room + "-cross"
    target = live.tmp / "cross-profile"
    grant = {**live.grant, "dimension": dimension, "room": destination_room, "profile_root": str(target),
             "encryption_domain_id": keyset.encryption_domain_id, "key_generation": keyset.key_generation,
             "recipients": [keyset.operational_recipient, *keyset.recovery_recipients]}
    live.policy["grants"].append(grant)
    live.save_policy()
    copied = live.cli("snapshot", "copy", live.room, "--source-dimension", "recovery-fixture", "--snapshot", live.point,
                      "--destination-dimension", dimension, "--destination-room", destination_room)
    assert copied["agent_recovery"]["data_uploaded"] > 0
    point = copied["logical_jat_id"]
    with _selected(live) as context:
        source_document, _ = references._load(context)
    with _context(SimpleNamespace(dimension=dimension, room=destination_room, snapshot=point)) as context:
        document, _ = references._load(context)
        assert document["binding"]["encryption_domain_id"] != source_document["binding"]["encryption_domain_id"]
        assert document["binding"]["physical_binding"] != source_document["binding"]["physical_binding"]
        assert {entry["object"]["key"] for entry in document["entries"]}.isdisjoint({entry["object"]["key"] for entry in source_document["entries"]})
        assert all(entry["origin"] == source_document["binding"] for entry in document["entries"])
    recips = [argument for recipient in grant["recipients"] for argument in ("--recipient", recipient)]
    live.cli("agents", "configure", "--dimension", dimension, "--room", destination_room, "--profile", "personal",
             "--workspace", live.workspace, "--profile-root", target, *recips)
    live.workspace.rename(live.tmp / "before-cross-hydrate")
    live.cli("hydrate", destination_room, "--snapshot", point, "--dimension", dimension, "--backend", "minio", "--destination", live.workspace, "--ide", "terminal")
    selector = live.cli("agents", "room-status", "--dimension", dimension, "--room", destination_room, "--workspace", live.workspace)["profiles"][0]["profile_ref"]
    result = live.cli("agents", "recovery-restore", "--dimension", dimension, "--room", destination_room, "--snapshot", point,
                      "--workspace", live.workspace, "--profile-ref", selector, "--tool", "codex", "--session-id", SESSION)
    relative, original = live.originals["codex"]
    assert (target / "codex" / relative).read_bytes() == original
    assert result["native_resume_verified"] is False
    live.cli("rooms", "remove", live.room, "--dimension", "recovery-fixture", "--backend", "minio")
    live.cli("agents", "recovery-verify", "--dimension", dimension, "--room", destination_room, "--snapshot", point,
             "--workspace", live.workspace, "--profile-ref", selector, "--tool", "codex")
    live.cli("rooms", "remove", destination_room, "--dimension", dimension, "--backend", "minio")
