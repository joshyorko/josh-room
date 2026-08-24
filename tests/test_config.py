import io
import json
import sys

from josh_room.cli import _instance_root, main
from josh_room.config import auth_status, private_config


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


def test_operation_state_is_separate_from_read_only_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("JOSH_ROOM_INSTANCE", raising=False)
    assert _instance_root() == tmp_path / "state" / "josh-room"
