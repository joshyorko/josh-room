import json
import os
import sys
from pathlib import Path

import pytest

from josh_room.jat import JATError, _extract_operation_result, _jat_contract, _run


def _managed_env(monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", "/private/runtime/rcc")
    monkeypatch.setenv("JOSH_ROOM_RCC_HOME", "/private/runtime/robocorp")
    monkeypatch.setenv("JOSH_ROOM_JAT_ARTIFACT", "sha256:" + "a" * 64)
    monkeypatch.setenv("JOSH_ROOM_JAT_SHA", "b" * 40)


def _cli_stdout(operation, **extra):
    payload = {
        "format_version": 2,
        "operation": operation,
        "success": True,
        "exit_status": 0,
        "producer_version": "jat-test",
    }
    payload.update(extra)
    return json.dumps(payload, indent=2)


def _fake_run_cli(seen, stdout_text, exit_status=0):
    def fake_run_cli(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, kwargs=kwargs)
        if "--receipt-file" in argv:
            receipt = Path(argv[argv.index("--receipt-file") + 1])
            receipt.write_text(json.dumps({"artifactDigest": os.environ["JOSH_ROOM_JAT_ARTIFACT"], "exitCode": exit_status}))
        return exit_status, stdout_text, "decorative rcc diagnostic"

    return fake_run_cli


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX-specific")
def test_jat_timeout_returns_bounded_metadata_and_terminates_process_group():
    with pytest.raises(JATError) as error:
        _run([sys.executable, "-c", "import time; time.sleep(60)"], 0.01)
    assert error.value.result["timed_out"] is True
    assert error.value.result["exit_status"] is None
    assert len(error.value.result["argv"]) == 3


def test_jat_contract_uses_rcc_tasks_and_python_surface(tmp_path):
    (tmp_path / "robot.yaml").write_text("tasks:\n  Build:\n  Restore:\n  Serve:\n  JAT:\n    shell: python -m jat.cli\n")
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

    def fake_run(argv, _timeout, **_kwargs):
        seen["argv"] = argv
        request = json.loads(Path(argv[-1]).read_text())
        assert request == {
            "all_images": False,
            "folder": str(tmp_path / "source"),
            "output": str(tmp_path / "haul"),
            "images": [],
        }
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
    monkeypatch.setattr("josh_room.jat._run", lambda *_args, **_kwargs: (0, ""))
    with pytest.raises(JATError, match="fresh"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")


def test_jat_missing_receipt_reports_bounded_rcc_diagnostic(tmp_path, monkeypatch):
    (tmp_path / "output").mkdir()
    monkeypatch.setattr("josh_room.jat._run", lambda *_args, **_kwargs: (1, "typed request validation failed"))
    with pytest.raises(JATError, match="typed request validation failed"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(
            tmp_path, tmp_path / "source", tmp_path / "haul"
        )


def test_jat_rejects_receipt_operation_mismatch(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    def write_mismatch(*_args, **_kwargs):
        result_path.write_text('{"operation":"restore","success":true,"exit_status":0}')
        return 0, ""
    monkeypatch.setattr("josh_room.jat._run", write_mismatch)
    with pytest.raises(JATError, match="operation mismatch"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")


def test_jat_rejects_inconsistent_receipt_exit_status(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    def write_inconsistent(*_args, **_kwargs):
        result_path.write_text('{"operation":"build","success":true,"exit_status":1}')
        return 0, ""
    monkeypatch.setattr("josh_room.jat._run", write_inconsistent)
    with pytest.raises(JATError, match="exit status"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(tmp_path, tmp_path / "source", tmp_path / "haul")


def test_local_fallback_jat_pins_cwd_and_result_directory(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    seen = {}
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")

    def fake_run(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, kwargs=kwargs)
        result_path.write_text('{"operation":"restore","success":true,"exit_status":0}')
        return 0, "local RCC output"

    monkeypatch.setattr("josh_room.jat._run", fake_run)
    __import__("josh_room.jat", fromlist=["run_restore"]).run_restore(
        tmp_path, tmp_path / "haul", tmp_path / "destination"
    )

    assert seen["kwargs"]["cwd"] == tmp_path
    assert seen["kwargs"]["env"]["ROBOT_ARTIFACTS"] == str(tmp_path / "output")
    assert seen["kwargs"]["env"]["JAT_RUN_DIR"] == str(tmp_path / "output")


def test_extension_jat_uses_the_pinned_artifact_with_managed_rcc(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    result_path.write_text('{"operation":"build","success":true,"exit_status":0}')
    seen = {}

    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", "/private/runtime/rcc")
    monkeypatch.setenv("JOSH_ROOM_RCC_HOME", "/private/runtime/robocorp")
    monkeypatch.setenv("JOSH_ROOM_JAT_ARTIFACT", "sha256:" + "a" * 64)
    monkeypatch.setenv("JOSH_ROOM_JAT_SHA", "b" * 40)

    def fake_run(argv, timeout, **kwargs):
        seen.update(argv=argv, timeout=timeout, kwargs=kwargs)
        result_path.write_text('{"operation":"build","success":true,"exit_status":0}')
        receipt = Path(argv[argv.index("--receipt-file") + 1])
        receipt.write_text(json.dumps({"artifactDigest": os.environ["JOSH_ROOM_JAT_ARTIFACT"], "exitCode": 0}))
        return 0, "managed RCC output"

    monkeypatch.setattr("josh_room.jat._run", fake_run)
    result = __import__("josh_room.jat", fromlist=["run_build"]).run_build(
        tmp_path, tmp_path / "source", tmp_path / "haul"
    )

    assert seen["argv"][:6] == [
        "/private/runtime/rcc",
        "--no-build",
        "env",
        "exec",
        "--artifact",
        "sha256:" + "a" * 64,
    ]
    assert "--permissive-local" in seen["argv"]
    assert "--inherit-streams" in seen["argv"]
    assert "--receipt-file" in seen["argv"]
    assert "--json" in seen["argv"]
    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command[:6] == [
        "python", "-m", "jat.task_runner", "run", str(tmp_path / "tasks.py"), "-t",
    ]
    assert command[6] == "Build"
    assert command[7:9] == ["--", "--json-input"]
    assert seen["kwargs"]["env"]["ROBOCORP_HOME"] == "/private/runtime/robocorp"
    assert seen["kwargs"]["env"]["RCC_HOLOTREE_MODE"] == "private"
    assert seen["kwargs"]["env"]["JAT_RUN_DIR"] == str(tmp_path / "output")
    assert seen["kwargs"]["cwd"] == tmp_path
    assert result["version"] == "b" * 40


def test_extension_jat_rejects_missing_managed_runtime_instead_of_using_path_rcc(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.delenv("JOSH_ROOM_RCC_EXE", raising=False)
    monkeypatch.delenv("JOSH_ROOM_RCC_HOME", raising=False)
    monkeypatch.delenv("JOSH_ROOM_JAT_ARTIFACT", raising=False)

    with pytest.raises(JATError, match="managed Josh Room runtime is incomplete"):
        __import__("josh_room.jat", fromlist=["run_build"]).run_build(
            tmp_path, tmp_path / "source", tmp_path / "haul"
        )


def test_jat_cli_inspect_managed_mode_invokes_pinned_artifact_and_validates_receipt(tmp_path, monkeypatch):
    _managed_env(monkeypatch)
    haul = tmp_path / "hauls" / "workspace.haul"
    stdout = _cli_stdout(
        "inspect",
        inventory=[{"reference": "hauler/rcc-environment.rcca:latest", "type": "file", "size": 12}],
        anchors={"workspace": True, "rcc_environment": True},
    )
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, stdout))
    result = __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, haul)

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
    assert seen["kwargs"]["env"]["ROBOCORP_HOME"] == "/private/runtime/robocorp"
    assert seen["kwargs"]["env"]["RCC_HOLOTREE_MODE"] == "private"
    assert seen["kwargs"]["env"]["JAT_RUN_DIR"] == str(tmp_path / "output")
    assert result["operation"] == "inspect"
    assert result["success"] is True
    assert result["exit_status"] == 0
    assert result["inventory"] == [{"reference": "hauler/rcc-environment.rcca:latest", "type": "file", "size": 12}]
    assert result["version"] == "b" * 40


def test_jat_cli_inspect_fallback_mode_runs_jat_robot_task(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    monkeypatch.delenv("JOSH_ROOM_RCC_EXE", raising=False)
    monkeypatch.delenv("JOSH_ROOM_RCC_HOME", raising=False)
    monkeypatch.delenv("JOSH_ROOM_JAT_ARTIFACT", raising=False)
    haul = tmp_path / "workspace.haul"
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, _cli_stdout("inspect")))
    __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, haul)

    assert seen["argv"] == [
        "rcc",
        "run",
        "-r",
        str(tmp_path / "robot.yaml"),
        "-t",
        "JAT",
        "--",
        "inspect",
        "--haul",
        str(haul),
        "--json",
    ]
    assert seen["kwargs"]["cwd"] == tmp_path
    assert seen["kwargs"]["env"]["JAT_RUN_DIR"] == str(tmp_path / "output")


def test_extract_operation_result_pulls_result_from_noisy_rcc_stdout():
    payload = {
        "format_version": 2,
        "operation": "inspect",
        "success": True,
        "exit_status": 0,
        "note": "braces } and { inside strings",
    }
    noisy = (
        'rcc: resolving holotree {"config": {"nested": true, "hint": "literal } brace"}}\n'
        + json.dumps(payload, indent=2)
        + '\nrcc: done {"summary": "another } fake"}'
    )
    assert _extract_operation_result(noisy) == payload


def test_extract_operation_result_returns_none_without_operation_shape():
    assert _extract_operation_result("plain rcc chatter without any json") is None
    assert _extract_operation_result('{"format_version": 2, "producer_version": "jat-test"}') is None
    assert _extract_operation_result('{"operation": 7, "success": true, "exit_status": 0}') is None


def test_extract_operation_result_prefers_the_last_match():
    first = json.dumps({"operation": "inspect", "success": True, "exit_status": 0})
    second = json.dumps({"operation": "inspect", "success": True, "exit_status": 0, "producer_version": "final"})
    assert _extract_operation_result(first + "\nrcc noise\n" + second) == json.loads(second)


def test_extract_operation_result_ignores_nested_operation_objects():
    nested = json.dumps({"wrapper": {"operation": "inspect", "success": True, "exit_status": 0}})
    assert _extract_operation_result(nested) is None


def test_jat_cli_missing_machine_readable_result_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    monkeypatch.setattr("josh_room.jat._run_cli", lambda *_args, **_kwargs: (0, "rcc banner without json", ""))
    with pytest.raises(JATError, match="machine-readable"):
        __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, tmp_path / "workspace.haul")


def test_jat_cli_rejects_receipt_operation_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    stdout = _cli_stdout("restore")
    monkeypatch.setattr("josh_room.jat._run_cli", lambda *_args, **_kwargs: (0, stdout, ""))
    with pytest.raises(JATError, match="operation mismatch"):
        __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, tmp_path / "workspace.haul")


def test_jat_cli_rejects_inconsistent_receipt_exit_status(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    stdout = _cli_stdout("inspect", exit_status=0)
    monkeypatch.setattr("josh_room.jat._run_cli", lambda *_args, **_kwargs: (1, stdout, ""))
    with pytest.raises(JATError, match="inconsistent with RCC"):
        __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, tmp_path / "workspace.haul")


def test_jat_cli_failure_raises_with_exit_status(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    stdout = json.dumps({"operation": "inspect", "success": False, "exit_status": 1})
    monkeypatch.setattr("josh_room.jat._run_cli", lambda *_args, **_kwargs: (1, stdout, ""))
    with pytest.raises(JATError, match="failed with exit 1"):
        __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, tmp_path / "workspace.haul")


def test_jat_cli_serve_runs_foreground_without_timeout(tmp_path, monkeypatch):
    _managed_env(monkeypatch)
    haul = tmp_path / "workspace.haul"
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, _cli_stdout("serve")))
    __import__("josh_room.jat", fromlist=["run_serve"]).run_serve(tmp_path, haul, mode="both")

    assert seen["timeout"] is None
    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == ["python", "-m", "jat.cli", "serve", "--haul", str(haul), "--mode", "both", "--json"]


def test_jat_cli_extract_passes_reference_and_destination_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    haul = tmp_path / "workspace.haul"
    reference = "hauler/rcc-environment.rcca:latest"
    destination = tmp_path / "restore-target"
    seen = {}
    monkeypatch.setattr(
        "josh_room.jat._run_cli",
        _fake_run_cli(seen, _cli_stdout("extract", reference=reference)),
    )
    result = __import__("josh_room.jat", fromlist=["run_extract"]).run_extract(
        tmp_path, haul, reference, destination
    )

    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == [
        "extract",
        "--haul",
        str(haul),
        "--reference",
        reference,
        "--destination",
        str(destination),
        "--json",
    ]
    assert result["reference"] == reference
    assert result["success"] is True


def test_jat_cli_export_uses_containerd_format(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    haul = tmp_path / "workspace.haul"
    output = tmp_path / "export" / "workspace.tar"
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, _cli_stdout("export")))
    __import__("josh_room.jat", fromlist=["run_export"]).run_export(tmp_path, haul, output)

    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == [
        "export",
        "--haul",
        str(haul),
        "--format",
        "containerd",
        "--output",
        str(output),
        "--json",
    ]


