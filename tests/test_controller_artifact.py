import json
import os
import sys
from pathlib import Path

import pytest

import scripts.build_controller_artifact as builder_module
from scripts.build_controller_artifact import (
    _controller_input_digest,
    _execution_exit_code,
    _json_result,
    _reports_expected_rcc_version,
    _run,
    build_commands,
    load_manifest,
    prepare_fresh_robot,
    require_clean_source,
    resolve_rcc_pin,
    validate_receipt,
)
from scripts.pin_controller_artifact import pin_manifest


def manifest():
    return {
        "schema_version": 1,
        "rcc": {
            "version": "v18.19.5",
            "platforms": {
                "linux-x64": {
                    "asset": "rcc-linux64",
                    "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.5/rcc-linux64",
                    "sha256": None,
                },
            },
        },
        "controller": {
            "robot": "vscode-extension/runtime/controller/robot.yaml",
            "artifact_asset": "josh-room-controller-linux-amd64.rcca",
        },
    }


def test_rcc_pin_requires_real_v18_19_5_checksum_before_build():
    with pytest.raises(ValueError, match="checksum is pending"):
        resolve_rcc_pin(manifest(), "linux-x64")

    pin = resolve_rcc_pin(manifest(), "linux-x64", "a" * 64)
    assert pin["version"] == "v18.19.5"
    assert pin["sha256"] == "a" * 64


def test_rcc_version_output_must_identify_only_the_pinned_release():
    assert _reports_expected_rcc_version("RCC version: v18.19.6 (windows/amd64)", "v18.19.6")
    assert _reports_expected_rcc_version("18.19.6", "v18.19.6")
    assert not _reports_expected_rcc_version("RCC version: v18.19.60", "v18.19.6")
    assert not _reports_expected_rcc_version("v18.19.6 then v18.19.7", "v18.19.6")


