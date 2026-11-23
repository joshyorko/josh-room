from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import native_agents as agents

pytestmark = pytest.mark.skipif(not agents.sys.platform.startswith("linux"), reason="Linux-only initial native profile contract")


@pytest.fixture
def profile(tmp_path, identities, monkeypatch):
    monkeypatch.setattr(agents, "_admit", lambda *args, **kwargs: {"admitted": True, "authority_digest": "synthetic-authority"})
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    binding = {"dimension": "synthetic-dimension", "room": "demo-room", "profile": "synthetic-profile", "workspace": workspace}
    root = tmp_path / "profile"
    agents.configure(root, recipients=identities[1], **binding)
    return root, binding


def rollout(root, binding, *, text="synthetic turn"):
    target = root / "codex/sessions/2026/11/22/rollout-2026-11-22T15-00-00-019aaa00-0000-7000-8000-000000000001.jsonl"
    current = target.parent
    parents = []
    while not current.exists():
        parents.append(current)
        current = current.parent
    for path in reversed(parents):
        path.mkdir(mode=0o700)
    rows = [{"timestamp": "2026-11-22T15:00:00Z", "type": "session_meta", "payload": {"id": "019aaa00-0000-7000-8000-000000000001", "timestamp": "2026-11-22T15:00:00Z", "cwd": str(binding["workspace"]), "originator": "codex_cli_rs", "cli_version": "0.171.0", "source": "cli"}}, {"timestamp": "2026-11-22T15:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": text, "images": None, "local_images": []}}]
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


def test_real_age_capture_verifies_native_bytes_but_restore_fails_closed(profile, identities, tmp_path):
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
    decoded, _ = agents._decode(source, paths[:1], agents._binding(**binding))
    assert decoded == {target.relative_to(root / "codex").as_posix(): expected}
    restored = tmp_path / "restored"
    with pytest.raises(agents.NativeAgentError, match="crash-safe-materialization-unavailable"):
        agents.restore(restored, source=source, identity_paths=paths, **binding)
    assert not restored.exists()
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
        rollout(root, binding, text=("-" * 5 + "BEGIN OPENSSH " + "PRIVATE KEY" + "-" * 5))
    elif unsafe == "invalid-json":
        target.write_text("not native JSONL")
    else:
        target.rename(target.with_name("auth.json"))
    output = tmp_path / "must-not-exist.age"
    with pytest.raises(agents.NativeAgentError):
        agents.capture(root, output=output, recipients=recipients, **binding)
    assert not output.exists()


def test_verify_rejects_ciphertext_tampering(profile, identities, tmp_path):
    root, binding = profile
    paths, recipients = identities
    rollout(root, binding)
    source = tmp_path / "recovery.age"
    agents.capture(root, output=source, recipients=recipients, **binding)
    source.write_bytes(source.read_bytes()[:-12] + b"tampered-end")
    target = tmp_path / "new-profile"
    with pytest.raises(agents.NativeAgentError):
        agents.verify(root, source=source, identity_paths=paths, **binding)
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
        rows[1]["payload"]["message"] = ("-" * 5 + "BEGIN OPENSSH " + "PRIVATE KEY" + "-" * 5)
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