def test_jat_cli_copy_passes_target_verbatim_without_credential_material(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    haul = tmp_path / "workspace.haul"
    target = "registry://registry.example.test/workspace:latest"
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, _cli_stdout("copy")))
    __import__("josh_room.jat", fromlist=["run_copy"]).run_copy(tmp_path, haul, target)

    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == ["copy", "--haul", str(haul), "--to", target, "--json"]
    assert "--retries" not in command
    assert "--plain-http" not in command
    assert "--insecure" not in command
    assert not any(value in " ".join(command).lower() for value in ("password", "token", "secret", "credential"))


def test_jat_cli_copy_optional_flags_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "0")
    haul = tmp_path / "workspace.haul"
    target = "registry://registry.example.test/workspace:latest"
    seen = {}
    monkeypatch.setattr("josh_room.jat._run_cli", _fake_run_cli(seen, _cli_stdout("copy")))
    __import__("josh_room.jat", fromlist=["run_copy"]).run_copy(
        tmp_path, haul, target, retries=1, plain_http=True, insecure=True
    )

    command = seen["argv"][seen["argv"].index("--") + 1:]
    assert command == [
        "copy",
        "--haul",
        str(haul),
        "--to",
        target,
        "--retries",
        "1",
        "--plain-http",
        "--insecure",
        "--json",
    ]


