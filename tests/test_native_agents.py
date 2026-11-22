from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from josh_room import native_agents as agents

pytestmark = pytest.mark.skipif(not agents.sys.platform.startswith("linux"), reason="Linux-only initial native profile contract")


@pytest.fixture
def profile(tmp_path, identities):
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    binding = {"dimension": "synthetic-dimension", "room": "demo-room", "profile": "synthetic-profile", "workspace": workspace}
    root = tmp_path / "profile"
    agents.configure(root, recipients=identities[1], **binding)
    return root, binding


def rollout(root, binding, *, text="synthetic turn"):
    target = root / "codex/sessions/2026/11/22/rollout-synthetic-session.jsonl"
    current = target.parent
    parents = []
    while not current.exists():
        parents.append(current)
        current = current.parent
    for path in reversed(parents):
        path.mkdir(mode=0o700)
    rows = [{"type": "session_meta", "payload": {"id": "019aaa00-0000-7000-8000-000000000001", "cwd": str(binding["workspace"]), "cli_version": "0.171.0"}}, {"type": "event_msg", "payload": {"message": text}}]
    target.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    target.chmod(0o600)
    return target


@pytest.fixture
def identities(tmp_path):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age and age-keygen unavailable")
    paths = []
    recipients = []
    for index in range(2):
        path = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
        path.chmod(0o600)
        recipients.append(subprocess.run(["age-keygen", "-y", str(path)], check=True, capture_output=True, text=True).stdout.strip())
        paths.append(path)
    return paths, recipients


def test_status_is_content_free_and_does_not_create_paths(profile):
    root, binding = profile
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    result = agents.status(root, **binding)
    assert result["state"] == "configured"
    assert result["providers"]["t3"]["capture"] == "blocked"
    assert result["native_resume_verified"] is False
    assert str(binding["workspace"]) not in json.dumps(result)
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_scope_and_path_binding_fail_closed(profile, tmp_path):
    root, binding = profile
    with pytest.raises(agents.NativeAgentError, match="binding-mismatch"):
        agents.status(root, **{**binding, "dimension": "other"})
    with pytest.raises(agents.NativeAgentError, match="collision"):
        agents.configure(root, **binding)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(agents.NativeAgentError, match="unsafe-path"):
        agents.status(link, **binding)


def test_existing_busy_profile_is_not_touched(profile):
    root, binding = profile
    with agents._lock(root):
        assert agents.status(root, **binding)["state"] == "busy"
        with pytest.raises(agents.NativeAgentError, match="busy"):
            agents.launch(root, **binding, executable="python")


def test_real_age_roundtrip_preserves_native_bytes_but_excludes_auth_and_t3(profile, identities, tmp_path):
    root, binding = profile
    paths, recipients = identities
    target = rollout(root, binding)
    expected = target.read_bytes()
    (root / "codex/auth.json").write_text("excluded synthetic auth sentinel")
    (root / "t3/statev2.sqlite").write_text("excluded synthetic database sentinel")
    source = tmp_path / "retained.age"
    captured = agents.capture(root, output=source, recipients=recipients, **binding)
    assert captured["files"] == 1
    assert source.read_bytes().startswith(b"age-encryption.org/v1")
    assert b"synthetic turn" not in source.read_bytes()
    assert agents.verify(root, source=source, identity_paths=paths[:1], **binding)["files"] == 1
    restored = tmp_path / "restored"
    result = agents.restore(restored, source=source, identity_paths=paths[1:], **binding)
    assert result["native_resume_verified"] is False
    assert (restored / target.relative_to(root)).read_bytes() == expected
    assert not (restored / "codex/auth.json").exists()
    assert list((restored / "t3").iterdir()) == []
    assert agents.status(restored, **binding)["state"] == "configured"
    with pytest.raises(agents.NativeAgentError, match="collision"):
        agents.restore(restored, source=source, identity_paths=paths, **binding)
    with pytest.raises(agents.NativeAgentError):
        agents.verify(root, source=source, identity_paths=paths, **{**binding, "room": "wrong"})
    assert not list(tmp_path.glob(".native-*"))


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions", "path", "credential", "invalid-json", "unknown-file"])
def test_capture_rejects_unsafe_material_before_reference(profile, identities, tmp_path, unsafe):
    root, binding = profile
    _, recipients = identities
    target = rollout(root, binding)
    if unsafe == "symlink":
        copy = tmp_path / "copy"
        target.rename(copy)
        target.symlink_to(copy)
    elif unsafe == "hardlink":
        os.link(target, tmp_path / "linked-rollout")
    elif unsafe == "permissions":
        target.chmod(0o644)
    elif unsafe == "path":
        target.write_text(target.read_text().replace(str(binding["workspace"]), "/workspaces/another-room"))
    elif unsafe == "credential":
        rollout(root, binding, text="-----BEGIN OPENSSH PRIVATE KEY-----")
    elif unsafe == "invalid-json":
        target.write_text("not native JSONL")
    else:
        target.rename(target.with_name("auth.json"))
    output = tmp_path / "must-not-exist.age"
    with pytest.raises(agents.NativeAgentError):
        agents.capture(root, output=output, recipients=recipients, **binding)
    assert not output.exists()


