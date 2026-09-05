import json
import os
from pathlib import Path

import pytest

from josh_room import auth as auth_module
from josh_room import cli
from josh_room.catalog import Catalog
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


def dimension(provider="minio", dimension_id="archive", **overrides):
    values = {
        "dimension_id": dimension_id,
        "display_name": "Archive",
        "provider": provider,
        "endpoint": "http://127.0.0.1:9000" if provider == "minio" else "https://r2.example.invalid",
        "bucket": "archive-bucket",
        "credential_profile": "archive-profile",
        "catalog_key": "catalog.jroom.age",
    }
    values.update(overrides)
    return DimensionConfig(
        **values,
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


def test_dimensions_hierarchy_resolves_minio_before_reading_a_legacy_catalog(tmp_path, monkeypatch, capsys):
    config = {"dimensions": {"archive": dimension().to_private()}}
    backend = FakeBackend(catalog=b"legacy-catalog")
    monkeypatch.setattr(cli, "private_config", lambda: config)
    monkeypatch.setattr(cli, "_backend", lambda *_args: backend)
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)

    assert cli.main(["dimensions", "list", "--dimension", "archive", "--with-hierarchy", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)

    assert result["error_code"] == "legacy-encryption-migration-required"


@pytest.mark.parametrize(
    ("backend", "expected_state", "expected_code"),
    [
        (FakeBackend(catalog=b"legacy-catalog"), "legacy", "legacy-encryption-migration-required"),
        (FakeBackend(), "uninitialized", "encryption-initialization-required"),
        (FakeBackend(control=make_keyset().to_json()), "failed", "encryption-domain-mismatch"),
    ],
)
def test_doctor_surfaces_minio_encryption_state_before_catalog_decrypt(
    backend, expected_state, expected_code, tmp_path, monkeypatch
):
    configured = "00000000-0000-4000-8000-000000000002"
    selected = dimension(encryption_domain_id=configured) if expected_state == "failed" else dimension()
    config = {"dimensions": {"archive": selected.to_private()}}
    global_identity = identity(tmp_path / "global-identity")
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(global_identity))
    monkeypatch.setattr(cli, "private_config", lambda: config)
    monkeypatch.setattr(cli, "_backend", lambda *_args: backend)
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda _name: "/synthetic/tool")
    monkeypatch.setattr(cli, "_tar_capable", lambda: True)
    monkeypatch.setattr(cli, "_jat_contract", lambda _root: {"robot": True, "tasks": True, "interactive": True})
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: pytest.fail("doctor must resolve encryption before decrypting"))

    report = cli._doctor(tmp_path, "minio", "terminal", dimension="archive")

    assert report["encryption_state"] == expected_state
    assert report["error_code"] == expected_code


