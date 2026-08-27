import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).parents[1]
SECRET_FIELDS = {"access_key_id", "secret_access_key", "session_token", "age_identity"}


def _schema(name: str) -> dict:
    path = ROOT / "schemas" / name
    assert path.is_file(), f"missing schema contract: {name}"
    return json.loads(path.read_text())


def _validator(name: str) -> Draft202012Validator:
    schema = _schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_private_config_schema_defines_non_secret_dimensions():
    schema = _schema("private-config.schema.json")
    dimensions = schema["properties"]["dimensions"]
    record = schema["$defs"]["dimension"]

    assert dimensions["type"] == "object"
    assert dimensions["additionalProperties"] == {"$ref": "#/$defs/dimension"}
    assert set(record["required"]) == {
        "display_name",
        "provider",
        "endpoint",
        "bucket",
        "credential_profile",
    }
    assert record["properties"]["provider"]["enum"] == ["r2", "minio"]
    assert record["additionalProperties"] is False
    assert not SECRET_FIELDS & record["properties"].keys()
    assert {"r2", "minio"} <= schema["properties"].keys()


def test_dimension_catalog_v2_schema_binds_rooms_and_saved_fingerprints():
    schema = _schema("dimension-catalog-v2.schema.json")
    snapshot = schema["$defs"]["snapshot"]

    assert set(schema["required"]) == {
        "format_version",
        "dimension_id",
        "revision",
        "projects",
    }
    assert schema["properties"]["format_version"] == {"const": 2}
    assert schema["properties"]["projects"]["additionalProperties"] == {
        "$ref": "#/$defs/room"
    }
    assert {
        "snapshot_id",
        "object_key",
        "ciphertext_sha256",
        "ciphertext_size",
        "created_at",
        "workspace_fingerprint",
    } <= set(snapshot["required"])
    serialized = json.dumps(schema)
    assert not any(secret in serialized for secret in SECRET_FIELDS)