def test_restore_tampering_and_wrong_identity_leave_target_absent(profile, identities, tmp_path):
    root, binding = profile
    paths, recipients = identities
    rollout(root, binding)
    source = tmp_path / "recovery.age"
    agents.capture(root, output=source, recipients=recipients, **binding)
    source.write_bytes(source.read_bytes()[:-12] + b"tampered-end")
    target = tmp_path / "new-profile"
    with pytest.raises(agents.NativeAgentError):
        agents.restore(target, source=source, identity_paths=paths, **binding)
    assert not target.exists()
    assert not list(tmp_path.glob(".native-*"))


def test_launch_env_reaches_actual_child_not_parent(profile, tmp_path):
    root, binding = profile
    executable = tmp_path / "synthetic-t3"
    output = tmp_path / "child-result.json"
    executable.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(" + repr(str(output)) + ").write_text(json.dumps({'argv':sys.argv[1:],'env':{k:os.environ[k] for k in ['T3CODE_HOME','CODEX_HOME','CLAUDE_CONFIG_DIR']}}))\n")
    executable.chmod(0o700)
    previous = os.environ.get("T3CODE_HOME")
    assert agents.launch(root, executable=str(executable), **binding)["ok"]
    result = json.loads(output.read_text())
    assert result["argv"][0:3] == ["serve", "--base-dir", str(root / "t3")]
    assert result["env"] == {"T3CODE_HOME": str(root / "t3"), "CODEX_HOME": str(root / "codex"), "CLAUDE_CONFIG_DIR": str(root / "claude")}
    assert os.environ.get("T3CODE_HOME") == previous


def test_windows_is_explicitly_unqualified(monkeypatch, tmp_path):
    monkeypatch.setattr(agents.sys, "platform", "win32")
    with pytest.raises(agents.NativeAgentError, match="platform-not-qualified"):
        agents.configure(tmp_path / "absent", dimension="demo", room="demo", profile="demo", workspace=tmp_path)


@pytest.mark.parametrize("change", ["version", "identity", "row-type", "extra-field", "quoted-credential", "escaped-secret", "duplicate-field"])
def test_native_format_and_decoded_credentials_fail_closed(profile, identities, tmp_path, change):
    root, binding = profile
    target = rollout(root, binding)
    rows = [json.loads(row) for row in target.read_text().splitlines()]
    if change == "version":
        rows[0]["payload"]["cli_version"] = "unsupported-future-version"
    elif change == "identity":
        rows[0]["payload"]["id"] = "arbitrary-export-text"
    elif change == "row-type":
        rows[1]["type"] = "arbitrary-export"
    elif change == "extra-field":
        rows[1]["injected"] = "unknown field"
    elif change == "quoted-credential":
        rows[1]["payload"]["access_token"] = "synthetic-token-must-be-rejected"
    elif change == "escaped-secret":
        rows[1]["payload"]["message"] = "-----BEGIN OPENSSH PRIVATE KEY-----"
    data = "\n".join(json.dumps(row, ensure_ascii=True) for row in rows) + "\n"
    if change == "escaped-secret":
        data = data.replace("PRIVATE", "\\u0050RIVATE")
    if change == "duplicate-field":
        data = data.replace('"type": "event_msg"', '"type": "event_msg", "type": "event_msg"')
    target.write_text(data)
    output = tmp_path / "blocked.age"
    with pytest.raises(agents.NativeAgentError):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()


def test_capture_cannot_change_configured_recipient_scope(profile, identities, tmp_path):
    root, binding = profile
    rollout(root, binding)
    changed = [identities[1][0], "age1" + "a" * 58]
    with pytest.raises(agents.NativeAgentError, match="recipient-mismatch"):
        agents.capture(root, output=tmp_path / "wrong-scope.age", recipients=changed, **binding)


def test_restore_collision_race_keeps_existing_empty_directory(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    rollout(root, binding)
    source = tmp_path / "retained.age"
    agents.capture(root, output=source, recipients=identities[1], **binding)
    target = tmp_path / "occupied-after-validation"
    initialize = agents._initialize

    def race(staging, bound, recipients):
        initialize(staging, bound, recipients)
        target.mkdir(mode=0o700)

    monkeypatch.setattr(agents, "_initialize", race)
    with pytest.raises(agents.NativeAgentError, match="collision"):
        agents.restore(target, source=source, identity_paths=identities[0], **binding)
    assert target.is_dir() and list(target.iterdir()) == []
    assert not list(tmp_path.glob(".native-*"))
