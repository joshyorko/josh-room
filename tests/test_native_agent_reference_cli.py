"""The explicit recovery CLI keeps exact history and native custody boundaries."""

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import (
    cli,
    native_agent_discovery,
    native_agent_reference_cli,
    native_agents,
    native_agents_cli,
)


@pytest.mark.parametrize("name", ["JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_PROVIDER_CREDENTIALS", "JOSH_ROOM_ENCRYPTION_MATERIAL"])
def test_plaintext_authority_handoff_refused_before_backend(monkeypatch, name):
    monkeypatch.setenv(name, "/synthetic/credential-handoff")
    monkeypatch.setattr(native_agent_reference_cli, "private_config", lambda: pytest.fail("must refuse before backend selection"))
    args = SimpleNamespace(snapshot="historical-point", dimension="demo", room="room")
    with pytest.raises(native_agents.NativeAgentError, match="native-keyring-required"), native_agent_reference_cli._context(args):
        pytest.fail("must not open context")


def test_latest_recovery_selection_refused_before_authority(monkeypatch):
    monkeypatch.setattr(native_agent_reference_cli, "private_config", lambda: pytest.fail("must refuse before backend selection"))
    args = SimpleNamespace(snapshot="latest", dimension="demo", room="room")
    with pytest.raises(native_agents.NativeAgentError, match="exact-history-required"), native_agent_reference_cli._context(args):
        pytest.fail("must not select another point")


def test_cli_preserves_known_rejected_sidecar_and_uploaded_orphan(monkeypatch, capsys):
    from josh_room.native_agent_reference import NativeReferenceError

    def failed(args):
        raise NativeReferenceError("native-reference-conflict", sidecar_published=False)

    monkeypatch.setattr(native_agent_reference_cli, "dispatch", failed)
    assert cli.main(["agents", "recovery-status", "--dimension", "demo", "--room", "room", "--snapshot", "point"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "commit-uncertain" and receipt["committed"] is True
    assert receipt["sidecar_published"] is False and receipt["orphaned"] is True
    assert receipt["native_resume_verified"] is False


def recovery_args(action="recovery-status", *flags):
    return native_agents_cli.build_parser().parse_args([
        action, "--dimension", "demo", "--room", "room", "--snapshot", "point", *flags,
    ])


def test_absent_recovery_status_has_versioned_empty_reference(monkeypatch):
    @contextmanager
    def opened(_args):
        yield object()

    def inspect(_context, *, profile, tool):
        assert profile is None and tool is None

    monkeypatch.setattr(native_agent_reference_cli, "_context", opened)
    monkeypatch.setattr(native_agent_reference_cli.references, "inspect_reference", inspect)
    result = native_agent_reference_cli.dispatch(recovery_args())
    assert result["ok"] is True and result["state"] == "reference-absent"
    assert result["contract_version"] == 2 and result["format"] is None
    assert result["entries"] == [] and result["native_resume_verified"] is False


@pytest.mark.parametrize("flags", [
    ["--workspace", "/synthetic/workspace"],
    ["--profile-ref", "opaque-profile"],
    ["--tool", "codex"],
    ["--workspace", "/synthetic/workspace", "--profile-ref", "opaque-profile"],
    ["--workspace", "/synthetic/workspace", "--tool", "codex"],
    ["--profile-ref", "opaque-profile", "--tool", "codex"],
])
def test_partial_recovery_status_filter_refused_before_opening_authority(monkeypatch, flags):
    monkeypatch.setattr(native_agent_reference_cli, "_context", lambda args: pytest.fail("partial filters must fail closed"))
    with pytest.raises(native_agents.NativeAgentError, match="native-profile-selector-incomplete"):
        native_agent_reference_cli.dispatch(recovery_args("recovery-status", *flags))


def test_filtered_recovery_status_resolves_opaque_profile_and_checks_current_grant(monkeypatch):
    context = object()
    workspace = Path("/synthetic/workspace")
    grant = {"profile": "personal", "workspace": str(workspace), "scope": "personal"}
    calls = []

    @contextmanager
    def opened(_args):
        yield context

    def resolve(**kwargs):
        assert kwargs == {"dimension": "demo", "room": "room", "workspace": workspace,
                          "profile_ref": "opaque-profile", "tool": "codex"}
        calls.append("resolve")
        return grant

    def admitted(selected, profile, tool, actual_workspace, operation):
        assert selected is context
        assert (profile, tool, actual_workspace, operation) == ("personal", "codex", str(workspace), "room-verify")
        calls.append("admit")
        return grant, {}, {}

    def inspect(selected, *, profile, tool):
        assert selected is context and (profile, tool) == ("personal", "codex")
        calls.append("inspect")
        return {"state": "history-reference", "contract_version": 2, "format": "josh-room.agent-references.v2",
                "entries": [{"capture_id": "a" * 64}], "history_only": True, "native_resume_verified": False}

    monkeypatch.setattr(native_agent_reference_cli, "_context", opened)
    monkeypatch.setattr(native_agent_discovery, "resolve_profile", resolve)
    monkeypatch.setattr(native_agent_reference_cli.references, "_grant", admitted)
    monkeypatch.setattr(native_agent_reference_cli.references, "inspect_reference", inspect)
    result = native_agent_reference_cli.dispatch(recovery_args(
        "recovery-status", "--workspace", str(workspace), "--profile-ref", "opaque-profile", "--tool", "codex"))
    assert result["entries"] == [{"capture_id": "a" * 64}]
    assert calls == ["resolve", "admit", "inspect"]


@pytest.mark.parametrize("action", ["recovery-verify", "recovery-restore"])
def test_recovery_cli_retains_explicit_historical_capture_id(action):
    flags = ["--workspace", "/synthetic/workspace", "--profile-ref", "opaque-profile", "--tool", "claude",
             "--capture-id", "a" * 64]
    if action == "recovery-restore":
        flags += ["--session-id", "11111111-1111-4111-8111-111111111111"]
    assert recovery_args(action, *flags).capture_id == "a" * 64
