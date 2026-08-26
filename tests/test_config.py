import io
import json
import stat
import sys

import pytest

from josh_room import config as config_module
from josh_room.cli import _instance_root, main
from josh_room.config import auth_status, private_config


def test_legacy_r2_config_is_available_as_a_dimension():
    assert hasattr(config_module, "DimensionConfig"), (
        "Dimension configuration contract is missing"
    )
    assert hasattr(config_module, "dimension_configs"), (
        "Dimension compatibility reader is missing"
    )
    config = {
        "default_backend": "r2",
        "r2": {
            "endpoint": "https://synthetic.invalid",
            "bucket": "legacy-room",
            "credential_profile": "legacy-profile",
            "catalog_key": "catalog.jroom.age",
        },
    }

    dimensions = config_module.dimension_configs(config)

    assert dimensions["r2"] == config_module.DimensionConfig(
        dimension_id="r2",
        display_name="Cloudflare R2",
        provider="r2",
        endpoint="https://synthetic.invalid",
        bucket="legacy-room",
        credential_profile="legacy-profile",
        catalog_key="catalog.jroom.age",
        region="auto",
    )


def test_dimension_serialization_is_non_secret_and_rejects_inline_credentials():
    assert hasattr(config_module, "DimensionConfig"), (
        "Dimension configuration contract is missing"
    )
    dimension = config_module.DimensionConfig(
        dimension_id="archive",
        display_name="Archive",
        provider="minio",
        endpoint="https://minio.synthetic.invalid",
        bucket="archive",
        credential_profile="archive-profile",
        catalog_key="archive.jroom.age",
        region="us-east-1",
    )

    assert dimension.to_private() == {
        "display_name": "Archive",
        "provider": "minio",
        "endpoint": "https://minio.synthetic.invalid",
        "bucket": "archive",
        "credential_profile": "archive-profile",
        "catalog_key": "archive.jroom.age",
        "region": "us-east-1",
    }
    assert (
        not {"access_key_id", "secret_access_key", "session_token", "age_identity"}
        & dimension.to_private().keys()
    )

    with pytest.raises(ValueError, match="unsupported Dimension setting"):
        config_module.DimensionConfig.from_private(
            "unsafe",
            {
                **dimension.to_private(),
                "secret_access_key": "must-not-be-serialized",
            },
        )
    with pytest.raises(ValueError, match="unsupported Dimension setting"):
        config_module.DimensionConfig(
            dimension_id="unsafe",
            display_name="Unsafe",
            provider="r2",
            endpoint="https://synthetic.invalid",
            bucket="unsafe",
            credential_profile="unsafe-profile",
            options=(("age_identity", "must-not-be-serialized"),),
        )


def _dimension_body(**overrides):
    body = {
        "display_name": "Archive",
        "provider": "r2",
        "endpoint": "https://example.invalid",
        "bucket": "archive",
        "credential_profile": "archive-profile",
    }
    body.update(overrides)
    return body


def test_dimension_rejects_endpoint_userinfo():
    with pytest.raises(ValueError, match="userinfo"):
        config_module.DimensionConfig.from_private(
            "archive",
            _dimension_body(endpoint="https://user:secret@example.invalid"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dimension_id", "../room"),
        ("catalog_key", "catalog\\private"),
        ("catalog_key", "catalog key"),
    ],
)
def test_dimension_rejects_unsafe_identifiers(field, value):
    body = _dimension_body()
    dimension_id = "archive"
    if field == "dimension_id":
        dimension_id = value
    else:
        body[field] = value

    with pytest.raises(ValueError, match=field.replace("_", " ")):
        config_module.DimensionConfig.from_private(dimension_id, body)


@pytest.mark.parametrize(
    ("provider", "option", "value"),
    [
        ("r2", "temporary_credentials", "yes"),
        ("r2", "multipart_threshold", 0),
        ("minio", "verify_tls", "yes"),
        ("minio", "ca_bundle", 7),
        ("minio", "region", 7),
    ],
)
def test_dimension_rejects_option_types_that_violate_the_schema(
    provider, option, value
):
    with pytest.raises((TypeError, ValueError), match=option.replace("_", " ")):
        config_module.DimensionConfig.from_private(
            "archive",
            _dimension_body(provider=provider, **{option: value}),
        )


def test_doctor_reports_keyring_profile_without_values(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"r2": {"endpoint": "https://example.invalid", "bucket": "synthetic", "credential_profile": "synthetic", "temporary_credentials": True}}))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("josh_room.config.available", lambda: True)
    status = auth_status()
    assert status == {"state": "configured-unverified", "mode": "s3-api-credentials", "credential_source": "os-secret-service", "credentials_verified": False, "bucket_configured": True, "temporary_credentials_preferred": True}
    assert "synthetic" not in json.dumps(status)


def test_setup_stores_secrets_in_keyring_and_only_metadata_in_config(tmp_path, monkeypatch, capsys):
    stored = []
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("josh_room.cli.store_keyring", lambda profile, values: stored.append((profile, values)))
    monkeypatch.setattr("josh_room.cli.store_keyring_value", lambda profile, field, value, **_kwargs: stored.append((profile, {field: value})))
    payload = {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
        "endpoint": "https://example.invalid",
        "bucket": "synthetic-bucket",
        "region": "auto",
        "age-identity": "<synthetic-age-identity>",
        "age-recipients": ["age1daily", "age1recovery"],
        "jat-root": "/synthetic/jat",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert not (tmp_path / "config.json").exists()
    assert main(["setup", "--profile", "r2-profile", "--age-profile", "age-profile", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stored"] is True
    config = private_config()
    assert config["default_backend"] == "r2"
    assert config["r2"]["credential_profile"] == "r2-profile"
    assert config["age_identity_profile"] == "age-profile"
    serialized = json.dumps(config)
    assert "synthetic-access" not in serialized
    assert "synthetic-secret" not in serialized
    assert "<synthetic-age-identity>" not in serialized
    assert stat.S_IMODE((tmp_path / "config.json").stat().st_mode) == 0o600


def test_operation_state_is_separate_from_read_only_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("JOSH_ROOM_INSTANCE", raising=False)
    assert _instance_root() == tmp_path / "state" / "josh-room"