def test_source_provenance_allows_only_windows_line_ending_conversion(tmp_path):
    import subprocess

    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    tracked = repository / "controller.txt"
    tracked.write_bytes(b"first\nsecond\n")
    subprocess.run(["git", "-C", str(repository), "add", "controller.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)

    tracked.write_bytes(b"first\r\nsecond\r\n")
    require_clean_source(repository)
    tracked.write_bytes(b"changed\r\nsecond\r\n")
    with pytest.raises(RuntimeError, match="source differs from commit"):
        require_clean_source(repository)
    tracked.write_bytes(b"first\r\nsecond\r\n")
    (repository / "untracked.txt").write_text("extra\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="untracked files"):
        require_clean_source(repository)


def test_controller_input_digest_tracks_packaged_runtime_resources(tmp_path):
    controller = tmp_path / "controller"
    package = controller / "josh_room"
    package.mkdir(parents=True)
    robot = controller / "robot.yaml"
    robot.write_text(
        "environmentConfigs:\n  - environment_linux_amd64_freeze.yaml\n  - conda.yaml\n",
        encoding="utf-8",
    )
    (controller / "conda.yaml").write_text("dependencies: [python]\n", encoding="utf-8")
    (controller / "environment_linux_amd64_freeze.yaml").write_text("linux-lock\n", encoding="utf-8")
    windows_freeze = controller / "environment_windows_amd64_freeze.yaml"
    windows_freeze.write_text("windows-lock\n", encoding="utf-8")
    resource = package / "workspace_capture_defaults.json"
    resource.write_text('{"capture": true}\n', encoding="utf-8")

    initial = _controller_input_digest(robot)
    resource.write_text('{"capture": false}\n', encoding="utf-8")
    assert _controller_input_digest(robot) != initial
    resource.write_text('{"capture": true}\n', encoding="utf-8")
    initial = _controller_input_digest(robot)
    windows_freeze.write_text("other platform changed\n", encoding="utf-8")
    assert _controller_input_digest(robot) == initial


def test_checked_in_controller_manifest_and_consumers_are_exact_and_in_parity():
    root = Path(__file__).parents[1]
    value = load_manifest(root / "vscode-extension/runtime/controller-artifact-manifest.json")
    template = root / "templates/room/vscode-extension/runtime/controller-artifact-manifest.json"
    assert (root / "vscode-extension/runtime/controller-artifact-manifest.json").read_bytes() == template.read_bytes()
    version = value["rcc"]["version"]
    assert version.startswith("v18.19.") and version.removeprefix("v18.19.").isdigit()
    assert set(value["rcc"]["platforms"]) == {"linux-x64", "win32-x64"}
    expected_rcc_assets = {"linux-x64": "rcc-linux64", "win32-x64": "rcc-windows64.exe"}
    for platform, asset in expected_rcc_assets.items():
        pin = value["rcc"]["platforms"][platform]
        assert pin["asset"] == asset
        assert pin["url"] == f"https://github.com/joshyorko/rcc/releases/download/{version}/{asset}"
        assert len(pin["sha256"]) == 64 and all(char in "0123456789abcdef" for char in pin["sha256"])
        assert type(pin["size"]) is int and pin["size"] > 0
    runtime_value = json.loads((root / "vscode-extension/runtime/manifest.json").read_text())
    runtime_template = root / "templates/room/vscode-extension/runtime/manifest.json"
    assert runtime_value == json.loads(runtime_template.read_text())
    assert runtime_value["rcc"]["version"] == version
    assert runtime_value["rcc"]["platforms"] == value["rcc"]["platforms"]
    controller = runtime_value["controller"]["environment_artifacts"]
    assert set(controller) == {"linux-x64", "win32-x64"}
    release_lock = json.loads((root / "release-lock.json").read_text())
    assert release_lock["rcc"]["version"] == version
    for platform, asset in (("linux-x64", "josh-room-controller-linux-amd64.rcca"), ("win32-x64", "josh-room-controller-windows-amd64.rcca")):
        artifact = controller[platform]
        assert artifact["platform"] == platform
        assert artifact["digest"].startswith("sha256:") and len(artifact["digest"]) == 71
        assert artifact["specification_digest"].startswith("sha256:") and len(artifact["specification_digest"]) == 71
        assert artifact["archive"]["asset"] == asset
        assert artifact["archive"]["url"].startswith("https://github.com/joshyorko/josh-room/releases/download/")
        assert artifact["archive"]["url"].endswith("/" + asset)
        assert len(artifact["archive"]["sha256"]) == 64
        assert artifact["archive"]["size"] > 0
        locked = release_lock["controller"]["platforms"][platform]
        assert locked["artifact_digest"] == artifact["digest"]
        assert locked["specification_digest"] == artifact["specification_digest"]
        assert locked["archive_sha256"] == artifact["archive"]["sha256"]
        if "input_digest" in artifact:
            assert artifact["input_digest"] == locked["input_digest"]
    workflow = (root / ".github/workflows/controller-artifact.yml").read_text()
    assert "workflow_dispatch" in workflow
    assert "v18.19.2" not in workflow
    assert "required: true" in workflow


def test_controller_build_commands_use_canonical_artifact_flow():
    commands = build_commands(
        rcc="/managed/rcc",
        robot="/workspace/vscode-extension/runtime/controller/robot.yaml",
        archive="/dist/josh-room-controller-linux-amd64.rcca",
        artifact="sha256:" + "b" * 64,
        receipt="/dist/controller-receipt.json",
    )
    assert commands == [
        ["env", "publish", "--robot", "/workspace/vscode-extension/runtime/controller/robot.yaml", "--provider", "local", "--json"],
        ["env", "export", "--artifact", "sha256:" + "b" * 64, "--provider", "local", "--output", "/dist/josh-room-controller-linux-amd64.rcca"],
        ["env", "acquire", "--archive", "/dist/josh-room-controller-linux-amd64.rcca", "--permissive-local", "--json"],
        ["--no-build", "ht", "vars", "--robot", "/workspace/vscode-extension/runtime/controller/robot.yaml", "--json"],
        ["--no-build", "env", "exec", "--artifact", "sha256:" + "b" * 64, "--permissive-local", "--inherit-streams", "--receipt-file", "/dist/controller-receipt.json", "--", "python", "-m", "josh_room", "dimensions", "list", "--json"],
    ]


def test_fresh_robot_discards_old_freeze_and_generates_a_native_candidate(tmp_path):
    source = tmp_path / "source" / "controller"
    source.mkdir(parents=True)
    (source / "robot.yaml").write_text(
        'tasks:\n  "Josh Room":\n    shell: python -m josh_room\n\n'
        'environmentConfigs:\n  - environment_linux_amd64_freeze.yaml\n  - conda.yaml\n',
        encoding="utf-8",
    )
    (source / "conda.yaml").write_text("channels: [conda-forge]\ndependencies: [python=3.13]\n", encoding="utf-8")
    (source / "environment_linux_amd64_freeze.yaml").write_text("old freeze\n", encoding="utf-8")
    robot = prepare_fresh_robot(source / "robot.yaml", tmp_path / "build", "linux-x64")

    assert robot.read_text(encoding="utf-8") == (
        'tasks:\n  "Josh Room":\n    shell: python -m josh_room --help\n\n'
        "environmentConfigs:\n  - conda.yaml\n"
    )
    assert (robot.parent / "environment_linux_amd64_freeze.yaml").read_text() == "old freeze\n"


def test_rcc_json_result_accepts_ht_vars_array():
    assert _json_result('[{"key": "value"}]\n') == [{"key": "value"}]


@pytest.mark.parametrize("receipt", [{}, [], {"exitCode": False}])
def test_controller_execution_receipts_require_explicit_integer_exit_code(receipt):
    with pytest.raises((ValueError, TypeError), match="receipt"):
        _execution_exit_code(receipt, "controller")

def test_rcc_exec_receives_vsix_controller_source_path(tmp_path):
    controller = tmp_path / "controller"
    result = _run(
        Path(sys.executable),
        [
            "-c",
            "import json, os; print(json.dumps({'pythonpath': os.environ.get('PYTHONPATH')}))",
            "--json",
        ],
        home=tmp_path / "rcc-home",
        cwd=tmp_path,
        environment_overrides={"PYTHONPATH": str(controller)},
    )
    assert result == {"pythonpath": str(controller)}


def test_controller_crypto_proof_runs_inside_acquired_artifact():
    artifact = "sha256:" + "a" * 64
    command = builder_module.controller_crypto_command(
        artifact=artifact,
        receipt="/dist/crypto-receipt.json",
        script="/repo/scripts/controller_crypto_smoke.py",
    )
    assert command == [
        "--no-build",
        "env",
        "exec",
        "--artifact",
        artifact,
        "--permissive-local",
        "--inherit-streams",
        "--receipt-file",
        "/dist/crypto-receipt.json",
        "--",
        "python",
        "/repo/scripts/controller_crypto_smoke.py",
    ]


def test_controller_artifact_stage_is_adjacent_to_destination(tmp_path):
    destination = tmp_path / "dist" / "controller.rcca"
    destination.parent.mkdir()
    stage = builder_module.destination_stage(destination)
    try:
        assert stage.parent == destination.parent
        assert stage != destination
    finally:
        stage.unlink(missing_ok=True)


def test_receipt_is_immutable_and_carries_controller_provenance(tmp_path):
    receipt = {
        "format_version": 1,
        "artifact_digest": "sha256:" + "b" * 64,
        "specification_digest": "sha256:" + "c" * 64,
        "legacy_blueprint_key": "blueprint-1",
        "archive": {"sha256": "d" * 64, "size": 123},
        "rcc_version": "v18.19.5",
        "rcc_sha256": "f" * 64,
        "input_digest": "sha256:" + "1" * 64,
        "freeze": {"asset": "environment_linux_amd64_freeze.yaml", "sha256": "2" * 64, "size": 50},
        "source": "e" * 40,
        "platform": "linux-x64",
        "verified_acquire": True,
        "verified_no_build": True,
        "verified_exec": True,
        "verified_crypto": True,
    }
    validate_receipt(receipt, expected_platform="linux-x64", expected_rcc="v18.19.5")
    broken = {**receipt, "rcc_version": "v18.19.3"}
    with pytest.raises(ValueError, match="RCC version"):
        validate_receipt(broken, expected_platform="linux-x64", expected_rcc="v18.19.5")
    missing_crypto = dict(receipt)
    missing_crypto.pop("verified_crypto")
    with pytest.raises(ValueError, match="provenance fields"):
        validate_receipt(missing_crypto, expected_platform="linux-x64", expected_rcc="v18.19.5")


def test_manifest_pin_integration_updates_root_and_template_atomically(tmp_path):
    root = tmp_path / "runtime-manifest.json"
    template = tmp_path / "template-runtime-manifest.json"
    for target in (root, template):
        target.write_text(json.dumps({"schema_version": 1, "extension_version": "0.1.6", "controller": {"robot": "runtime/controller/robot.yaml"}}))
    artifact = tmp_path / "josh-room-controller-linux-amd64.rcca"
    artifact.write_bytes(b"controller-artifact")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "format_version": 1,
        "artifact_digest": "sha256:" + "a" * 64,
        "archive": {"sha256": "f" * 64, "size": artifact.stat().st_size},
        "rcc_version": "v18.19.5",
        "platform": "linux-x64",
        "source": "b" * 40,
    }))
    with pytest.raises(ValueError, match="archive SHA256"):
        pin_manifest(root, template, artifact, receipt, "v0.1.7-controller-artifact", "b" * 40)