def test_marker_v2_schema_corroborates_dimension_room_jat_and_path():
    schema = _schema("workspace-marker-v2.schema.json")

    assert set(schema["required"]) == {
        "format_version",
        "dimension_id",
        "project_id",
        "display_name",
        "snapshot_id",
        "workspace_fingerprint",
        "workspace_path_sha256",
    }
    assert schema["properties"]["format_version"] == {"const": 2}
    assert schema["properties"]["workspace_fingerprint"]["pattern"] == "^[0-9a-f]{64}$"
    assert schema["properties"]["workspace_path_sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert schema["additionalProperties"] is False
    assert not SECRET_FIELDS & schema["properties"].keys()


def test_private_config_schema_validates_dimension_instances():
    validator = _validator("private-config.schema.json")
    record = {
        "display_name": "Archive",
        "provider": "r2",
        "endpoint": "https://example.invalid",
        "bucket": "archive",
        "credential_profile": "archive-profile",
        "catalog_key": "catalog.jroom.age",
        "temporary_credentials": True,
    }

    validator.validate({"default_dimension": "archive", "dimensions": {"archive": record}})

    invalid_instances = [
        {"dimensions": {"../room": record}},
        {"dimensions": {"archive": {**record, "secret_access_key": "synthetic"}}},
        {"dimensions": {"archive": {**record, "endpoint": "not a uri"}}},
        {"dimensions": {"archive": {**record, "temporary_credentials": "yes"}}},
    ]
    for instance in invalid_instances:
        with pytest.raises(ValidationError):
            validator.validate(instance)


def test_dimension_catalog_v2_schema_validates_instances():
    validator = _validator("dimension-catalog-v2.schema.json")
    snapshot = {
        "snapshot_id": "snap-1",
        "object_key": f"objects/sha256/{'a' * 64}",
        "ciphertext_sha256": "a" * 64,
        "ciphertext_size": 1,
        "created_at": "2026-08-26T00:00:00Z",
        "workspace_fingerprint": "b" * 64,
    }
    catalog = {
        "format_version": 2,
        "dimension_id": "archive",
        "revision": 1,
        "projects": {
            "demo": {
                "display_name": "Demo",
                "latest": "snap-1",
                "snapshots": {"snap-1": snapshot},
            }
        },
    }

    validator.validate(catalog)

    invalid_dimension = copy.deepcopy(catalog)
    invalid_dimension["dimension_id"] = "../archive"
    invalid_object = copy.deepcopy(catalog)
    invalid_object["projects"]["demo"]["snapshots"]["snap-1"]["object_key"] = "objects/../private"
    invalid_fingerprint = copy.deepcopy(catalog)
    invalid_fingerprint["projects"]["demo"]["snapshots"]["snap-1"]["workspace_fingerprint"] = "short"
    for instance in (invalid_dimension, invalid_object, invalid_fingerprint):
        with pytest.raises(ValidationError):
            validator.validate(instance)


def test_workspace_marker_v2_schema_validates_instances():
    validator = _validator("workspace-marker-v2.schema.json")
    marker = {
        "format_version": 2,
        "dimension_id": "archive",
        "project_id": "demo",
        "display_name": "Demo",
        "snapshot_id": "snap-1",
        "workspace_fingerprint": "b" * 64,
        "workspace_path_sha256": "c" * 64,
    }

    validator.validate(marker)

    invalid_dimension = {**marker, "dimension_id": "../archive"}
    invalid_path = {**marker, "workspace_path_sha256": "short"}
    credential_bearing = {**marker, "secret_access_key": "synthetic"}
    for instance in (invalid_dimension, invalid_path, credential_bearing):
        with pytest.raises(ValidationError):
            validator.validate(instance)


def test_private_config_schema_rejects_provider_inapplicable_options_and_bad_endpoints():
    validator = _validator("private-config.schema.json")
    record = {
        "display_name": "Archive",
        "provider": "r2",
        "endpoint": "https://example.invalid",
        "bucket": "archive",
        "credential_profile": "archive-profile",
    }
    invalid_records = [
        {**record, "verify_tls": True},
        {**record, "ca_bundle": "synthetic-ca.pem"},
        {**record, "path_style": True},
        {**record, "endpoint": "file:///tmp/room"},
        {**record, "endpoint": "https:///missing-host"},
        {**record, "endpoint": "https://user:secret@example.invalid"},
        {
            **record,
            "provider": "minio",
            "temporary_credentials": True,
        },
    ]

    for invalid_record in invalid_records:
        with pytest.raises(ValidationError):
            validator.validate({"dimensions": {"archive": invalid_record}})


def test_dimension_catalog_schema_constrains_project_and_snapshot_identifiers():
    validator = _validator("dimension-catalog-v2.schema.json")
    snapshot = {
        "snapshot_id": "snap-1",
        "object_key": f"objects/sha256/{'a' * 64}",
        "ciphertext_sha256": "a" * 64,
        "ciphertext_size": 1,
        "created_at": "2026-08-26T00:00:00Z",
        "workspace_fingerprint": "b" * 64,
    }
    catalog = {
        "format_version": 2,
        "dimension_id": "archive",
        "revision": 1,
        "projects": {
            "demo": {
                "display_name": "Demo",
                "latest": "snap-1",
                "snapshots": {"snap-1": snapshot},
            }
        },
    }

    invalid_project = copy.deepcopy(catalog)
    invalid_project["projects"]["../demo"] = invalid_project["projects"].pop("demo")
    invalid_snapshot = copy.deepcopy(catalog)
    invalid_snapshot["projects"]["demo"]["snapshots"]["../snap-1"] = (
        invalid_snapshot["projects"]["demo"]["snapshots"].pop("snap-1")
    )

    for invalid_catalog in (invalid_project, invalid_snapshot):
        with pytest.raises(ValidationError):
            validator.validate(invalid_catalog)
