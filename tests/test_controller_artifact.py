import json
import os
import sys
from pathlib import Path

import pytest

import scripts.build_controller_artifact as builder_module
from scripts.build_controller_artifact import (
    _execution_exit_code,
    _json_result,
    _run,
    build_commands,
    load_manifest,
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


def test_checked_in_controller_manifest_is_v18_19_5_only_and_has_no_fake_checksums():
    root = Path(__file__).parents[1]
    value = load_manifest(root / "vscode-extension/runtime/controller-artifact-manifest.json")
    template = root / "templates/room/vscode-extension/runtime/controller-artifact-manifest.json"
    assert (root / "vscode-extension/runtime/controller-artifact-manifest.json").read_bytes() == template.read_bytes()
    assert value["rcc"]["version"] == "v18.19.5"
    assert value["rcc"]["platforms"]["linux-x64"] == {
        "asset": "rcc-linux64",
        "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.5/rcc-linux64",
        "sha256": "1a617ad7c736fa67c605e20e5ebe3c7d54b02cd548f733e05c809cf49a48db1e",
        "size": 22360226
    }
    assert value["rcc"]["platforms"]["linux-x64"]["size"] == 22360226
    assert value["rcc"]["platforms"]["win32-x64"] == {
        "asset": "rcc-windows64.exe",
        "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.5/rcc-windows64.exe",
        "sha256": "7b62dc1f421f7cf33560c1b0567fc5bf8a65fc1d919af28d0ab633f85814a731",
        "size": 20084224,
    }
    runtime_value = json.loads((root / "vscode-extension/runtime/manifest.json").read_text())
    runtime_template = root / "templates/room/vscode-extension/runtime/manifest.json"
    assert runtime_value == json.loads(runtime_template.read_text())
    controller = runtime_value["controller"]["environment_artifacts"]
    assert controller["linux-x64"] == {
        "digest": "sha256:136ac1121dc14b63333276c571e3712f3bc6c5eb16f497764200ea5bcfa8511a",
        "specification_digest": "sha256:8f632d0b238da15b20536c78b3440aa2771a504785b8b478383064bbd47a0f23",
        "platform": "linux-x64",
        "archive": {
            "asset": "josh-room-controller-linux-amd64.rcca",
            "url": "https://github.com/joshyorko/josh-room/releases/download/v0.1.11-controller-artifacts/josh-room-controller-linux-amd64.rcca",
            "sha256": "3dd7d542b2fdd35633152a400e30d7c6bb861c5bb128f6e22ef6bdbc3b048f88",
            "size": 130993088,
        },
    }
    assert controller["win32-x64"] == {
        "digest": "sha256:3d6389a89a8cbc068208bbcf198eae3b63d5c46a5d18f1ac1e6b250ebcff228a",
        "specification_digest": "sha256:1a864c001fc21b59ba5129e884d5a5060bf2aebb047760fda45d5a0c5285a3e0",
        "platform": "win32-x64",
        "archive": {
            "asset": "josh-room-controller-windows-amd64.rcca",
            "url": "https://github.com/joshyorko/josh-room/releases/download/v0.1.11-controller-artifacts/josh-room-controller-windows-amd64.rcca",
            "sha256": "1d16ffca8958bd19b32e2747653983d7e2a9755d8534b97192eac5a915ca16d0",
            "size": 77963976,
        },
    }
    assert value["rcc"]["platforms"]["win32-x64"]["sha256"] == "7b62dc1f421f7cf33560c1b0567fc5bf8a65fc1d919af28d0ab633f85814a731"
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
        pin_manifest(root, template, artifact, receipt, "v0.1.7-controller-artifact")



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
        "source": "b" * 40,
    }))

    pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts", platform="win32-x64")
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
    }))
    with pytest.raises(ValueError, match="specification digest"):
        pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts")

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
    }))
    with pytest.raises(ValueError, match="artifact digest"):
        pin_manifest(root, template, artifact, receipt, "v0.1.11-controller-artifacts")

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