def test_manifest_pin_integration_keeps_platform_artifacts_separate(tmp_path):
    root = tmp_path / "runtime-manifest.json"
    template = tmp_path / "template-runtime-manifest.json"
    initial = {"schema_version": 1, "controller": {"environment_artifact": {"digest": "linux"}}}
    for target in (root, template):
        target.write_text(json.dumps(initial))
    artifact = tmp_path / "josh-room-controller-windows-amd64.rcca"
    artifact.write_bytes(b"windows-controller-artifact")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "format_version": 1,
        "artifact_digest": "sha256:" + "a" * 64,
        "specification_digest": "sha256:" + "c" * 64,
        "archive": {"sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(), "size": artifact.stat().st_size},
        "rcc_version": "v18.19.5",
        "platform": "win32-x64",
        "source": "b4da2846fee429a2e2878f817faac6a48e4384fd",
    }))

    pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts", "b4da2846fee429a2e2878f817faac6a48e4384fd", platform="win32-x64")
    for target in (root, template):
        value = json.loads(target.read_text())
        assert value["controller"]["environment_artifact"]["digest"] == "linux"
        assert value["controller"]["environment_artifacts"]["win32-x64"]["platform"] == "win32-x64"


def test_manifest_pin_rejects_malformed_specification_digest(tmp_path):
    root = tmp_path / "runtime-manifest.json"
    template = tmp_path / "template-runtime-manifest.json"
    for target in (root, template):
        target.write_text(json.dumps({"schema_version": 1, "controller": {}}))
    artifact = tmp_path / "controller.rcca"
    artifact.write_bytes(b"controller")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "artifact_digest": "sha256:" + "a" * 64,
        "specification_digest": "sha256:not-a-digest",
        "archive": {"sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(), "size": artifact.stat().st_size},
        "rcc_version": "v18.19.5",
        "platform": "linux-x64",
        "source": "b4da2846fee429a2e2878f817faac6a48e4384fd",
    }))
    with pytest.raises(ValueError, match="specification digest"):
        pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts", "b4da2846fee429a2e2878f817faac6a48e4384fd")

