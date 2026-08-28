import importlib.util
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
