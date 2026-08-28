import json
from pathlib import Path

import pytest

from scripts.build_controller_artifact import (
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
            "version": "v18.19.3",
            "platforms": {
                "linux-x64": {
                    "asset": "rcc-linux64",
                    "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.3/rcc-linux64",
                    "sha256": None,
                },
            },
        },
        "controller": {
            "robot": "vscode-extension/runtime/controller/robot.yaml",
            "artifact_asset": "josh-room-controller-linux-amd64.rcca",
        },
    }


def test_rcc_pin_requires_real_v18_19_3_checksum_before_build():
    with pytest.raises(ValueError, match="checksum is pending"):
        resolve_rcc_pin(manifest(), "linux-x64")

    pin = resolve_rcc_pin(manifest(), "linux-x64", "a" * 64)
    assert pin["version"] == "v18.19.3"
    assert pin["sha256"] == "a" * 64


def test_checked_in_controller_manifest_is_v18_19_3_only_and_has_no_fake_checksums():
    root = Path(__file__).parents[1]
    value = load_manifest(root / "vscode-extension/runtime/controller-artifact-manifest.json")
    template = root / "templates/room/vscode-extension/runtime/controller-artifact-manifest.json"
    assert (root / "vscode-extension/runtime/controller-artifact-manifest.json").read_bytes() == template.read_bytes()
    assert value["rcc"]["version"] == "v18.19.3"
    assert value["rcc"]["platforms"]["linux-x64"] == {
        "asset": "rcc-linux64",
        "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.3/rcc-linux64",
        "sha256": "7e588c01751ca2ae15ba13ef67f2f4b7567697a5a8389737059a73936f509428",
        "size": 22282402,
    }
    assert value["rcc"]["platforms"]["win32-x64"]["sha256"] == "523a6be8ad92235fbe0a4e4732699f2cd66f9ef6ad57e045df434257c46112e4"
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


def test_receipt_is_immutable_and_carries_controller_provenance(tmp_path):
    receipt = {
        "format_version": 1,
        "artifact_digest": "sha256:" + "b" * 64,
        "specification_digest": "sha256:" + "c" * 64,
        "legacy_blueprint_key": "blueprint-1",
        "archive": {"sha256": "d" * 64, "size": 123},
        "rcc_version": "v18.19.3",
        "source": "e" * 40,
        "platform": "linux-x64",
        "verified_acquire": True,
        "verified_no_build": True,
        "verified_exec": True,
    }
    validate_receipt(receipt, expected_platform="linux-x64", expected_rcc="v18.19.3")
    broken = {**receipt, "rcc_version": "v18.19.2"}
    with pytest.raises(ValueError, match="RCC version"):
        validate_receipt(broken, expected_platform="linux-x64", expected_rcc="v18.19.3")


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
        "rcc_version": "v18.19.3",
        "platform": "linux-x64",
        "source": "b" * 40,
    }))
    with pytest.raises(ValueError, match="archive SHA256"):
        pin_manifest(root, template, artifact, receipt, "v0.1.7-controller-artifact")
