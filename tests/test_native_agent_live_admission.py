"""Opt-in real MinIO/native Secret Service acceptance using a disposable fixture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from josh_room import keyring
from josh_room import native_agent_admission as admission
from josh_room import native_agents as native
from josh_room.claude_native_history import project_key
from josh_room.config import DimensionRegistry
from josh_room.encryption_domain import KEYSET_CONTROL_KEY
from josh_room.minio import MinioConfig

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native custody acceptance"),
]
SESSION = "11111111-1111-4111-8111-111111111111"
STAMP = "2026-11-22T16:00:00Z"


def _write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def live_admission(tmp_path, monkeypatch):
    configured = os.environ.get("JOSH_ROOM_TEST_LIVE_AGENT_ROOT")
    if not configured:
        pytest.skip("explicit disposable enrolled MinIO/native Secret Service fixture required")
    base = Path(configured)
    assert base.is_absolute() and base.resolve() == base
    assert base.stat().st_uid == os.getuid() and base.stat().st_mode & 0o077 == 0
    setup = json.loads((base / "provider-setup-receipt.json").read_text())
    assert setup["isolated_secret_service"] is True
    bus = "unix:path=" + str(base / "keyring-runtime/bus")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus)
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_ENCRYPTION", "JOSH_ROOM_ENCRYPTION_HANDOFF"):
        monkeypatch.delenv(name, raising=False)
    status = keyring.backend_status()
    assert status.backend == "linux-secret-service" and status.available and not status.locked
    tmp_path.chmod(0o700)
    home = tmp_path / "host-config"
    home.mkdir(mode=0o700)
    directory = home / "josh-room"
    directory.mkdir(mode=0o700)
    configuration = json.loads((base / "provider-config/config.json").read_text())
    selected = DimensionRegistry(configuration).select("recovery-fixture")
    assert selected.provider == "minio" and selected.encryption_domain_id is None
    endpoint = urlsplit(selected.endpoint)
    assert endpoint.scheme == "http" and endpoint.hostname == "127.0.0.1"
    _write(directory / "config.json", configuration)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(directory))
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://example.invalid/synthetic/native-admission.git"], check=True)
    references = tmp_path / "references"
    references.mkdir(mode=0o700)
    root = tmp_path / "profile"
    keyset = admission._current_keyset(selected)
    recipients = sorted([keyset.operational_recipient, *keyset.recovery_recipients])
    binding = {"dimension": "recovery-fixture", "room": "native-room", "profile": "personal", "workspace": str(workspace)}
    grant = {
        **binding, "profile_root": str(root), "scope": "personal", "tools": ["codex", "claude"],
        "repository": {"host": "example.invalid", "namespace": "synthetic", "name": "native-admission"},
        "local_reference_root": str(references), "encryption_domain_id": keyset.encryption_domain_id,
        "key_generation": keyset.key_generation, "recipients": recipients,
    }
    policy = {"format": admission.FORMAT_V2, "grants": [grant]}
    policy_path = directory / admission.POLICY_FILE
    _write(policy_path, policy)
    assert admission.require_admission(binding, recipients, profile_root=root, operation="configure")["admitted"] is True
    assert native.configure(root, recipients=recipients, **binding)["ok"] is True
    return SimpleNamespace(
        base=base, root=root, workspace=workspace, references=references, binding=binding,
        recipients=recipients, selected=selected, policy=policy, policy_path=policy_path,
    )


def _native_rows(tool, workspace):
    if tool == "codex":
        relative = Path(f"sessions/2026/11/22/rollout-2026-11-22T16-00-00-{SESSION}.jsonl")
        rows = [
            {"timestamp": STAMP, "type": "session_meta", "payload": {
                "id": SESSION, "timestamp": STAMP, "cwd": str(workspace), "originator": "codex_cli_rs",
                "cli_version": "0.171.0", "source": "cli", "model_provider": "offline",
            }},
            {"timestamp": STAMP, "type": "event_msg", "payload": {
                "type": "user_message", "message": "synthetic admission question", "images": None, "local_images": [],
            }},
            {"timestamp": STAMP, "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic retained answer"}],
            }},
        ]
    else:
        relative = Path("projects") / project_key(str(workspace)) / f"{SESSION}.jsonl"
        common = {
            "isSidechain": False, "userType": "external", "cwd": str(workspace), "sessionId": SESSION,
            "version": "2.1.310", "gitBranch": "synthetic", "timestamp": STAMP,
        }
        user = "22222222-2222-4222-8222-222222222222"
        rows = [
            {**common, "parentUuid": None, "type": "user", "uuid": user,
             "message": {"role": "user", "content": "synthetic Claude admission question"}},
            {**common, "parentUuid": user, "type": "assistant", "uuid": "33333333-3333-4333-8333-333333333333",
             "message": {
                 "id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic-offline",
                 "content": [{"type": "text", "text": "synthetic retained Claude answer"}], "stop_reason": "end_turn",
                 "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
             }},
        ]
    return relative, ("".join(json.dumps(row) + "\n" for row in rows)).encode()


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_real_enrolled_native_capture_verify_without_runtime_credentials(live_admission, tmp_path, tool):
    fixture = live_admission
    relative, payload = _native_rows(tool, fixture.workspace)
    source = fixture.root / tool / relative
    source.parent.mkdir(mode=0o700, parents=True)
    for directory in source.parents:
        if directory.is_relative_to(fixture.root):
            directory.chmod(0o700)
    source.write_bytes(payload)
    source.chmod(0o600)
    reference = fixture.references / f"{tool}.age"
    assert native.capture(fixture.root, output=reference, recipients=fixture.recipients, tool=tool, **fixture.binding)["ok"] is True
    identity = keyring.secure_lookup("recovery-fixture-recovery", "age-identity")
    handoff = tmp_path / "transient-verify.identity"
    descriptor = os.open(handoff, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(identity)
        del identity
        result = native.verify(fixture.root, source=reference, identity_paths=[handoff], tool=tool, **fixture.binding)
        assert result["ok"] is True and result["native_resume_verified"] is False
    finally:
        handoff.unlink(missing_ok=True)
    assert source.read_bytes() == payload and not handoff.exists()
    assert not (fixture.root / "codex/auth.json").exists()
    assert not (fixture.root / "claude/.credentials.json").exists()


def test_real_enrolled_native_refuses_revoked_grant_and_stale_generation(live_admission):
    fixture = live_admission
    grant = fixture.policy["grants"][0]
    grant["tools"] = ["codex"]
    _write(fixture.policy_path, fixture.policy)
    with pytest.raises(admission.AdmissionError, match="native-admission-tool-not-authorized"):
        admission.require_admission(fixture.binding, fixture.recipients, profile_root=fixture.root, operation="discover", tool="claude")
    grant["tools"] = ["codex", "claude"]
    grant["key_generation"] += 1
    _write(fixture.policy_path, fixture.policy)
    with pytest.raises(admission.AdmissionError, match="native-admission-keyset-mismatch"):
        admission.require_admission(fixture.binding, fixture.recipients, profile_root=fixture.root, operation="discover")


@pytest.mark.skipif(os.environ.get("JOSH_ROOM_TEST_LIVE_AGENT_KEYSET_REVOCATION") != "1", reason="exclusive disposable-provider keyset mutation opt-in required")
def test_real_enrolled_native_refuses_missing_current_keyset(live_admission):
    fixture = live_admission
    config = MinioConfig.from_dimension(fixture.selected)
    client = admission._native_client(config)
    response = client.get_object(Bucket=config.bucket, Key=KEYSET_CONTROL_KEY)
    original = response["Body"].read()
    response["Body"].close()
    try:
        client.delete_object(Bucket=config.bucket, Key=KEYSET_CONTROL_KEY)
        with pytest.raises(admission.AdmissionError, match="native-admission-(keyset-unavailable|unavailable)"):
            admission.require_admission(fixture.binding, fixture.recipients, profile_root=fixture.root, operation="discover")
    finally:
        client.put_object(Bucket=config.bucket, Key=KEYSET_CONTROL_KEY, Body=original)
        del original
    assert admission.require_admission(fixture.binding, fixture.recipients, profile_root=fixture.root, operation="discover")["admitted"] is True
