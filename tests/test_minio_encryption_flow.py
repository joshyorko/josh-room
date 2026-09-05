import json
import os
from pathlib import Path

import pytest

from josh_room import auth as auth_module
from josh_room.cli import build_parser
from josh_room.config import DimensionConfig
from josh_room.encryption_domain import (
    KEYSET_CONTROL_KEY,
    EncryptionKeyset,
    EncryptionMaterial,
)
from josh_room.r2 import R2Conflict

OPERATIONAL_RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"
RECOVERY_RECIPIENT = "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"
DOMAIN_ID = "00000000-0000-4000-8000-000000000001"


def dimension(provider="minio", dimension_id="archive"):
    return DimensionConfig(
        dimension_id=dimension_id,
        display_name="Archive",
        provider=provider,
        endpoint="http://127.0.0.1:9000" if provider == "minio" else "https://r2.example.invalid",
        bucket="archive-bucket",
        credential_profile="archive-profile",
        catalog_key="catalog.jroom.age",
    )


def identity(path: Path, value="AGE-SECRET-KEY-operational"):
    path.write_text(value + "\n")
    path.chmod(0o600)
    return path


class FakeBackend:
    def __init__(self, *, catalog=None, control=None):
        self.config = type("Config", (), {
            "provider": "minio",
            "endpoint": "http://127.0.0.1:9000",
            "bucket": "archive-bucket",
            "dimension_id": "archive",
        })()
        self.catalog = catalog
        self.control = control
        self.control_etag = '"control-1"' if control is not None else None
        self.calls = []
        self.conflict = False
        self.race_winner = None

    def read_control(self, key, max_bytes):
        self.calls.append(("read_control", key, max_bytes))
        assert key == KEYSET_CONTROL_KEY
        return self.control, self.control_etag

    def create_control(self, key, body):
        self.calls.append(("create_control", key, body))
        if self.conflict:
            self.control = self.race_winner.to_json()
            self.control_etag = '"control-1"'
            raise R2Conflict("control object conditional conflict")
        if self.control is not None:
            raise R2Conflict("control object conditional conflict")
        self.control = body
        self.control_etag = '"control-1"'
        return self.control_etag

    def read_catalog(self):
        self.calls.append(("read_catalog",))
        return self.catalog, '"catalog-1"' if self.catalog is not None else None


def make_keyset(identity_value="AGE-SECRET-KEY-operational", **overrides):
    values = {
        "encryption_domain_id": DOMAIN_ID,
        "provider": "minio",
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "archive-bucket",
        "key_generation": 1,
        "operational_identity": identity_value,
        "operational_recipient": OPERATIONAL_RECIPIENT,
        "recovery_recipients": (RECOVERY_RECIPIENT,),
    }
    values.update(overrides)
    return EncryptionKeyset(**values)


def test_fresh_minio_bucket_enrolls_a_keyset_without_a_catalog(tmp_path, monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr("josh_room.crypto.generate_identity", lambda path: identity(path))
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    material = auth_module.resolve_encryption_material(
        dimension(),
        backend,
        recovery_recipients=[RECOVERY_RECIPIENT],
    )

    assert material.keyset.provider == "minio"
    assert material.keyset.key_generation == 1
    assert material.keyset.encryption_domain_id != dimension().encryption_domain_id
    assert len([call for call in backend.calls if call[0] == "create_control"]) == 1
    assert "AGE-SECRET-KEY-operational" not in json.dumps({"ok": True})
    assert material.identity.is_file()
    assert material.identity.read_text().strip() == "AGE-SECRET-KEY-operational"


def test_minio_enrollment_discards_losing_identity_after_a_conditional_race(tmp_path, monkeypatch):
    winner = make_keyset(identity_value="AGE-SECRET-KEY-winner")
    backend = FakeBackend()
    backend.conflict = True
    backend.race_winner = winner
    generated = tmp_path / "loser"
    monkeypatch.setattr("josh_room.crypto.generate_identity", lambda path: identity(path, "AGE-SECRET-KEY-loser"))
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)

    material = auth_module.resolve_encryption_material(
        dimension(),
        backend,
        recovery_recipients=[RECOVERY_RECIPIENT],
        identity_path=generated,
    )

    assert material.keyset == winner
    assert material.identity.read_text().strip() == "AGE-SECRET-KEY-winner"
    assert not generated.exists()