def test_restore_refuses_before_decryption_or_plaintext_creation(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("blocked restore must not decrypt or create plaintext")

    monkeypatch.setattr(agents, "_decode", forbidden)
    monkeypatch.setattr(agents, "_initialize", forbidden)
    target = tmp_path / "absent"
    with pytest.raises(agents.NativeAgentError, match="crash-safe-materialization-unavailable"):
        agents.restore(target, source=tmp_path / "not-read.age", identity_paths=[], dimension="untrusted")
    assert list(tmp_path.iterdir()) == []


def test_cancel_foreground_launch_stops_owned_process_and_releases_lock(profile, tmp_path):
    root, binding = profile
    executable = tmp_path / "synthetic-long-running-t3"
    pid_path = tmp_path / "synthetic-child.pid"
    executable.write_text("#!/usr/bin/env python3\nimport os,time\nfrom pathlib import Path\nPath(" + repr(str(pid_path)) + ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
    executable.chmod(0o700)
    script = "from pathlib import Path; from josh_room import native_agents as agents; agents._admit=lambda *a,**k: {}; agents.launch(Path(" + repr(str(root)) + "), executable=" + repr(str(executable)) + ", dimension='synthetic-dimension',room='demo-room',profile='synthetic-profile',workspace=Path(" + repr(str(binding["workspace"])) + "))"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(agents.__file__).parents[1])
    process = subprocess.Popen([sys.executable, "-c", script], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text())
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert agents.status(root, **binding)["state"] == "configured"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_capture_refuses_unmanaged_open_writer_before_output(profile, identities, tmp_path):
    root, binding = profile
    target = rollout(root, binding)
    output = tmp_path / "never-published.age"
    with target.open("ab"), pytest.raises(agents.NativeAgentError, match="writer-active"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()


def test_writer_arriving_during_encryption_prevents_publication(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    target = rollout(root, binding)
    output = tmp_path / "never-published.age"
    original = agents.subprocess.Popen

    def attempted_writer(*args, **kwargs):
        with pytest.raises(BlockingIOError):
            descriptor = os.open(target, os.O_WRONLY | os.O_NONBLOCK)
            os.close(descriptor)
        return original(*args, **kwargs)

    monkeypatch.setattr(agents.subprocess, "Popen", attempted_writer)
    with pytest.raises(agents.NativeAgentError, match="writer-active"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()
    assert not list(tmp_path.glob(".native-encrypted-*"))


def test_source_replacement_after_lease_admission_is_rejected(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    target = rollout(root, binding)
    original_entries = agents._entries

    def replace_before_read(root, binding, barrier, expected, tool="codex"):
        replacement = target.with_name("replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, target)
        return original_entries(root, binding, barrier, expected, tool)

    monkeypatch.setattr(agents, "_entries", replace_before_read)
    output = tmp_path / "never-published.age"
    with pytest.raises(agents.NativeAgentError, match="source-changed"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()


def test_missing_host_admission_blocks_before_native_content(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    monkeypatch.undo()
    config_home = tmp_path / "missing-host-authority"
    config_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    def forbidden(*args, **kwargs):
        pytest.fail("native content must not be inventoried without host admission")

    monkeypatch.setattr(agents, "_inventory", forbidden)
    output = tmp_path / "never-published.age"
    with pytest.raises(agents.NativeAgentError, match="admission"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()


def test_changed_authority_during_capture_prevents_publication(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    rollout(root, binding)
    calls = []

    def changed(*args, **kwargs):
        calls.append(kwargs["operation"])
        return {"authority_sha256": str(len(calls))}

    monkeypatch.setattr(agents, "_admit", changed)
    output = tmp_path / "never-published.age"
    with pytest.raises(agents.NativeAgentError, match="admission-changed"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert calls == ["capture", "capture"]
    assert not output.exists()


def test_new_native_file_during_encryption_prevents_publication(profile, identities, tmp_path, monkeypatch):
    root, binding = profile
    target = rollout(root, binding)
    original = agents.subprocess.Popen

    def new_file(*args, **kwargs):
        added = target.with_name("rollout-2026-11-22T15-00-00-22222222-2222-4222-8222-222222222222.jsonl")
        added.write_bytes(target.read_bytes())
        added.chmod(0o600)
        return original(*args, **kwargs)

    monkeypatch.setattr(agents.subprocess, "Popen", new_file)
    output = tmp_path / "never-published.age"
    with pytest.raises(agents.NativeAgentError, match="source-changed"):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()


def test_single_session_cold_materialization_and_completed_commit_recovery(profile, identities, tmp_path):
    root, binding = profile
    target = rollout(root, binding)
    payload = target.read_bytes()
    reference = tmp_path / "retained.age"
    agents.capture(root, output=reference, recipients=identities[1], **binding)
    cold = tmp_path / "cold-profile"
    agents.configure(cold, recipients=identities[1], **binding)
    options = {"source": reference, "identity_paths": identities[0][:1], "session_id": "019aaa00-0000-7000-8000-000000000001", **binding}
    result = agents.materialize_session(cold, **options)
    assert result["state"] == "history-materialized" and result["native_resume_verified"] is False
    restored = cold / target.relative_to(root)
    assert restored.read_bytes() == payload
    assert agents.materialize_session(cold, **options)["recovered_commit"] is True
    restored.write_bytes(b"existing different user state")
    with pytest.raises(agents.NativeAgentError, match="target-mismatch"):
        agents.materialize_session(cold, **options)
    assert restored.read_bytes() == b"existing different user state"


def test_single_session_materialization_refuses_used_profile(profile, identities, tmp_path):
    root, binding = profile
    rollout(root, binding)
    reference = tmp_path / "retained.age"
    agents.capture(root, output=reference, recipients=identities[1], **binding)
    cold = tmp_path / "cold-profile"
    agents.configure(cold, recipients=identities[1], **binding)
    (cold / "t3/userdata").mkdir(mode=0o700)
    with pytest.raises(agents.NativeAgentError, match="profile-not-fresh"):
        agents.materialize_session(cold, source=reference, identity_paths=identities[0][:1], session_id="019aaa00-0000-7000-8000-000000000001", **binding)
    assert list((cold / "codex").iterdir()) == []


def _bound_reference_fixture(profile, identities, tmp_path):
    root, binding = profile
    original = rollout(root, binding)
    source = tmp_path / "bound-history.age"
    agents.capture(root, output=source, recipients=identities[1], **binding)
    destination_binding = {**binding, "dimension": "copied-dimension", "room": "copied-room", "profile": "copied-profile"}
    destination = tmp_path / "bound-profile"
    agents.configure(destination, recipients=identities[1], **destination_binding)
    options = {"source": source, "identity_paths": identities[0][:1], "archive_binding": binding,
               "session_id": "019aaa00-0000-7000-8000-000000000001", **destination_binding}
    return root, original, destination, options


def test_bound_materialization_preserves_native_identity_under_destination_authority(profile, identities, tmp_path, monkeypatch):
    root, original, destination, options = _bound_reference_fixture(profile, identities, tmp_path)
    expected = original.read_bytes()
    admissions = []

    def admitted(binding, recipients, **kwargs):
        admissions.append((binding, recipients, kwargs))
        return {"admitted": True, "authority_sha256": "synthetic-destination-authority"}

    monkeypatch.setattr(agents, "_admit", admitted)
    result = agents._materialize_bound_session(destination, **options)
    restored = destination / original.relative_to(root)
    assert restored.read_bytes() == expected
    assert result["state"] == "history-materialized" and result["native_resume_verified"] is False
    assert len(admissions) >= 2
    for binding, recipients, operation in admissions:
        assert binding["dimension"] == "copied-dimension" and binding["room"] == "copied-room"
        assert binding["profile"] == "copied-profile" and binding["workspace"] == str(options["workspace"])
        assert recipients == sorted(identities[1])
        assert operation == {"root": destination, "operation": "room-materialize", "destination": options["source"], "tool": "codex"}
    assert agents._materialize_bound_session(destination, **options)["recovered_commit"] is True
    restored.write_bytes(b"existing different native state")
    with pytest.raises(agents.NativeAgentError, match="target-mismatch"):
        agents._materialize_bound_session(destination, **options)
    assert restored.read_bytes() == b"existing different native state"


def test_bound_materialization_accepts_separately_rewrapped_destination_recipients(profile, identities, tmp_path):
    root, original, destination, options = _bound_reference_fixture(profile, identities, tmp_path)
    expected = original.read_bytes()
    document = json.loads(agents._decrypt_to_memory(options["source"], identities[0][:1]))
    keys, recipients = [], []
    for index in range(2):
        key = tmp_path / f"destination-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
        key.chmod(0o600)
        keys.append(key)
        recipients.append(subprocess.run(["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True).stdout.strip())
    document["recipients"] = sorted(recipients)
    policy = json.loads((destination / agents.POLICY).read_bytes())
    policy["recipients"] = sorted(recipients)
    (destination / agents.POLICY).write_text(json.dumps(policy))
    copied = tmp_path / "destination-history.age"
    with copied.open("wb") as encrypted:
        subprocess.run(["age", "-r", recipients[0], "-r", recipients[1]], input=json.dumps(document).encode(), stdout=encrypted, stderr=subprocess.DEVNULL, check=True)
    copied.chmod(0o600)
    result = agents._materialize_bound_session(destination, **{**options, "source": copied, "identity_paths": keys[:1]})
    assert result["native_resume_verified"] is False
    assert (destination / original.relative_to(root)).read_bytes() == expected


@pytest.mark.parametrize("change", ["workspace", "recipient", "stale-authority", "busy", "archive-binding"])
def test_bound_materialization_refuses_mismatched_or_unavailable_destination(profile, identities, tmp_path, monkeypatch, change):
    root, original, destination, options = _bound_reference_fixture(profile, identities, tmp_path)
    expected = original.read_bytes()
    if change == "workspace":
        other = tmp_path / "other-workspace"
        other.mkdir(mode=0o700)
        options["archive_binding"] = {**options["archive_binding"], "workspace": other}
        reason = "path-mismatch"
    elif change == "recipient":
        policy = json.loads((destination / agents.POLICY).read_bytes())
        policy["recipients"] = ["age1" + "a" * 58, "age1" + "b" * 58]
        (destination / agents.POLICY).write_text(json.dumps(policy))
        reason = "recipient-mismatch"
    elif change == "archive-binding":
        options["archive_binding"] = {**options["archive_binding"], "room": "unverified-room"}
        reason = "invalid-recovery-manifest"
    elif change == "stale-authority":
        calls = []

        def changed(*args, **kwargs):
            calls.append(kwargs)
            return {"authority_sha256": str(len(calls))}

        monkeypatch.setattr(agents, "_admit", changed)
        reason = "admission-denied"
    else:
        with agents._lock(destination), pytest.raises(agents.NativeAgentError, match="profile-busy"):
            agents._materialize_bound_session(destination, **options)
        assert not list((destination / "codex").iterdir())
        return
    with pytest.raises(agents.NativeAgentError, match=reason):
        agents._materialize_bound_session(destination, **options)
    assert not (destination / original.relative_to(root)).exists()
    assert original.read_bytes() == expected


def test_decryption_memory_helper_enforces_limit_without_plaintext_staging(profile, identities, tmp_path):
    root, binding = profile
    original = rollout(root, binding)
    reference = tmp_path / "memory-only.age"
    agents.capture(root, output=reference, recipients=identities[1], **binding)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    data = agents._decrypt_to_memory(reference, identities[0][:1])
    assert json.loads(data)["binding"]["room"] == binding["room"]
    assert agents._decrypt_to_memory(reference, identities[0][:1], maximum=len(data)) == data
    with pytest.raises(agents.NativeAgentError, match="native-history-limit"):
        agents._decrypt_to_memory(reference, identities[0][:1], maximum=len(data) - 1)
    for invalid in (0, -1, True, agents.MAX_BYTES * 2 + 1):
        with pytest.raises(agents.NativeAgentError, match="native-decryption-limit-invalid"):
            agents._decrypt_to_memory(reference, identities[0][:1], maximum=invalid)
    assert before == {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert original.read_bytes()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_killed_native_session_materialization_recovers_absent_or_complete(profile, identities, tmp_path, phase):
    import select

    root, binding = profile
    target = rollout(root, binding)
    expected = target.read_bytes()
    reference = tmp_path / "retained.age"
    agents.capture(root, output=reference, recipients=identities[1], **binding)
    cold = tmp_path / "cold-profile"
    agents.configure(cold, recipients=identities[1], **binding)
    script = """import sys,time
from pathlib import Path
from josh_room import native_agents as agents,native_agent_materialization as materialization
agents._admit=lambda *a,**k: {}
actual=materialization._link_anonymous
phase=sys.argv[5]
def interrupted_link(*args):
    if phase=='after': actual(*args)
    print('ready',flush=True)
    time.sleep(60)
materialization._link_anonymous=interrupted_link
agents.materialize_session(Path(sys.argv[1]),source=Path(sys.argv[2]),identity_paths=[Path(sys.argv[3])],session_id='019aaa00-0000-7000-8000-000000000001',dimension='synthetic-dimension',room='demo-room',profile='synthetic-profile',workspace=Path(sys.argv[4]))
"""
    environment = dict(os.environ, PYTHONPATH=str(Path(agents.__file__).parents[1]))
    child = subprocess.Popen([sys.executable, "-c", script, str(cold), str(reference), str(identities[0][0]), str(binding["workspace"]), phase], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == b"ready"
        child.kill()
        child.wait(timeout=5)
        final = cold / target.relative_to(root)
        if phase == "before":
            assert not final.exists()
        else:
            assert final.read_bytes() == expected
        result = agents.materialize_session(cold, source=reference, identity_paths=identities[0][:1], session_id="019aaa00-0000-7000-8000-000000000001", **binding)
        assert result["native_resume_verified"] is False
        assert final.read_bytes() == expected
        assert not list(cold.rglob(".native-*"))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize("change", ["outer-timestamp", "metadata-timestamp", "originator", "filename-identity", "filename-date", "ordinal-negative", "ordinal-bool", "ordinal-overflow", "nan", "infinite-exponent"])
def test_capture_rejects_unparseable_native_codex_contract(profile, identities, tmp_path, change):
    root, binding = profile
    target = rollout(root, binding)
    rows = [json.loads(row) for row in target.read_text().splitlines()]
    if change == "outer-timestamp":
        rows[1].pop("timestamp")
    elif change == "metadata-timestamp":
        rows[0]["payload"].pop("timestamp")
    elif change == "originator":
        rows[0]["payload"].pop("originator")
    elif change == "filename-identity":
        moved = target.with_name(target.name.replace("019aaa00", "029aaa00"))
        target.rename(moved)
        target = moved
    elif change == "filename-date":
        moved = target.with_name(target.name.replace("15-00-00", "25-00-00"))
        target.rename(moved)
        target = moved
    elif change in {"nan", "infinite-exponent"}:
        rows[1]["payload"]["synthetic_number"] = float("nan")
    else:
        rows[1]["ordinal"] = {"ordinal-negative": -1, "ordinal-bool": True, "ordinal-overflow": 2**64}[change]
    serialized = "\n".join(json.dumps(row) for row in rows) + "\n"
    if change == "infinite-exponent":
        serialized = serialized.replace("NaN", "1e9999")
    target.write_text(serialized)
    output = tmp_path / "not-published.age"
    with pytest.raises(agents.NativeAgentError):
        agents.capture(root, output=output, recipients=identities[1], **binding)
    assert not output.exists()
