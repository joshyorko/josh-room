import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room.cli import main


@pytest.mark.first_run
def test_josh_device_ready_r2_picker_hydrate_ide_and_rcc(tmp_path, monkeypatch, capsys):
    if os.environ.get("JOSH_ROOM_FIRST_RUN_LIVE") != "1":
        pytest.skip("Josh's device-ready Room acceptance is private and secret-gated")
    required = ["JOSH_ROOM_CONFIG_DIR", "JOSH_ROOM_JAT_ROOT", "DBUS_SESSION_BUS_ADDRESS"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("first-run private config, JAT, or host keyring bus is unavailable")

    monkeypatch.setenv("JOSH_ROOM_INSTANCE", str(tmp_path / "state"))
    monkeypatch.setenv("JOSH_ROOM_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    assert main(["doctor", "--backend", "r2", "--ide", "vscode-insiders", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert all(check["ok"] for check in doctor["checks"])

    assert main(["projects", "list", "--json"]) == 0
    projects = json.loads(capsys.readouterr().out)["projects"]
    assert {project["display_name"] for project in projects} >= {"Hive"}
    selected = next(index for index, project in enumerate(projects, 1) if project["id"] == "hive")

    launched = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: str(selected))
    monkeypatch.setattr("josh_room.cli._launch_ide", lambda executable, destination: launched.append((executable, destination)))
    assert main(["enter"]) == 0
    capsys.readouterr()
    workspace = tmp_path / "workspaces" / "hive" / "workspace" / "hive"
    assert (workspace / "README.md").is_file()
    assert Path(launched[0][1]) == tmp_path / "workspaces" / "hive"
    assert launched[0][0].endswith("code-insiders")

    subprocess.run(["rcc", "version"], cwd=workspace, check=True, capture_output=True, text=True)
    subprocess.run(["rcc", "diagnostics", "--quick"], cwd=workspace, check=True, capture_output=True, text=True)
