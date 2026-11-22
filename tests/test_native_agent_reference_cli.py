"""The explicit recovery CLI keeps exact history and native custody boundaries."""

import json
from types import SimpleNamespace

import pytest

from josh_room import cli, native_agent_reference_cli, native_agents


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
