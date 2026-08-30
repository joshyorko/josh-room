import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CONTROLLER_ROOT = ROOT / "vscode-extension/runtime/controller"


def packaged_jat():
    package_name = "packaged_josh_room_runtime"
    package = types.ModuleType(package_name)
    package.__path__ = [str(CONTROLLER_ROOT / "josh_room")]
    sys.modules[package_name] = package
    module_name = f"{package_name}.jat"
    spec = importlib.util.spec_from_file_location(module_name, CONTROLLER_ROOT / "josh_room/jat.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_packaged_windows_process_termination_does_not_use_posix_process_groups():
    jat = packaged_jat()

    class Process:
        def __init__(self):
            self.terminated = False
            self.communicated = False

        def terminate(self):
            self.terminated = True

        def communicate(self):
            self.communicated = True

    process = Process()
    jat._terminate_process(process, platform="nt")
    assert process.terminated is True
    assert process.communicated is True


def test_packaged_windows_run_does_not_request_posix_process_sessions(monkeypatch):
    jat = packaged_jat()
    captured = {}

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return "", ""

    def popen(_argv, **kwargs):
        captured["options"] = kwargs
        return Process()

    monkeypatch.setattr(jat.os, "name", "nt")
    monkeypatch.setattr(jat.subprocess, "Popen", popen)
    assert jat._run(["synthetic"], 2) == (0, "")
    assert "start_new_session" not in captured["options"]


def test_packaged_extension_runtime_handoff_fails_closed_when_mode_is_missing(monkeypatch, tmp_path):
    jat = packaged_jat()
    monkeypatch.delenv("JOSH_ROOM_EXTENSION_MODE", raising=False)
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", "/managed/rcc")
    monkeypatch.setenv("JOSH_ROOM_RCC_HOME", "/managed/home")
    monkeypatch.setenv("JOSH_ROOM_JAT_ARTIFACT", "sha256:" + "a" * 64)
    with pytest.raises(jat.JATError, match="managed Josh Room runtime is incomplete"):
        jat._managed_runtime(tmp_path)


def test_packaged_jat_exposes_the_new_cli_surface():
    jat = packaged_jat()
    for name in ("run_inspect", "run_extract", "run_export", "run_copy", "_extract_operation_result"):
        assert callable(getattr(jat, name))
    parsed = jat._extract_operation_result(
        'noise {"operation": "inspect", "success": true, "exit_status": 0}' + " trailing"
    )
    assert parsed == {"operation": "inspect", "success": True, "exit_status": 0}


def test_packaged_jat_inspect_managed_argv_matches_src_contract(tmp_path, monkeypatch):
    jat = packaged_jat()
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", "/private/runtime/rcc")
    monkeypatch.setenv("JOSH_ROOM_RCC_HOME", "/private/runtime/robocorp")
    monkeypatch.setenv("JOSH_ROOM_JAT_ARTIFACT", "sha256:" + "a" * 64)
    monkeypatch.setenv("JOSH_ROOM_JAT_SHA", "b" * 40)
    haul = tmp_path / "workspace.haul"
    stdout = json.dumps(
        {
            "format_version": 2,
            "operation": "inspect",
            "success": True,
            "exit_status": 0,
            "producer_version": "jat-test",
            "inventory": [{"reference": "hauler/rcc-environment.rcca:latest", "type": "file", "size": 12}],
        },
        indent=2,
    )
    seen = {}

    def fake_run_cli(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, kwargs=kwargs)
        receipt = Path(argv[argv.index("--receipt-file") + 1])
        receipt.write_text(json.dumps({"artifactDigest": os.environ["JOSH_ROOM_JAT_ARTIFACT"], "exitCode": 0}))
        return 0, stdout, "decorative rcc diagnostic"

    monkeypatch.setattr(jat, "_run_cli", fake_run_cli)
    result = jat.run_inspect(tmp_path, haul)

    assert seen["argv"][:6] == [
        "/private/runtime/rcc",
        "--no-build",
        "env",
        "exec",
        "--artifact",
        "sha256:" + "a" * 64,
    ]
    for flag in ("--permissive-local", "--inherit-streams", "--receipt-file", "--json"):
        assert flag in seen["argv"]
    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == ["python", "-m", "jat.cli", "inspect", "--haul", str(haul), "--json"]
    assert result["inventory"] == [{"reference": "hauler/rcc-environment.rcca:latest", "type": "file", "size": 12}]
    assert result["version"] == "b" * 40
