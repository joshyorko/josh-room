"""An active authority session must not freeze the local MinIO registry."""

import io
import json
import sys

import pytest
from synthetic_identity import synthetic_identity

from josh_room import auth, cli, config


def minio_connection(**updates):
    return {
        "display_name": "Local storage",
        "provider": "minio",
        "endpoint": "http://127.0.0.1:19000",
        "credential_profile": "fixture-minio",
        "region": "us-east-1",
        **updates,
    }


@pytest.fixture
def active_r2_session(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_INSTANCE", str(tmp_path / "instance"))
    for name in (
        "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_RUNTIME_CREDENTIALS",
        "JOSH_ROOM_RUNTIME_PROFILE", "JOSH_ROOM_IDENTITY",
    ):
        monkeypatch.setenv(name, "")
    persisted = {
        "connections": {
            "cloud": {
                "display_name": "R2",
                "provider": "r2",
                "endpoint": "https://persisted.example.invalid",
                "credential_profile": "persisted-r2",
            },
        },
        "dimensions": {
            "archive": {"display_name": "Archive", "connection_id": "cloud", "bucket": "archive"},
        },
    }
    config.save_private_config(persisted)
    auth._write_runtime({
        "purpose": "r2",
        "accessKeyId": "synthetic-r2-access",
        "secretAccessKey": "synthetic-r2-secret",
        "sessionToken": "synthetic-r2-session",
        "ageIdentity": synthetic_identity("runtime-registry"),
        "ageRecipients": ["synthetic-daily", "synthetic-recovery"],
        "endpoint": "https://authority.example.invalid",
        "bucket": "archive",
        "expiresIn": 600,
    }, dimension_id="archive")
    yield persisted
    auth._clear_runtime_session()


@pytest.mark.parametrize("canonical", [True, False])
def test_created_minio_connection_is_visible_after_r2_session(
    active_r2_session, monkeypatch, capsys, canonical,
):
    monkeypatch.setattr(cli, "store_keyring", lambda *_args: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "endpoint": "http://127.0.0.1:19000",
        "access-key-id": "synthetic-minio-access",
        "secret-access-key": "synthetic-minio-secret",
    })))
    create = ["provider", "connection", "create", "--provider", "minio", "--endpoint", "http://127.0.0.1:19000"] if canonical else ["connections", "setup", "minio"]
    assert cli.main([*create, "--connection", "local", "--credential-profile", "fixture-minio", "--json"]) == 0
    capsys.readouterr()

    current = config.connection_configs(config.private_config())
    assert current["local"].credential_profile == "fixture-minio"
    assert current["cloud"].credential_profile == "oauth-runtime"
    assert current["cloud"].endpoint == "https://authority.example.invalid"


def test_new_minio_dimension_is_visible_after_r2_session(active_r2_session):
    persisted = active_r2_session
    persisted["connections"]["local"] = minio_connection()
    persisted["dimensions"]["local-room"] = {
        "display_name": "Local room", "connection_id": "local", "bucket": "local-room",
    }
    persisted["default_dimension"] = "local-room"
    config.save_private_config(persisted)

    registry = config.DimensionRegistry(config.private_config())
    assert registry.select().dimension_id == "local-room"
    assert registry.select("local-room").credential_profile == "fixture-minio"
    assert registry.select("archive").credential_profile == "oauth-runtime"


