"""Exercise the real CLI boundary without auth or global runtime writes."""

import json
import sys
from pathlib import Path

import pytest

from josh_room import cli, native_agents, native_agents_cli


def binding(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ["--profile-root", str(tmp_path / "profile"), "--dimension", "synthetic", "--room", "demo-room", "--profile", "personal", "--workspace", str(workspace)]


def test_capabilities_bypasses_auth_and_runtime(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline capabilities must not initialize runtime or trust")

    monkeypatch.setattr(cli, "initialize_system_trust", forbidden)
    monkeypatch.setattr(cli, "_instance_root", forbidden)
    monkeypatch.setattr(cli, "_write_runtime_result", forbidden)
    assert cli.main(["agents", "capabilities", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["native_resume_verified"] is False
    assert result["providers"]["t3"]["capture"] == "blocked"
    assert result["providers"]["claude"]["capture"] == "blocked"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="native recovery is Linux-qualified only")
def test_configure_status_and_binding_refusal(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(native_agents, "_admit", lambda *args, **kwargs: {"admitted": True})
    flags = binding(tmp_path)
    assert cli.main(["agents", "configure", *flags, "--recipient", "age1" + "a" * 58, "--recipient", "age1" + "b" * 58]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "configured"
    assert cli.main(["agents", "plan", *flags]) == 0
    assert json.loads(capsys.readouterr().out)["native_resume_verified"] is False
    flags[flags.index("demo-room")] = "another-room"
    assert cli.main(["agents", "status", *flags]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "profile-binding-mismatch"
    assert str(tmp_path) not in json.dumps(result)


def test_unqualified_windows_fails_without_creating_profile(tmp_path, capsys, monkeypatch):
    flags = binding(tmp_path)
    monkeypatch.setattr(native_agents.sys, "platform", "win32")
    assert cli.main(["agents", "configure", *flags, "--recipient", "age1" + "a" * 58, "--recipient", "age1" + "b" * 58]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "platform-not-qualified"
    assert not (tmp_path / "profile").exists()


def test_untrusted_diagnostics_are_sanitized(monkeypatch, capsys):
    def fail():
        raise OSError("private session content and credentials")

    monkeypatch.setattr(native_agents, "capabilities", fail)
    assert native_agents_cli.main(["capabilities"]) == 1
    assert "private session content" not in capsys.readouterr().out


@pytest.mark.parametrize("action", ["capture", "verify", "restore"])
def test_recovery_requires_explicit_inputs(action):
    with pytest.raises(SystemExit):
        native_agents_cli.build_parser().parse_args([action])


def test_extension_status_is_native_registered_and_packaged():
    root = Path(__file__).resolve().parents[1]
    package = json.loads((root / "vscode-extension/package.json").read_text())
    command = "joshRoom.agentSessionsStatus"
    assert f"onCommand:{command}" in package["activationEvents"]
    assert any(item["command"] == command for item in package["contributes"]["menus"]["view/item/context"])
    for name in ("extension.js", "package.json"):
        assert (root / "vscode-extension" / name).read_bytes() == (root / "templates/room/vscode-extension" / name).read_bytes()


def test_missing_host_grant_refuses_configure_before_creating_profile(tmp_path, monkeypatch, capsys):
    flags = binding(tmp_path)
    config_home = tmp_path / "host-config"
    config_home.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert cli.main(["agents", "configure", *flags, "--recipient", "age1" + "a" * 58, "--recipient", "age1" + "b" * 58]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "blocked"
    assert "admission" in result["error"]
    assert not (tmp_path / "profile").exists()


def test_chooser_contract_fails_closed_for_every_provider(capsys):
    assert cli.main(["agents", "capabilities"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["contract_version"] == 1
    assert set(result["providers"]) == {"t3", "codex", "claude"}
    for provider in result["providers"].values():
        assert provider["selectable"] is False
        assert provider["restore"] == "blocked"
    assert result["providers"]["codex"]["restore_reason"] == "crash-safe-materialization-unavailable"
