"""Room status never exposes roots; explicit discovery revalidates host authority."""

from __future__ import annotations

import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from josh_room import native_agent_admission as admission
from josh_room import native_agent_discovery as discovery
from josh_room import native_agents as native
from josh_room import native_agents_cli
from josh_room.claude_native_history import CLAUDE_VERSION, project_key

pytestmark = pytest.mark.skipif(not native.sys.platform.startswith("linux"), reason="Linux native profile contract")
SESSION = "11111111-1111-4111-8111-111111111111"
_REAL_ADMIT = native._admit


def write(path, document):
    path.write_text(json.dumps(document))
    path.chmod(0o600)


@pytest.fixture
def authorized(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    references = tmp_path / "references"
    references.mkdir(mode=0o700)
    config_home = tmp_path / "host-config"
    config_home.mkdir(mode=0o700)
    directory = config_home / "josh-room"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq", "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"]
    binding = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": str(workspace)}
    root = tmp_path / "private-profile"
    grant = {**binding, "repository": {"host": "example.invalid", "namespace": "synthetic", "name": "demo"}, "scope": "personal", "profile_root": str(root), "local_reference_root": str(references), "encryption_domain_id": "11111111-1111-4111-8111-111111111111", "key_generation": 1, "recipients": recipients, "tools": ["codex", "claude"]}
    policy = {"format": admission.FORMAT_V2, "grants": [grant]}
    write(directory / admission.POLICY_FILE, policy)
    native._initialize(root, binding, recipients)
    target = root / f"codex/sessions/2026/11/22/rollout-2026-11-22T15-00-00-{SESSION}.jsonl"
    target.parent.mkdir(mode=0o700, parents=True)
    for parent in target.parents:
        if parent.is_relative_to(root):
            parent.chmod(0o700)
    rows = [{"timestamp": "2026-11-22T15:00:00Z", "type": "session_meta", "payload": {"id": SESSION, "timestamp": "2026-11-22T15:00:00Z", "cwd": str(workspace), "originator": "codex_cli_rs", "cli_version": native.CODEX_VERSION, "source": "cli"}}, {"timestamp": "2026-11-22T15:00:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "private synthetic body must never reach output", "images": None, "local_images": []}}]
    target.write_text("".join(json.dumps(row) + "\n" for row in rows))
    target.chmod(0o600)
    monkeypatch.setattr(native, "_admit", lambda *args, **kwargs: {"admitted": True, "authority_sha256": "synthetic-authority"})
    return SimpleNamespace(root=root, workspace=workspace, directory=directory, policy=policy, grant=grant, binding=binding, target=target)


def status(case):
    return discovery.room_status(dimension="synthetic", room="demo", workspace=case.workspace)


def discover(case, **changes):
    reference = status(case)["profiles"][0]["profile_ref"]
    options = {"dimension": "synthetic", "room": "demo", "workspace": case.workspace, "profile_ref": reference, "tool": "codex"}
    options.update(changes)
    return discovery.discover(**options)


def test_room_status_is_offline_content_free_and_root_free(authorized, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline Room status must not inspect histories or contact providers")

    monkeypatch.setattr(native, "_inventory", forbidden)
    monkeypatch.setattr(native, "_admit", forbidden)
    monkeypatch.setattr(admission, "_current_keyset", forbidden)
    before = sorted(path.relative_to(authorized.root).as_posix() for path in authorized.root.rglob("*"))
    result = status(authorized)
    profile = result["profiles"][0]
    assert profile["state"] == "configured" and profile["scope"] == "personal"
    assert profile["tools"] == ["claude", "codex"] and len(profile["profile_ref"]) == 64
    encoded = json.dumps(result)
    assert str(authorized.root) not in encoded and str(authorized.workspace) not in encoded
    assert "private synthetic body" not in encoded
    assert profile["admission"] == "revalidation-required"
    assert profile["room_reference_allowed"] is False
    assert all(not value["selectable"] for value in result["capabilities"]["providers"].values())
    assert before == sorted(path.relative_to(authorized.root).as_posix() for path in authorized.root.rglob("*"))


@pytest.mark.parametrize("scope,flag,expected", [("personal", True, True), ("oss", True, True), ("homelab", True, True), ("personal", False, False), ("work", True, False)])
def test_room_status_reports_only_explicit_supported_reference_opt_in(authorized, monkeypatch, scope, flag, expected):
    authorized.policy["format"] = admission.FORMAT_V3
    authorized.grant.update(scope=scope, profile=scope, allow_room_reference=flag)
    authorized.binding["profile"] = scope
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    write(authorized.root / native.POLICY, {"format": native.FORMAT, "binding": authorized.binding, "recipients": authorized.grant["recipients"]})

    def forbidden(*args, **kwargs):
        pytest.fail("status opt-in metadata must remain offline")

    monkeypatch.setattr(native, "_admit", forbidden)
    profile = status(authorized)["profiles"][0]
    assert profile["room_reference_allowed"] is expected
    assert profile["tools"] == ["claude", "codex"]
    assert profile["state"] == "configured"
    assert str(authorized.root) not in json.dumps(profile)
    assert "allow_room_reference" not in profile


def test_profile_selector_is_invalidated_when_room_opt_in_is_revoked(authorized):
    authorized.policy["format"] = admission.FORMAT_V3
    authorized.grant["allow_room_reference"] = True
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    reference = status(authorized)["profiles"][0]["profile_ref"]
    options = {"dimension": "synthetic", "room": "demo", "workspace": authorized.workspace, "profile_ref": reference, "tool": "claude"}
    assert discovery.resolve_profile(**options)["profile_root"] == str(authorized.root)
    authorized.grant["allow_room_reference"] = False
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    with pytest.raises(native.NativeAgentError, match="selector-stale-or-unavailable"):
        discovery.resolve_profile(**options)
    updated = status(authorized)["profiles"][0]
    assert updated["profile_ref"] != reference and updated["room_reference_allowed"] is False


def test_v3_local_discovery_still_returns_only_metadata_when_reference_opt_in_is_false(authorized):
    authorized.policy["format"] = admission.FORMAT_V3
    authorized.grant["allow_room_reference"] = False
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    assert discover(authorized)["sessions"][0]["provider_session_id"] == SESSION


@pytest.mark.parametrize("flag", [0, 1, "true", None])
def test_room_status_refuses_malformed_reference_opt_in(authorized, flag):
    authorized.policy["format"] = admission.FORMAT_V3
    authorized.grant["allow_room_reference"] = flag
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    assert status(authorized)["profiles"] == []


def test_missing_or_other_room_grant_is_unavailable_without_mutation(authorized):
    result = discovery.room_status(dimension="synthetic", room="another-room", workspace=authorized.workspace)
    assert result["profiles"] == [] and result["reason"] == "native-room-profile-unavailable"
    (authorized.directory / admission.POLICY_FILE).unlink()
    assert status(authorized)["reason"] == "native-room-authority-unavailable"


def test_status_refuses_workspace_owned_policy(authorized, monkeypatch):
    monkeypatch.setattr(admission, "policy_config_dir", lambda: authorized.workspace)
    result = status(authorized)
    assert result["profiles"] == [] and result["reason"] == "native-room-authority-unavailable"


def test_explicit_discovery_returns_ids_only_and_never_claims_t3_restoration(authorized):
    result = discover(authorized)
    assert result["state"] == "history-discovered" and result["scope"] == "personal"
    assert result["sessions"] == [{"provider_session_id": SESSION, "tool": "codex", "cwd_matches": True, "record_count": 2, "native_resume_verified": False, "t3_import": {"state": "requires-verified-owned-server", "new_t3_thread_id": None, "original_t3_identity_restored": False}}]
    encoded = json.dumps(result)
    for private in (str(authorized.root), str(authorized.workspace), str(authorized.target), "private synthetic body"):
        assert private not in encoded
    assert result["native_resume_verified"] is False and result["original_t3_identity_restored"] is False


@pytest.mark.parametrize("binding_matches", [True, False])
def test_claude_discovery_validates_native_history_without_exposing_content(authorized, binding_matches):
    target = authorized.root / "claude/projects" / project_key(str(authorized.workspace)) / f"{SESSION}.jsonl"
    target.parent.mkdir(mode=0o700, parents=True)
    for parent in target.parents:
        if parent.is_relative_to(authorized.root):
            parent.chmod(0o700)
    user_id = "22222222-2222-4222-8222-222222222222"
    common = {"isSidechain": False, "userType": "external", "cwd": str(authorized.workspace) if binding_matches else "/workspaces/other", "sessionId": SESSION, "version": CLAUDE_VERSION, "timestamp": "2026-11-22T16:00:00Z"}
    rows = [
        {**common, "parentUuid": None, "type": "user", "uuid": user_id,
         "message": {"role": "user", "content": "private synthetic Claude question"}},
        {**common, "parentUuid": user_id, "type": "assistant", "uuid": "33333333-3333-4333-8333-333333333333",
         "message": {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic-offline", "content": [{"type": "text", "text": "private synthetic Claude answer"}], "stop_reason": "end_turn"}},
    ]
    target.write_text("".join(json.dumps(row) + "\n" for row in rows))
    target.chmod(0o600)
    if not binding_matches:
        with pytest.raises(native.NativeAgentError, match="claude-native-binding-mismatch"):
            discover(authorized, tool="claude")
        return
    result = discover(authorized, tool="claude")
    assert result["tool"] == "claude" and result["sessions"][0]["provider_session_id"] == SESSION
    assert result["sessions"][0]["record_count"] == 2
    assert result["sessions"][0]["t3_import"]["new_t3_thread_id"] is None
    encoded = json.dumps(result)
    assert str(target) not in encoded and str(authorized.workspace) not in encoded and "private synthetic" not in encoded


def test_stale_profile_selector_is_not_authority(authorized):
    reference = status(authorized)["profiles"][0]["profile_ref"]
    authorized.grant["scope"] = "oss"
    authorized.grant["profile"] = "oss"
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    with pytest.raises(native.NativeAgentError, match="selector-stale-or-unavailable"):
        discovery.discover(dimension="synthetic", room="demo", workspace=authorized.workspace, profile_ref=reference, tool="codex")


def test_legacy_grants_do_not_implicitly_enable_claude(authorized):
    authorized.policy["format"] = admission.FORMAT
    authorized.grant.pop("tools")
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    assert status(authorized)["profiles"][0]["tools"] == ["codex"]
    with pytest.raises(native.NativeAgentError, match="tool-not-authorized"):
        discover(authorized, tool="claude")


def test_discovery_refuses_busy_profile_without_unlocking(authorized):
    with native._lock(authorized.root):
        assert status(authorized)["profiles"][0]["state"] == "busy"
        with pytest.raises(native.NativeAgentError, match="profile-busy"):
            discover(authorized)
    assert status(authorized)["profiles"][0]["state"] == "configured"


def test_discovery_revalidates_authority_before_returning_ids(authorized, monkeypatch):
    calls = []

    def changing(*args, **kwargs):
        calls.append(kwargs["operation"])
        return {"authority_sha256": str(len(calls))}

    monkeypatch.setattr(native, "_admit", changing)
    with pytest.raises(native.NativeAgentError, match="admission-changed"):
        discover(authorized)
    assert calls == ["discover", "discover"]


def test_discovery_rejects_revoked_selector_during_read(authorized, monkeypatch):
    entries = native._entries

    def revoke(*args, **kwargs):
        value = entries(*args, **kwargs)
        authorized.policy["grants"] = []
        write(authorized.directory / admission.POLICY_FILE, authorized.policy)
        return value

    monkeypatch.setattr(native, "_entries", revoke)
    with pytest.raises(native.NativeAgentError, match="authority-invalid"):
        discover(authorized)


def test_duplicate_native_identity_is_unavailable(authorized):
    reverted = authorized.target.with_name(authorized.target.stem + "_22222222-2222-4222-8222-222222222222.jsonl")
    reverted.write_bytes(authorized.target.read_bytes())
    reverted.chmod(0o600)
    with pytest.raises(native.NativeAgentError, match="identity-ambiguous"):
        discover(authorized)


def test_work_discovery_keeps_local_scope_and_no_export(authorized):
    authorized.grant.update(profile="work", scope="work")
    authorized.binding["profile"] = "work"
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    write(authorized.root / native.POLICY, {"format": native.FORMAT, "binding": authorized.binding, "recipients": authorized.grant["recipients"]})
    assert discover(authorized)["scope"] == "work"


def test_cli_room_contract_never_accepts_private_root_argument(authorized, capsys):
    flags = ["--dimension", "synthetic", "--room", "demo", "--workspace", str(authorized.workspace)]
    assert native_agents_cli.main(["room-status", *flags, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    reference = result["profiles"][0]["profile_ref"]
    assert native_agents_cli.main(["discover", *flags, "--profile-ref", reference, "--tool", "codex", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["sessions"][0]["provider_session_id"] == SESSION
    with pytest.raises(SystemExit):
        native_agents_cli.build_parser().parse_args(["room-status", *flags, "--profile-root", str(authorized.root)])


def test_profile_recipient_mismatch_is_refused(authorized):
    write(authorized.root / native.POLICY, {"format": native.FORMAT, "binding": authorized.binding, "recipients": [authorized.grant["recipients"][0], "age1" + "a" * 58]})
    with pytest.raises(native.NativeAgentError, match="recipient-mismatch"):
        discover(authorized)


@pytest.mark.parametrize("scope", ["personal", "work"])
def test_discovery_uses_real_host_admission_with_synthetic_keyset(authorized, monkeypatch, scope):
    from josh_room.encryption_domain import physical_bucket_identity

    if not shutil.which("git"):
        pytest.skip("actual Git observation unavailable")
    subprocess.run(["git", "init", str(authorized.workspace)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(authorized.workspace), "remote", "add", "origin", "https://example.invalid/synthetic/demo.git"], capture_output=True, check=True)
    authorized.grant.update(profile=scope, scope=scope)
    authorized.binding["profile"] = scope
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    write(authorized.root / native.POLICY, {"format": native.FORMAT, "binding": authorized.binding, "recipients": authorized.grant["recipients"]})
    endpoint = "https://minio.example.invalid"
    dimension = {"display_name": "Synthetic", "provider": "minio", "endpoint": endpoint, "bucket": "synthetic", "credential_profile": "synthetic", "encryption_domain_id": authorized.grant["encryption_domain_id"]}
    write(authorized.directory / "config.json", {"dimensions": {"synthetic": dimension}})
    keyset = SimpleNamespace(binding=physical_bucket_identity("minio", endpoint, "synthetic"), encryption_domain_id=authorized.grant["encryption_domain_id"], key_generation=1, operational_recipient=authorized.grant["recipients"][0], recovery_recipients=(authorized.grant["recipients"][1],))
    monkeypatch.setattr(admission, "_current_keyset", lambda selected: keyset)
    monkeypatch.setattr(native, "_admit", _REAL_ADMIT)
    assert discover(authorized)["scope"] == scope
    authorized.grant["tools"] = ["claude"]
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    with pytest.raises(native.NativeAgentError, match="tool-not-authorized"):
        discover(authorized)