def test_minio_copy_routes_without_cloudflare(tmp_path, monkeypatch, capsys):
    config = {
        "dimensions": {
            "archive": dimension().to_private(),
            "cloud": dimension(provider="r2", dimension_id="cloud", bucket="cloud-bucket").to_private(),
        },
    }
    events = []
    monkeypatch.setattr(cli, "private_config", lambda: config)
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "ensure_runtime_session", lambda **_kwargs: events.append("cloudflare"))
    backend_calls = []
    monkeypatch.setattr(cli, "_backend", lambda *args: backend_calls.append(args) or object())
    monkeypatch.setattr(cli, "_identity", lambda: tmp_path / "identity")
    monkeypatch.setattr(cli, "resolve_encryption_material", lambda *_args, **_kwargs: type(
        "Material", (), {
            "identity": tmp_path / "identity",
            "encryption_domain_id": "11111111-1111-4111-8111-111111111111",
            "key_generation": 1,
            "recipient": "recipient",
            "keyset": type("Keyset", (), {"recovery_recipients": ("recovery",)})(),
        }
    )())
    monkeypatch.setattr(cli, "_read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty("dimension"), "catalog-1"))
    monkeypatch.setattr(cli, "copy_snapshot_stream", lambda *_args, **_kwargs: {"ok": True})

    assert cli.main([
        "snapshot", "copy", "demo", "--source-dimension", "archive",
        "--destination-dimension", "cloud", "--destination-room", "copied", "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is True
    assert [call[0] for call in backend_calls] == ["minio", "r2"]
    assert events == []


def test_marker_derived_operation_selects_the_marker_dimension(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from josh_room.workspace_state import workspace_fingerprint, write_workspace_marker

    write_workspace_marker(
        workspace,
        dimension_id="archive",
        project_id="demo",
        display_name="Demo",
        snapshot_id="snapshot-one",
        workspace_fingerprint=workspace_fingerprint(workspace),
    )
    config = {
        "default_dimension": "cloud",
        "dimensions": {
            "archive": dimension().to_private(),
            "cloud": dimension(provider="r2", dimension_id="cloud", bucket="cloud-bucket").to_private(),
        },
    }
    monkeypatch.setattr(cli, "private_config", lambda: config)
    args = build_parser().parse_args(["link", "--workspace", str(workspace), "--project", "demo", "--snapshot", "snapshot-one"])

    assert cli._effective_dimension(args).dimension_id == "archive"


def test_ensure_minio_domain_rejects_configured_domain_mismatch(tmp_path, monkeypatch):
    configured = "00000000-0000-4000-8000-000000000002"
    backend = FakeBackend(control=make_keyset().to_json())

    with pytest.raises(RuntimeError) as failure:
        auth_module.ensure_minio_domain(
            dimension(encryption_domain_id=configured),
            backend,
            recovery_recipients=[RECOVERY_RECIPIENT],
            identity_path=tmp_path / "handoff",
        )

    assert failure.value.result["error_code"] == "encryption-domain-mismatch"


def test_ensure_minio_domain_rejects_mismatched_conditional_race_winner(tmp_path, monkeypatch):
    configured = "00000000-0000-4000-8000-000000000002"
    winner = make_keyset()
    backend = FakeBackend()
    backend.conflict = True
    backend.race_winner = winner
    monkeypatch.setattr("josh_room.crypto.generate_identity", lambda path: identity(path, "AGE-SECRET-KEY-loser"))
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)

    candidate = tmp_path / "loser"
    with pytest.raises(RuntimeError) as failure:
        auth_module.ensure_minio_domain(
            dimension(encryption_domain_id=configured),
            backend,
            recovery_recipients=[RECOVERY_RECIPIENT],
            identity_path=candidate,
        )

    assert failure.value.result["error_code"] == "encryption-domain-mismatch"
    assert not candidate.exists()


def test_mismatched_race_winner_preserves_preexisting_caller_identity_path(tmp_path, monkeypatch):
    winner = make_keyset()
    backend = FakeBackend()
    backend.conflict = True
    backend.race_winner = winner
    candidate = identity(tmp_path / "caller", "AGE-SECRET-KEY-caller")
    monkeypatch.setattr("josh_room.crypto.generate_identity", lambda _path: None)
    monkeypatch.setattr("josh_room.crypto.derive_recipient", lambda _path: OPERATIONAL_RECIPIENT)
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)

    material = auth_module.ensure_minio_domain(
        dimension(),
        backend,
        recovery_recipients=[RECOVERY_RECIPIENT],
        identity_path=candidate,
    )

    assert material.keyset == winner
    assert candidate.read_text() == "AGE-SECRET-KEY-caller\n"


def test_recovery_handoff_derives_public_recipient_without_remote_recovery_private_identity(tmp_path, monkeypatch):
    recovery = identity(tmp_path / "recovery", "AGE-SECRET-KEY-recovery")
    operational = tmp_path / "operational"
    backend = FakeBackend()
    monkeypatch.setattr("josh_room.crypto.generate_identity", lambda path: identity(path, "AGE-SECRET-KEY-operational"))
    monkeypatch.setattr(
        "josh_room.crypto.derive_recipient",
        lambda path: RECOVERY_RECIPIENT if Path(path) == recovery else OPERATIONAL_RECIPIENT,
    )
    monkeypatch.setattr("josh_room.auth.store_encryption_identity", lambda *_args: None)

    material = auth_module.ensure_minio_domain(
        dimension(),
        backend,
        recovery_handoff=recovery,
        identity_path=operational,
    )
    keyset_body = json.loads(backend.control)

    assert material.identity == operational
    assert keyset_body["recovery_recipients"] == [RECOVERY_RECIPIENT]
    assert "AGE-SECRET-KEY-recovery" not in backend.control.decode()
    assert recovery.read_text() == "AGE-SECRET-KEY-recovery\n"
    assert operational.read_text() == "AGE-SECRET-KEY-operational\n"
    args = build_parser().parse_args([
        "encryption", "initialize", "--dimension", "archive",
        "--recovery-handoff", str(recovery), "--json",
    ])
    assert args.recovery_handoff == recovery


def test_failed_identity_generation_removes_private_candidate(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate"

    def fail_after_writing(path):
        identity(path)
        raise RuntimeError("synthetic key generation failure")

    monkeypatch.setattr("josh_room.crypto.generate_identity", fail_after_writing)
    with pytest.raises(RuntimeError, match="synthetic key generation failure"):
        auth_module.ensure_minio_domain(
            dimension(),
            FakeBackend(),
            recovery_recipients=[RECOVERY_RECIPIENT],
            identity_path=candidate,
        )

    assert not candidate.exists()


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
