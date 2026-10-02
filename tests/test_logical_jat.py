import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from josh_room.logical_jat import LogicalJat

ROOT = Path(__file__).parents[1]


def _valid_descriptor():
    return {
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": "jat-01",
        "dimension_id": "dimension-01",
        "encryption_domain_id": "domain-01",
        "room_id": "room-01",
        "created_at": "2026-10-02T12:30:00Z",
        "capture_policy_sha256": "a" * 64,
        "origin_room_id": "room-original",
        "workspace": {
            "engine": "restic",
            "repository_id": "b" * 64,
            "repository_format": 2,
            "snapshot_id": "c" * 64,
            "tree_id": "d" * 64,
            "source_path": ".",
            "logical_bytes": 12,
            "data_added": 4,
            "data_added_packed": 3,
        },
        "components": {
            "rcc_environment": None,
            "homebrew_recovery": None,
            "hauler_content": None,
        },
        "source": {"git_commit": "7" * 40, "dirty": True},
        "producer": {
            "josh_room_version": "0.1.0",
            "restic_version": "0.19.1",
            "source_platform": "linux-x64",
            "restore_platforms": ["linux-x64", "win32-x64"],
        },
    }


def _component(kind):
    snapshot = {
        "repository_id": "b" * 64,
        "repository_format": 2,
        "snapshot_id": "1" * 64,
        "tree_id": "2" * 64,
    }
    if kind == "rcca":
        return {
            "kind": kind,
            "snapshot": snapshot,
            "archive_sha256": "e" * 64,
            "archive_size": 123,
            "member_basename": "rcc-environment.rcca",
            "artifact_digest": "sha256:" + "3" * 64,
            "specification_digest": "sha256:" + "4" * 64,
            "platform": "linux_amd64",
            "rcc_version": "v18.19.5",
            "robot_relative_path": "robot.yaml",
        }
    if kind == "homebrew-recovery":
        return {
            "kind": kind,
            "snapshot": snapshot,
            "archive_sha256": "e" * 64,
            "archive_size": 123,
            "member_basename": "homebrew-recovery.tar.zst",
        }
    return {
        "kind": kind,
        "snapshot": snapshot,
        "archive_sha256": "e" * 64,
        "archive_size": 234,
        "member_basename": "hauler-content.tar.zst",
        "references": [{"digest": "sha256:" + "5" * 64, "media_type": "application/vnd.oci.image.manifest.v1+json"}],
    }


def _validator():
    schema_path = ROOT / "schemas" / "logical-jat-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_v1_schema_accepts_small_complete_snapshot_and_components():
    descriptor = _valid_descriptor()
    descriptor["parent_logical_jat_id"] = "jat-previous"
    descriptor["workspace"]["parent_snapshot_id"] = "9" * 64
    descriptor["components"]["rcc_environment"] = _component("rcca")
    descriptor["components"]["homebrew_recovery"] = _component("homebrew-recovery")
    descriptor["components"]["hauler_content"] = _component("hauler-content")

    _validator().validate(descriptor)
    encoded = LogicalJat.from_dict(descriptor).to_json()
    assert len(encoded.encode("utf-8")) <= 64 * 1024


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(format_version=True),
        lambda body: body.update(format_version=2),
        lambda body: body.update(unrecognized="field"),
        lambda body: body.update(access_token="synthetic-secret"),
        lambda body: body.update(room_id="/private/room"),
        lambda body: body.update(origin_room_id="../private"),
        lambda body: body.update(created_at="2026-10-02 12:30:00"),
        lambda body: body.update(capture_policy_sha256="x" * 64),
        lambda body: body["workspace"].update(repository_format=True),
        lambda body: body["workspace"].update(snapshot_id="../snapshot"),
        lambda body: body["workspace"].update(tree_id="z" * 64),
        lambda body: body["workspace"].update(logical_bytes=True),
        lambda body: body["components"].update(rcc_environment=_component("homebrew-recovery")),
        lambda body: body["components"].update(homebrew_recovery={**_component("homebrew-recovery"), "token": "secret"}),
        lambda body: body["components"].update(rcc_environment={**_component("rcca"), "member_basename": "private.rcca"}),
        lambda body: body["components"].update(rcc_environment={**_component("rcca"), "archive_sha256": "not-a-digest"}),
        lambda body: body["components"].update(rcc_environment={**_component("rcca"), "artifact_digest": "sha256:invalid"}),
        lambda body: body["components"].update(rcc_environment={**_component("rcca"), "robot_relative_path": "../outside/robot.yaml"}),
        lambda body: body.update(source={"workspace_path": "/private/workspace"}),
        lambda body: body.update(producer={**body["producer"], "private_repo": "owner/private"}),
        lambda body: body["producer"].update(restore_platforms=[]),
        lambda body: body["producer"].update(source_platform="macos"),
    ],
)
def test_schema_rejects_invalid_or_private_descriptor_fields(mutate):
    descriptor = _valid_descriptor()
    mutate(descriptor)
    with pytest.raises(ValidationError):
        _validator().validate(descriptor)
    with pytest.raises((TypeError, ValueError)):
        LogicalJat.from_dict(descriptor)


def test_python_contract_round_trips_canonically_and_hashes_exact_bytes():
    descriptor = _valid_descriptor()
    first = LogicalJat.from_dict(descriptor)
    second = LogicalJat.from_json(json.dumps(descriptor, indent=2))

    expected = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert first.to_json() == expected
    assert second.to_json() == expected
    assert first.sha256 == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert first.to_dict() == descriptor

    descriptor["workspace"]["snapshot_id"] = "8" * 64
    emitted = first.to_dict()
    emitted["workspace"]["snapshot_id"] = "9" * 64
    assert first.to_dict()["workspace"]["snapshot_id"] == "c" * 64


def test_python_contract_rejects_duplicate_json_keys_and_oversized_descriptor():
    with pytest.raises(ValueError, match="duplicate"):
        LogicalJat.from_json('{"format_version":1,"format_version":1}')
    with pytest.raises(ValueError, match="size"):
        LogicalJat.from_json(" " * (64 * 1024) + json.dumps(_valid_descriptor()))


def test_parent_hints_do_not_change_selected_complete_snapshot_reference():
    current = _valid_descriptor()
    with_parent = copy.deepcopy(current)
    with_parent["parent_logical_jat_id"] = "missing-parent"
    with_parent["workspace"]["parent_snapshot_id"] = "8" * 64

    parsed = LogicalJat.from_dict(with_parent)
    assert parsed.to_dict()["workspace"]["snapshot_id"] == current["workspace"]["snapshot_id"]
    assert parsed.to_dict()["parent_logical_jat_id"] == "missing-parent"


def test_python_contract_rejects_component_repository_mismatch():
    descriptor = _valid_descriptor()
    descriptor["components"]["rcc_environment"] = _component("rcca")
    descriptor["components"]["rcc_environment"]["snapshot"]["repository_id"] = "3" * 64

    with pytest.raises(ValueError, match="repository"):
        LogicalJat.from_dict(descriptor)


def test_python_contract_requires_explicit_distinct_origin_for_copied_room():
    descriptor = _valid_descriptor()
    descriptor["origin_room_id"] = descriptor["room_id"]

    with pytest.raises(ValueError, match="origin room"):
        LogicalJat.from_dict(descriptor)


def test_python_contract_allows_origin_binding_to_be_omitted():
    descriptor = _valid_descriptor()
    descriptor.pop("origin_room_id")

    assert "origin_room_id" not in LogicalJat.from_dict(descriptor).to_dict()