def test_updated_and_removed_minio_records_do_not_survive_in_session_snapshot(
    active_r2_session,
):
    persisted = active_r2_session
    persisted["connections"]["local"] = minio_connection()
    persisted["dimensions"]["old-room"] = {
        "display_name": "Old", "connection": "local", "bucket": "old-room",
    }
    config.save_private_config(persisted)
    # A session from before the local edits contains the old registry.
    path = auth._runtime_paths()[2]
    snapshot = json.loads(path.read_text())
    snapshot["connections"]["local"] = minio_connection()
    snapshot["dimensions"]["old-room"] = persisted["dimensions"]["old-room"]
    snapshot["default_dimension"] = "old-room"
    path.write_text(json.dumps(snapshot))
    before = {item: item.read_bytes() for item in auth._runtime_paths()}

    persisted["connections"]["local"] = minio_connection(endpoint="http://127.0.0.1:19001", auth_state="disconnected")
    del persisted["dimensions"]["old-room"]
    config.save_private_config(persisted)
    current = config.private_config()
    assert config.connection_configs(current)["local"].endpoint == "http://127.0.0.1:19001"
    assert config.connection_configs(current)["local"].auth_state == "disconnected"
    assert "old-room" not in config.dimension_configs(current)
    assert "default_dimension" not in current
    assert current["age_recipients"] == ["synthetic-daily", "synthetic-recovery"]
    assert all(item.read_bytes() == value for item, value in before.items())


@pytest.mark.parametrize("legacy", [True, False])
def test_minio_legacy_and_inline_dimensions_refresh_from_persisted_config(active_r2_session, legacy):
    persisted = active_r2_session
    record = {**minio_connection(), "bucket": "local-room"}
    if legacy:
        persisted["minio"] = {key: value for key, value in record.items() if key not in {"provider", "display_name"}}
        selected = "minio"
    else:
        persisted["dimensions"]["inline"] = record
        selected = "inline"
    config.save_private_config(persisted)
    current = config.DimensionRegistry(config.private_config()).select(selected)
    assert current.provider == "minio"
    assert current.bucket == "local-room"


def test_runtime_only_config_remains_available_without_persisted_file(active_r2_session):
    path = config.config_dir() / "config.json"
    path.unlink()
    assert config.DimensionRegistry(config.private_config()).select("archive").credential_profile == "oauth-runtime"


def test_provider_change_cannot_mix_runtime_r2_with_persisted_minio(active_r2_session):
    persisted = active_r2_session
    persisted["connections"]["cloud"] = minio_connection()
    config.save_private_config(persisted)
    with pytest.raises(ValueError, match="provider changed.*cloud"):
        config.private_config()


def test_malformed_persisted_registry_does_not_fall_back_to_old_snapshot(active_r2_session):
    persisted = active_r2_session
    persisted["connections"] = []
    config.save_private_config(persisted)
    with pytest.raises(TypeError, match="connections configuration must be an object"):
        config.private_config()


@pytest.mark.parametrize("shape", ["legacy", "inline", "connection"])
def test_deleted_minio_records_are_not_resurrected(active_r2_session, shape):
    path = auth._runtime_paths()[2]
    snapshot = json.loads(path.read_text())
    if shape == "legacy":
        snapshot["minio"] = {**minio_connection(), "bucket": "local-room"}
        snapshot["minio"].pop("provider")
        snapshot["minio"].pop("display_name")
        removed = "minio"
    elif shape == "inline":
        snapshot["dimensions"]["local-room"] = {**minio_connection(), "bucket": "local-room"}
        removed = "local-room"
    else:
        snapshot["connections"]["local"] = minio_connection()
        snapshot["dimensions"]["local-room"] = {"display_name": "Local", "connection_id": "local", "bucket": "local-room"}
        removed = "local-room"
    path.write_text(json.dumps(snapshot))
    current = config.private_config()
    assert removed not in config.dimension_configs(current)
    assert "local" not in config.connection_configs(current)


@pytest.mark.parametrize("body", [None, [], "invalid"])
def test_existing_persisted_config_must_be_an_object(active_r2_session, body):
    (config.config_dir() / "config.json").write_text(json.dumps(body))
    with pytest.raises(TypeError, match="private configuration must be an object"):
        config.private_config()


@pytest.mark.parametrize("body", [None, [], "invalid"])
def test_runtime_config_must_be_an_object(active_r2_session, body):
    auth._runtime_paths()[2].write_text(json.dumps(body))
    with pytest.raises(TypeError, match="runtime configuration must be an object"):
        config.private_config()
