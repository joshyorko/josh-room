"""Contract tests for the mixed legacy/Room Store Dimension Catalog v3."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from josh_room.catalog import Catalog, CatalogConflict
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat

DIMENSION_ID = "dimension-01"
DOMAIN_ID = "00000000-0000-4000-8000-000000000001"
PROJECT_ID = "room-01"
SIGNATURE = "e" * 64
ROOT = Path(__file__).parents[1]


def _descriptor(**overrides):
    body = {
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": "jat-01",
        "dimension_id": DIMENSION_ID,
        "encryption_domain_id": DOMAIN_ID,
        "room_id": PROJECT_ID,
        "created_at": "2026-10-02T12:30:00Z",
        "capture_policy_sha256": "a" * 64,
        "workspace": {
            "engine": "restic",
            "repository_id": "b" * 64,
            "repository_format": 2,
            "snapshot_id": "c" * 64,
            "tree_id": "d" * 64,
            "source_path": ".",
            "logical_bytes": 123,
            "data_added": 45,
            "data_added_packed": 34,
        },
        "components": {
            "rcc_environment": {
                "kind": "rcca",
                "snapshot": {
                    "repository_id": "b" * 64,
                    "repository_format": 2,
                    "snapshot_id": "1" * 64,
                    "tree_id": "2" * 64,
                },
                "archive_sha256": "e" * 64,
                "archive_size": 123,
                "member_basename": "rcc-environment.rcca",
                "artifact_digest": "sha256:" + "3" * 64,
                "specification_digest": "sha256:" + "4" * 64,
                "platform": "linux_amd64",
                "rcc_version": "v18.19.5",
                "robot_relative_path": "robot.yaml",
            },
            "homebrew_recovery": None,
            "hauler_content": None,
        },
        "source": {},
        "producer": {
            "josh_room_version": "0.1.0",
            "restic_version": "0.19.1",
            "source_platform": "linux-x64",
            "restore_platforms": ["linux-x64", "win32-x64"],
        },
    }
    body.update(overrides)
    return LogicalJat.from_dict(body)


def _legacy_snapshot(snapshot_id="legacy-01", digest="1" * 64):
    return {
        "snapshot_id": snapshot_id,
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": 10,
        "created_at": "2026-10-01T12:00:00+00:00",
        "workspace_fingerprint": "2" * 64,
    }


def _legacy_catalog():
    return Catalog.empty(DIMENSION_ID, DOMAIN_ID).add_snapshot(
        PROJECT_ID, "Synthetic Room", _legacy_snapshot()
    )


def _object_ref(body=b"encrypted logical descriptor"):
    digest = hashlib.sha256(body).hexdigest()
    return ObjectRef(f"objects/sha256/{digest}", digest, len(body))


def _schema_validator():
    schema = json.loads((ROOT / "schemas" / "dimension-catalog-v3.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _v3_body():
    return {
        "format_version": 3,
        "dimension_id": DIMENSION_ID,
        "encryption_domain_id": DOMAIN_ID,
        "revision": 1,
        "projects": {
            PROJECT_ID: {
                "display_name": "Synthetic Room",
                "latest": "jat-01",
                "snapshots": {
                    "legacy-01": _legacy_snapshot(),
                    "jat-01": {
                        "snapshot_id": "jat-01",
                        "payload_kind": "room-store-v1",
                        "dimension_id": DIMENSION_ID,
                        "encryption_domain_id": DOMAIN_ID,
                        "room_id": PROJECT_ID,
                        "object_key": "objects/sha256/" + "3" * 64,
                        "ciphertext_sha256": "3" * 64,
                        "ciphertext_size": 123,
                        "created_at": "2026-10-02T12:30:00Z",
                        "repository_id": "b" * 64,
                        "repository_format": 2,
                        "workspace_snapshot_id": "c" * 64,
                        "tree_id": "d" * 64,
                        "capture_policy_sha256": "a" * 64,
                        "workspace_signature": SIGNATURE,
                        "signature_algorithm": "josh-room-stat-v1",
                        "logical_bytes": 123,
                        "data_added": 45,
                        "data_added_packed": 34,
                        "component_refs": {
                            "rcc_environment": {"snapshot_id": "1" * 64, "tree_id": "2" * 64},
                            "homebrew_recovery": None,
                            "hauler_content": None,
                        },
                    },
                },
            }
        },
    }


def test_v3_schema_accepts_mixed_records_and_rejects_legacy_hash_fabrication():
    validator = _schema_validator()
    body = _v3_body()

    validator.validate(body)

    logical = body["projects"][PROJECT_ID]["snapshots"]["jat-01"]
    assert "workspace_fingerprint" not in logical
    invalid_kind = copy.deepcopy(body)
    invalid_kind["projects"][PROJECT_ID]["snapshots"]["jat-01"]["payload_kind"] = "unknown"
    fabricated_fingerprint = copy.deepcopy(body)
    fabricated_fingerprint["projects"][PROJECT_ID]["snapshots"]["jat-01"]["workspace_fingerprint"] = "f" * 64
    for invalid in (invalid_kind, fabricated_fingerprint):
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_v3_schema_rejects_boolean_revisions_and_missing_domain_binding():
    validator = _schema_validator()
    valid = _v3_body()
    invalid_revision = copy.deepcopy(valid)
    invalid_revision["revision"] = True
    invalid_version = copy.deepcopy(valid)
    invalid_version["format_version"] = True
    missing_domain = copy.deepcopy(valid)
    missing_domain.pop("encryption_domain_id")

    for invalid in (invalid_revision, invalid_version, missing_domain):
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_v3_catalog_requires_latest_to_match_snapshot_reachability():
    body = _v3_body()

    assert Catalog.from_body(body).latest(PROJECT_ID)["snapshot_id"] == "jat-01"

    body["projects"][PROJECT_ID]["latest"] = None
    with pytest.raises(ValueError, match="latest"):
        Catalog.from_body(body)


def test_logical_snapshot_corroboration_rejects_index_descriptor_mismatch():
    from josh_room.catalog import corroborate_logical_snapshot

    record = _v3_body()["projects"][PROJECT_ID]["snapshots"]["jat-01"]

    assert corroborate_logical_snapshot(PROJECT_ID, record, _descriptor()) == _descriptor()

    for field in ("repository_id", "workspace_snapshot_id", "tree_id", "capture_policy_sha256", "logical_bytes"):
        tampered = copy.deepcopy(record)
        tampered[field] = True if field == "logical_bytes" else "f" * 64
        with pytest.raises(ValueError, match="does not match"):
            corroborate_logical_snapshot(PROJECT_ID, tampered, _descriptor())
    tampered = copy.deepcopy(record)
    tampered["component_refs"]["rcc_environment"] = None
    with pytest.raises(ValueError, match="does not match"):
        corroborate_logical_snapshot(PROJECT_ID, tampered, _descriptor())


def test_v2_migrates_only_when_explicitly_adding_a_logical_snapshot():
    legacy = _legacy_catalog()
    before = copy.deepcopy(legacy.body)

    updated = legacy.add_logical_snapshot(
        PROJECT_ID, "Synthetic Room", _descriptor(), _object_ref(), SIGNATURE
    )

    assert updated.body["format_version"] == 3
    assert updated.body["revision"] == legacy.body["revision"] + 1
    assert legacy.body == before
    assert updated.body["projects"][PROJECT_ID]["snapshots"]["legacy-01"] == before["projects"][PROJECT_ID]["snapshots"]["legacy-01"]
    assert updated.body["projects"][PROJECT_ID]["latest"] == "jat-01"
    assert updated.latest(PROJECT_ID)["payload_kind"] == "room-store-v1"
    assert updated.latest(PROJECT_ID)["workspace_signature"] == SIGNATURE
    assert updated.latest(PROJECT_ID)["signature_algorithm"] == "josh-room-stat-v1"


def test_v2_reads_do_not_automatically_upgrade_to_v3():
    legacy = _legacy_catalog()

    loaded = Catalog.from_body(copy.deepcopy(legacy.body), DIMENSION_ID, DOMAIN_ID)

    assert loaded.body == legacy.body
    assert loaded.body["format_version"] == 2


def test_v3_resolves_legacy_and_logical_records_and_keeps_complete_refs():
    updated = _legacy_catalog().add_logical_snapshot(
        PROJECT_ID, "Synthetic Room", _descriptor(), _object_ref(), SIGNATURE
    )

    assert updated.resolve_snapshot(PROJECT_ID, "legacy-01")["snapshot_id"] == "legacy-01"
    logical = updated.resolve_snapshot(PROJECT_ID, "latest")
    assert logical["snapshot_id"] == "jat-01"
    assert logical["repository_id"] == "b" * 64
    assert logical["workspace_snapshot_id"] == "c" * 64
    assert logical["tree_id"] == "d" * 64
    assert logical["capture_policy_sha256"] == "a" * 64
    assert logical["logical_bytes"] == 123
    assert logical["data_added"] == 45
    assert logical["data_added_packed"] == 34


@pytest.mark.parametrize(
    "change",
    [
        lambda descriptor: descriptor.update(payload_kind="unknown-v1"),
        lambda descriptor: descriptor.update(logical_jat_id="../outside"),
        lambda descriptor: descriptor.update(dimension_id="other-dimension"),
        lambda descriptor: descriptor.update(encryption_domain_id="00000000-0000-4000-8000-000000000002"),
        lambda descriptor: descriptor["workspace"].update(logical_bytes=True),
    ],
)
def test_logical_add_rejects_invalid_or_cross_bound_descriptor(change):
    body = _descriptor().to_dict()
    change(body)

    with pytest.raises((TypeError, ValueError)):
        descriptor = LogicalJat.from_dict(body)
        _legacy_catalog().add_logical_snapshot(
            PROJECT_ID, "Synthetic Room", descriptor, _object_ref(), SIGNATURE
        )


def test_logical_add_rejects_object_ref_key_digest_and_size_mismatches():
    descriptor = _descriptor()
    good = _object_ref()

    for ref in (
        ObjectRef(good.key, "0" * 64, good.size),
        ObjectRef("objects/sha256/" + "0" * 64, good.sha256, good.size),
        ObjectRef(good.key, good.sha256, 0),
    ):
        with pytest.raises(ValueError):
            _legacy_catalog().add_logical_snapshot(
                PROJECT_ID, "Synthetic Room", descriptor, ref, SIGNATURE
            )


def test_logical_add_rejects_invalid_signature_and_wrong_project_binding():
    descriptor = _descriptor()

    with pytest.raises(ValueError):
        _legacy_catalog().add_logical_snapshot(
            PROJECT_ID, "Synthetic Room", descriptor, _object_ref(), "short"
        )

    with pytest.raises(ValueError):
        _legacy_catalog().add_logical_snapshot(
            "other-room", "Other Room", descriptor, _object_ref(), SIGNATURE
        )


def test_copy_reseals_descriptor_and_removal_never_returns_restic_pack_refs():
    descriptor = _descriptor()
    ref = _object_ref(descriptor.to_json().encode())
    logical = Catalog.empty(DIMENSION_ID, DOMAIN_ID).add_logical_snapshot(
        PROJECT_ID, "Synthetic Room", descriptor, ref, SIGNATURE
    )
    copied_body = descriptor.to_dict()
    copied_body.update(logical_jat_id="jat-02", room_id="room-02", origin_room_id=PROJECT_ID)
    copied = LogicalJat.from_dict(copied_body)
    copied_ref = _object_ref(copied.to_json().encode())
    with_copy = logical.add_logical_snapshot("room-02", "Copy", copied, copied_ref, SIGNATURE)

    copy_record = with_copy.latest("room-02")
    source_record = with_copy.latest(PROJECT_ID)
    assert copy_record["object_key"] == copied_ref.key
    assert copy_record["object_key"] != source_record["object_key"]
    assert copy_record["origin_room_id"] == PROJECT_ID
    assert copy_record["repository_id"] == source_record["repository_id"]
    assert copy_record["workspace_snapshot_id"] == source_record["workspace_snapshot_id"]
    assert copy_record["tree_id"] == source_record["tree_id"]
    assert copy_record["component_refs"] == source_record["component_refs"]

    after_remove, removable, _ = with_copy.remove_snapshot("room-02", "jat-02")

    assert removable == [copied_ref.key]
    assert after_remove.resolve_snapshot(PROJECT_ID, "latest")["object_key"] == ref.key
    assert "b" * 64 not in removable
    assert "c" * 64 not in removable


def test_v3_rejects_boolean_and_unsupported_versions_or_revisions():
    catalog = _legacy_catalog().add_logical_snapshot(
        PROJECT_ID, "Synthetic Room", _descriptor(), _object_ref(), SIGNATURE
    )

    for field, value in (
        ("format_version", True),
        ("format_version", 4),
        ("revision", True),
        ("revision", -1),
    ):
        body = copy.deepcopy(catalog.body)
        body[field] = value
        with pytest.raises((TypeError, ValueError)):
            Catalog.from_body(body, DIMENSION_ID, DOMAIN_ID)


def test_v3_rejects_stale_revision_publication():
    catalog = _legacy_catalog().add_logical_snapshot(
        PROJECT_ID, "Synthetic Room", _descriptor(), _object_ref(), SIGNATURE
    )

    with pytest.raises(CatalogConflict, match="stale"):
        catalog.update_if_revision(catalog.body["revision"] - 1, catalog.body)


def test_catalog_update_rejects_boolean_expected_revision():
    catalog = _legacy_catalog()

    with pytest.raises(CatalogConflict):
        catalog.update_if_revision(True, catalog.body)
