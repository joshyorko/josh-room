import json
import os
import sys
from pathlib import Path

import pytest

from josh_room.jat import JATError, _jat_contract, _run


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-specific")
def test_jat_timeout_returns_bounded_metadata_and_terminates_process_group():
    with pytest.raises(JATError) as error:
        _run([sys.executable, "-c", "import time; time.sleep(60)"], 0.01)
    assert error.value.result["timed_out"] is True
    assert error.value.result["exit_status"] is None
    assert len(error.value.result["argv"]) == 3


def test_jat_contract_uses_rcc_tasks_and_python_surface(tmp_path):
    (tmp_path / "robot.yaml").write_text("tasks:\n  Build:\n  Restore:\n  Serve:\n  3tc:\n    shell: python -m jat.cli\n")
    (tmp_path / "tasks.py").write_text(
        "from robocorp.tasks import task\n"
        "@task\ndef Build(): pass\n"
        "@task\ndef Restore(): pass\n"
        "@task\ndef Serve(): pass\n"
    )
    assert _jat_contract(tmp_path) == {"robot": True, "tasks": True, "interactive": True}


def test_jat_contract_rejects_legacy_only_checkout(tmp_path):
    (tmp_path / "joshs-all-the-things.sh").write_text("#!/bin/sh\n")
    assert _jat_contract(tmp_path) == {"robot": False, "tasks": False, "interactive": False}


def test_jat_build_uses_typed_rcc_request_and_preserves_receipt(tmp_path, monkeypatch):
    output = tmp_path / "output" / "result.json"
    output.parent.mkdir()
    output.write_text('{"format_version": 1, "operation": "build", "success": true, "exit_status": 0, "producer_version": "jat-test", "payload_size": 12, "sha256": "' + "a" * 64 + '"}')
    seen = {}

    def fake_run(argv, _timeout):
        seen["argv"] = argv
        request = json.loads(Path(argv[-1]).read_text())
        assert request == {"folder": str(tmp_path / "source"), "output": str(tmp_path / "haul")}
        output.write_text('{"format_version": 1, "operation": "build", "success": true, "exit_status": 0, "producer_version": "jat-test", "payload_size": 12, "sha256": "' + "a" * 64 + '"}')
        return 0, "decorative output"

    monkeypatch.setattr("josh_room.jat._run", fake_run)
    result = __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")
    assert seen["argv"][:7] == ["rcc", "run", "-r", str(tmp_path / "robot.yaml"), "-t", "Build", "--"]
    assert seen["argv"][7] == "--json-input"
    assert result["producer_version"] == "jat-test"
    assert result["argv"][0] == "rcc"
    assert result["payload_sha256"] == result["sha256"]


def test_jat_rejects_stale_receipt(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    result_path.write_text('{"operation":"build","success":true,"exit_status":0}')
    monkeypatch.setattr("josh_room.jat._run", lambda *_args: (0, ""))
    with pytest.raises(JATError, match="fresh"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")


def test_jat_rejects_receipt_operation_mismatch(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    def write_mismatch(*_args):
        result_path.write_text('{"operation":"restore","success":true,"exit_status":0}')
        return 0, ""
    monkeypatch.setattr("josh_room.jat._run", write_mismatch)
    with pytest.raises(JATError, match="operation mismatch"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")


def test_jat_rejects_inconsistent_receipt_exit_status(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    def write_inconsistent(*_args):
        result_path.write_text('{"operation":"build","success":true,"exit_status":1}')
        return 0, ""
    monkeypatch.setattr("josh_room.jat._run", write_inconsistent)
    with pytest.raises(JATError, match="exit status"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")