def test_jat_build_advanced_capture_keys_flow_into_the_request(tmp_path, monkeypatch):
    result_path = tmp_path / "output" / "result.json"
    result_path.parent.mkdir()
    captured = {}

    def fake_run(argv, _timeout, **_kwargs):
        captured["request"] = json.loads(Path(argv[-1]).read_text())
        result_path.write_text('{"operation":"build","success":true,"exit_status":0}')
        return 0, ""

    monkeypatch.setattr("josh_room.jat._run", fake_run)
    jat = __import__("josh_room.jat", fromlist=["run_build"])
    jat.run_build(
        tmp_path,
        tmp_path / "source",
        tmp_path / "haul",
        images_files=["images.txt"],
        hauler_manifests=["manifest.yml"],
        chunk_size="2GiB",
        exclude_extras=True,
        retries=3,
    )
    assert captured["request"]["images_files"] == ["images.txt"]
    assert captured["request"]["hauler_manifests"] == ["manifest.yml"]
    assert captured["request"]["chunk_size"] == "2GiB"
    assert captured["request"]["exclude_extras"] is True
    assert captured["request"]["retries"] == 3

    captured.clear()
    jat.run_build(tmp_path, tmp_path / "source", tmp_path / "haul")
    for key in ("images_files", "hauler_manifests", "chunk_size", "exclude_extras", "retries"):
        assert key not in captured["request"]


def test_jat_cli_inspect_fails_closed_when_managed_runtime_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.delenv("JOSH_ROOM_RCC_EXE", raising=False)
    monkeypatch.delenv("JOSH_ROOM_RCC_HOME", raising=False)
    monkeypatch.delenv("JOSH_ROOM_JAT_ARTIFACT", raising=False)
    with pytest.raises(JATError, match="managed Josh Room runtime is incomplete"):
        __import__("josh_room.jat", fromlist=["run_inspect"]).run_inspect(tmp_path, tmp_path / "workspace.haul")