def test_manifest_pin_rejects_malformed_artifact_digest(tmp_path):
    root = tmp_path / "runtime-manifest.json"
    template = tmp_path / "template-runtime-manifest.json"
    for target in (root, template):
        target.write_text(json.dumps({"schema_version": 1, "controller": {}}))
    artifact = tmp_path / "controller.rcca"
    artifact.write_bytes(b"controller")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "artifact_digest": "not-a-digest",
        "specification_digest": "sha256:" + "b" * 64,
        "archive": {"sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(), "size": artifact.stat().st_size},
        "rcc_version": "v18.19.5",
        "platform": "linux-x64",
        "source": "b4da2846fee429a2e2878f817faac6a48e4384fd",
    }))
    with pytest.raises(ValueError, match="artifact digest"):
        pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts", "b4da2846fee429a2e2878f817faac6a48e4384fd")


def test_manifest_pin_rejects_unexpected_controller_source(tmp_path):
    root = tmp_path / "runtime-manifest.json"
    template = tmp_path / "template-runtime-manifest.json"
    for target in (root, template):
        target.write_text(json.dumps({"schema_version": 1, "controller": {}}))
    artifact = tmp_path / "controller.rcca"
    artifact.write_bytes(b"controller")
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "artifact_digest": "sha256:" + "a" * 64,
        "specification_digest": "sha256:" + "b" * 64,
        "archive": {"sha256": __import__("hashlib").sha256(artifact.read_bytes()).hexdigest(), "size": artifact.stat().st_size},
        "rcc_version": "v18.19.5",
        "platform": "linux-x64",
        "source": "c" * 40,
    }))
    with pytest.raises(ValueError, match="source commit"):
        pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts", "d" * 40)
def test_packaged_controller_python_file_sets_are_byte_identical_and_importable(tmp_path):
    root = Path(__file__).parents[1]
    canonical = {path.name: path.read_bytes() for path in (root / "src/josh_room").glob("*.py")}
    for packaged_root in (
        root / "vscode-extension/runtime/controller/josh_room",
        root / "templates/room/vscode-extension/runtime/controller/josh_room",
    ):
        packaged = {path.name: path.read_bytes() for path in packaged_root.glob("*.py")}
        assert set(packaged) == set(canonical)
        assert packaged == canonical
        result = __import__("subprocess").run(
            [sys.executable, "-c", "import josh_room.encryption_domain, josh_room.cli, josh_room.jat; print(josh_room.encryption_domain.__file__)"],
            env={**os.environ, "PYTHONPATH": str(packaged_root.parent), "JOSH_ROOM_EXTENSION_MODE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert str(packaged_root) in result.stdout