def test_minio_catalog_without_keyset_requires_explicit_legacy_migration(tmp_path):
    backend = FakeBackend(catalog=b"encrypted-catalog")

    with pytest.raises(RuntimeError) as failure:
        auth_module.resolve_encryption_material(dimension(), backend, identity_path=tmp_path / "identity")

    assert failure.value.result == {
        "error_code": "legacy-encryption-migration-required",
        "encryption_state": "legacy",
        "dimension_id": "archive",
    }
    assert [call[0] for call in backend.calls] == ["read_control", "read_catalog"]


def test_existing_keyset_uses_domain_and_generation_scoped_cache(tmp_path, monkeypatch):
    keyset = make_keyset()
    backend = FakeBackend(control=keyset.to_json())
    cached = tmp_path / "cached"
    identity(cached)
    lookups = []
    monkeypatch.setattr("josh_room.auth.lookup_encryption_identity", lambda domain_id, generation: lookups.append((domain_id, generation)) or cached.read_text().strip())
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)

    material = auth_module.resolve_encryption_material(dimension(), backend, identity_path=tmp_path / "handoff")

    assert material.encryption_domain_id == DOMAIN_ID
    assert material.key_generation == 1
    assert lookups == [(DOMAIN_ID, 1)]
    assert [call[0] for call in backend.calls] == ["read_control"]


def test_r2_resolution_keeps_legacy_cloudflare_identity_path(monkeypatch, tmp_path):
    backend = FakeBackend()
    backend.config.provider = "r2"
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identity(tmp_path / "r2-identity")))

    assert auth_module.resolve_encryption_material(dimension(provider="r2"), backend) is None
    assert backend.calls == []


def test_configured_minio_command_uses_private_handoff_without_cloudflare_session(tmp_path, monkeypatch, capsys):
    from josh_room import cli

    config = {
        "default_dimension": "archive",
        "dimensions": {"archive": dimension().to_private()},
    }
    backend = FakeBackend()
    handoff = identity(tmp_path / "handoff")
    keyset = make_keyset()
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr(cli, "private_config", lambda: config)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "load_runtime_session", lambda: pytest.fail("MinIO must not load Cloudflare runtime"))
    monkeypatch.setattr(cli, "ensure_runtime_session", lambda **_kwargs: pytest.fail("MinIO must not request Cloudflare"))
    monkeypatch.setattr(cli, "resolve_encryption_material", lambda *_args, **_kwargs: EncryptionMaterial(keyset, handoff))
    captured = {}
    monkeypatch.setattr(
        cli,
        "dispatch",
        lambda _args, _instance: captured.update({
            "identity": os.environ.get("JOSH_ROOM_IDENTITY"),
            "material": os.environ.get("JOSH_ROOM_ENCRYPTION_MATERIAL"),
            "recipients": os.environ.get("JOSH_ROOM_SELECTED_RECIPIENTS"),
        }) or {"ok": True},
    )

    assert cli.main(["projects", "list", "--backend", "minio", "--dimension", "archive", "--json"]) == 0
    output = capsys.readouterr().out
    assert captured == {
        "identity": str(handoff),
        "material": str(handoff),
        "recipients": f"{OPERATIONAL_RECIPIENT},{RECOVERY_RECIPIENT}",
    }
    assert "AGE-SECRET-KEY" not in output


@pytest.mark.parametrize("argv", [
    ["encryption", "status", "--dimension", "archive", "--json"],
    ["encryption", "initialize", "--dimension", "archive", "--recovery-recipient", RECOVERY_RECIPIENT, "--json"],
    ["encryption", "migrate", "--dimension", "archive", "--json"],
    ["encryption", "resume", "--dimension", "archive", "--json"],
])
def test_encryption_actions_have_explicit_parser_contract(argv):
    args = build_parser().parse_args(argv)

    assert args.command == "encryption"
    assert args.encryption_command in {"status", "initialize", "migrate", "resume"}
