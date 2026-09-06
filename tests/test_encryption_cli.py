"""Migration CLI prerequisites and cutover, using only disposable synthetic data."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_encryption_migration import FakeStore, _envelope, _snapshot

from josh_room import auth, cli, crypto
from josh_room.catalog import Catalog
from josh_room.config import DimensionConfig
from josh_room.encryption_domain import KEYSET_CONTROL_KEY, MIGRATION_JOURNAL_KEY


@pytest.fixture
def migration(tmp_path, monkeypatch):
    if not all(shutil.which(tool) for tool in ("age", "age-keygen")):
        pytest.skip("real age and age-keygen tooling is unavailable")
    for name in tuple(os.environ):
        if name.startswith("JOSH_ROOM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "_instance_root", lambda: tmp_path / "instance")
    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "_configured", dict)
    monkeypatch.setattr(cli, "lookup_keyring_value", lambda *_args: None)
    monkeypatch.setattr(auth, "store_encryption_identity", lambda *_args: None)

    def unavailable(*_args):
        raise RuntimeError("synthetic keyring is empty")

    monkeypatch.setattr(auth, "lookup_encryption_identity", unavailable)
    dimension = DimensionConfig(
        dimension_id="archive", display_name="Synthetic Archive", provider="minio",
        endpoint="http://127.0.0.1:9000", bucket="synthetic-bucket",
        credential_profile="synthetic-profile", catalog_key="catalog.jroom.age",
    )
    monkeypatch.setattr(cli, "DimensionRegistry", lambda _config: SimpleNamespace(select=lambda _id: dimension))
    source = crypto.generate_identity(tmp_path / "source.identity")
    recovery = crypto.generate_identity(tmp_path / "recovery.identity")
    source_recipient = crypto.derive_recipient(source)
    old_recovery = crypto.generate_identity(tmp_path / "old-recovery.identity")
    old_recipients = [source_recipient, crypto.derive_recipient(old_recovery)]
    envelope = _envelope(tmp_path)
    encrypted = tmp_path / "source.age"
    crypto.encrypt(envelope, old_recipients, encrypted)
    ciphertext = encrypted.read_bytes()
    snapshot = _snapshot("one", ciphertext)
    catalog = Catalog.empty(dimension_id="archive").add_snapshot("migration-room", "Synthetic Room", snapshot)
    backend = FakeStore({snapshot["object_key"]: ciphertext})
    backend.config = dimension
    crypto.encrypt(json.dumps(catalog.body).encode(), old_recipients, encrypted)
    backend.catalog_body = encrypted.read_bytes()
    monkeypatch.setattr(cli, "_backend", lambda *_args: backend)
    return SimpleNamespace(
        source=source, recovery=recovery, backend=backend, catalog=catalog,
        envelope=envelope, dimension=dimension, instance=tmp_path / "instance",
    )


def invoke(action, migration, capsys, *, source=True, recovery=True, extra=()):
    argv = ["encryption", action, "--dimension", "archive", "--json"]
    if source:
        argv.extend(["--source-identity", str(migration.source)])
    if recovery:
        argv.extend(["--recovery-handoff", str(migration.recovery)])
    code = cli.main([*argv, *extra])
    output = capsys.readouterr().out
    assert "AGE-SECRET-KEY-" not in output
    assert str(migration.source) not in output
    assert str(migration.recovery) not in output
    return code, json.loads(output)


@pytest.mark.parametrize("action", ["migrate", "resume"])
@pytest.mark.parametrize("kind", ["missing", "absent-file", "unsafe", "invalid", "wrong-key"])
def test_source_prerequisite_errors_never_publish_a_keyset(action, kind, migration, capsys):
    if kind == "absent-file":
        migration.source.unlink()
    elif kind == "unsafe":
        migration.source.chmod(0o644)
    elif kind == "invalid":
        migration.source.write_text("invalid synthetic identity")
    elif kind == "wrong-key":
        migration.source = migration.recovery
    original_catalog = migration.backend.catalog_body

    code, result = invoke(action, migration, capsys, source=kind != "missing")

    assert code == 2
    assert migration.backend.controls == {}
    assert migration.backend.catalog_body == original_catalog
    assert migration.backend.catalog_puts == 0
    assert migration.backend.puts == []
    assert result["error_code"] == (
        "legacy-source-identity-required" if kind == "missing" else "legacy-source-identity-invalid"
    )
    assert result["encryption_state"] == "legacy"


@pytest.mark.parametrize("action", ["migrate", "resume"])
@pytest.mark.parametrize("kind", ["missing", "absent-file", "unsafe", "invalid", "ambiguous"])
def test_recovery_prerequisite_errors_are_structured_and_read_only(action, kind, migration, capsys):
    extra = ()
    if kind == "absent-file":
        migration.recovery.unlink()
    elif kind == "unsafe":
        migration.recovery.chmod(0o644)
    elif kind == "invalid":
        migration.recovery.write_text("invalid synthetic recovery identity")
    elif kind == "ambiguous":
        extra = ("--recovery-recipient", crypto.derive_recipient(migration.recovery))

    code, result = invoke(action, migration, capsys, recovery=kind != "missing", extra=extra)

    assert code == 2
    assert migration.backend.controls == {}
    assert migration.backend.catalog_puts == 0
    assert migration.backend.puts == []
    assert result["error_code"] == (
        "encryption-initialization-required" if kind == "missing" else "encryption-recovery-invalid"
    )


@pytest.mark.parametrize("action", ["migrate", "resume"])
def test_missing_catalog_is_rejected_before_enrollment(action, migration, capsys):
    migration.backend.catalog_body = None
    migration.backend.catalog_etag = None

    code, result = invoke(action, migration, capsys)

    assert code == 2
    assert migration.backend.controls == {}
    assert migration.backend.catalog_puts == 0
    assert result["error_code"] == "legacy-catalog-missing"


def test_preview_is_repeatable_and_execution_accepts_separate_handoffs(migration, capsys, monkeypatch):
    # A supplied recovery handoff must take precedence over configured fallback recipients.
    monkeypatch.setenv("JOSH_ROOM_RECIPIENTS", crypto.derive_recipient(migration.source))
    original_catalog = migration.backend.catalog_body
    for _ in range(2):
        code, plan = invoke("migrate", migration, capsys)
        assert code == 0
        assert plan["read_only"] is True
        assert plan["requires_confirmation"] is True
        assert plan["object_count"] == 1
        assert plan["snapshot_count"] == 1
        assert plan["destination_encryption_domain_id"] is None
        assert plan["journal_exists"] is False
        assert migration.backend.controls == {}
        assert migration.backend.catalog_body == original_catalog
        assert migration.backend.puts == []

    code, result = invoke(
        "resume", migration, capsys,
        extra=("--expected-catalog-etag", plan["source_catalog_etag"]),
    )

    assert code == 0
    assert result["status"] == "committed"
    assert migration.backend.catalog_puts == 1
    assert len(migration.backend.puts) == 1
    keyset = json.loads(migration.backend.controls[KEYSET_CONTROL_KEY][0])
    assert keyset["recovery_recipients"] == [crypto.derive_recipient(migration.recovery)]
    assert migration.recovery.read_text().strip() not in migration.backend.controls[KEYSET_CONTROL_KEY][0].decode()
    copied = migration.source.parent / "copied.age"
    copied.write_bytes(migration.backend.puts[0][1])
    assert crypto.decrypt(copied, [migration.recovery]) == migration.envelope
    assert migration.source.is_file() and migration.recovery.is_file()
    assert not list(migration.instance.glob(".josh-room-encryption-*"))


@pytest.mark.parametrize("status", ["ready-to-commit", "cutover-published"])
def test_published_cutover_reconciles_without_legacy_or_recovery_handoff(status, migration, capsys):
    code, _result = invoke("resume", migration, capsys)
    assert code == 0
    body, etag = migration.backend.controls[MIGRATION_JOURNAL_KEY]
    journal = json.loads(body)
    journal["status"] = status
    migration.backend.controls[MIGRATION_JOURNAL_KEY] = (json.dumps(journal).encode(), etag)
    migration.source.unlink()
    migration.recovery.unlink()

    code, result = invoke("resume", migration, capsys, source=False, recovery=False)

    assert code == 0
    assert result["status"] == "committed"
    assert migration.backend.catalog_puts == 1
    assert len(migration.backend.puts) == 1
    assert json.loads(migration.backend.controls[MIGRATION_JOURNAL_KEY][0])["status"] == "committed"


@pytest.mark.parametrize("status", ["interrupted", "ready-to-commit"])
def test_unpublished_migration_missing_source_leaves_existing_journal_unchanged(status, migration, capsys):
    migration.backend.fail_put_after = 0
    with pytest.raises(KeyboardInterrupt):
        invoke("resume", migration, capsys)
    capsys.readouterr()
    body, etag = migration.backend.controls[MIGRATION_JOURNAL_KEY]
    journal = json.loads(body)
    journal["status"] = status
    migration.backend.controls[MIGRATION_JOURNAL_KEY] = (json.dumps(journal).encode(), etag)
    original_controls = dict(migration.backend.controls)

    code, result = invoke("resume", migration, capsys, source=False, recovery=False)

    assert code == 2
    assert result["error_code"] == "legacy-source-identity-required"
    assert migration.backend.controls == original_controls
    assert migration.backend.catalog_puts == 0

    if status == "interrupted":
        code, plan = invoke("migrate", migration, capsys, recovery=False)
        assert code == 0
        assert plan["read_only"] is True
        assert plan["requires_confirmation"] is True
        assert plan["journal_status"] == "interrupted"
        assert plan["migration_id"] == journal["migration_id"]
        assert migration.backend.controls == original_controls
        migration.backend.fail_put_after = None
        code, result = invoke("resume", migration, capsys, recovery=False)
        assert code == 0
        assert result["status"] == "committed"


def test_existing_keyset_preview_is_read_only_and_needs_no_new_recovery(migration, capsys):
    material = auth.ensure_minio_domain(
        migration.dimension, migration.backend,
        recovery_handoff=migration.recovery, allow_legacy_migration=True,
    )
    Path(material.identity).unlink()
    original_controls = dict(migration.backend.controls)

    code, result = invoke("migrate", migration, capsys, recovery=False)

    assert code == 0
    assert result["destination_encryption_domain_id"] == material.encryption_domain_id
    assert migration.backend.controls == original_controls
    assert migration.backend.catalog_puts == 0


def test_source_identity_accepts_standard_age_keygen_comments(migration, capsys):
    migration.source.write_text("# created: synthetic fixture\n# public key: synthetic\n" + migration.source.read_text())

    code, result = invoke("migrate", migration, capsys)

    assert code == 0
    assert result["read_only"] is True
    assert migration.backend.controls == {}


@pytest.mark.parametrize("action", ["migrate", "resume"])
def test_invalid_public_recovery_recipient_is_structured_before_enrollment(action, migration, capsys):
    code, result = invoke(action, migration, capsys, recovery=False, extra=("--recovery-recipient", "age1invalid"))

    assert code == 2
    assert result["error_code"] == "encryption-recovery-invalid"
    assert migration.backend.controls == {}


@pytest.mark.parametrize("action", ["migrate", "resume"])
def test_committed_migration_is_a_verified_read_only_noop(action, migration, capsys):
    code, _result = invoke("resume", migration, capsys)
    assert code == 0
    original_controls = dict(migration.backend.controls)
    migration.source.unlink()
    migration.recovery.unlink()

    code, result = invoke(action, migration, capsys, source=False, recovery=False)

    assert code == 0
    assert result["status"] == "committed"
    assert result["read_only"] is True
    assert result["requires_confirmation"] is False
    assert migration.backend.controls == original_controls
    assert migration.backend.catalog_puts == 1
    assert len(migration.backend.puts) == 1


def test_confirmed_resume_retries_unpublished_cutover_with_verified_objects(migration, capsys):
    original_catalog = migration.backend.catalog_body
    original_etag = migration.backend.catalog_etag
    code, _result = invoke("resume", migration, capsys)
    assert code == 0
    migration.backend.catalog_body = original_catalog
    migration.backend.catalog_etag = original_etag
    body, etag = migration.backend.controls[MIGRATION_JOURNAL_KEY]
    journal = json.loads(body)
    journal["status"] = "ready-to-commit"
    migration.backend.controls[MIGRATION_JOURNAL_KEY] = (json.dumps(journal).encode(), etag)
    original_controls = dict(migration.backend.controls)

    code, plan = invoke("migrate", migration, capsys, recovery=False)
    assert code == 0
    assert plan["journal_status"] == "ready-to-commit"
    assert plan["journal_exists"] is True
    assert migration.backend.controls == original_controls

    code, result = invoke("resume", migration, capsys, recovery=False)
    assert code == 0
    assert result["status"] == "committed"
    assert len(migration.backend.puts) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "conflict"])
def test_confirmed_execution_replans_a_terminal_journal(status, migration, capsys):
    migration.backend.fail_put_after = 0
    with pytest.raises(KeyboardInterrupt):
        invoke("resume", migration, capsys)
    capsys.readouterr()
    body, etag = migration.backend.controls[MIGRATION_JOURNAL_KEY]
    journal = json.loads(body)
    journal["status"] = status
    migration.backend.controls[MIGRATION_JOURNAL_KEY] = (json.dumps(journal).encode(), etag)
    original_controls = dict(migration.backend.controls)

    code, plan = invoke("migrate", migration, capsys, recovery=False)
    assert code == 0
    assert plan["read_only"] is True
    assert plan["journal_exists"] is True
    assert migration.backend.controls == original_controls

    migration.backend.fail_put_after = None
    code, result = invoke("resume", migration, capsys, recovery=False)
    assert code == 0
    assert result["status"] == "committed"


@pytest.mark.parametrize("action", ["migrate", "resume"])
def test_expected_catalog_etag_rejects_new_unconfirmed_objects_before_enrollment(action, migration, capsys):
    code, plan = invoke("migrate", migration, capsys)
    assert code == 0
    encrypted = migration.source.parent / "unconfirmed.age"
    recipients = [crypto.derive_recipient(migration.source), crypto.derive_recipient(migration.recovery)]
    crypto.encrypt(_envelope(migration.source.parent, snapshot_id="unconfirmed", payload=b"new synthetic JAT"), recipients, encrypted)
    ciphertext = encrypted.read_bytes()
    snapshot = _snapshot("unconfirmed", ciphertext)
    catalog = migration.catalog.add_snapshot("migration-room", "Synthetic Room", snapshot)
    migration.backend.objects[snapshot["object_key"]] = ciphertext
    crypto.encrypt(json.dumps(catalog.body).encode(), recipients, encrypted)
    migration.backend.catalog_body = encrypted.read_bytes()
    migration.backend.catalog_etag = '"catalog-unconfirmed"'

    code, result = invoke(action, migration, capsys, extra=("--expected-catalog-etag", plan["source_catalog_etag"]))

    assert code == 2
    assert result["error_code"] == "encryption-migration-conflict"
    assert result["encryption_state"] == "conflict"
    assert migration.backend.controls == {}
    assert migration.backend.catalog_puts == 0
    assert migration.backend.puts == []


@pytest.mark.parametrize("action", ["migrate", "resume"])
def test_insufficient_temporary_disk_is_rejected_before_enrollment(action, migration, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))

    code, result = invoke(action, migration, capsys)

    assert code == 2
    assert result["error_code"] == "encryption-migration-insufficient-disk"
    assert migration.backend.controls == {}
    assert migration.backend.catalog_puts == 0
    assert migration.backend.puts == []


def test_expected_catalog_etag_is_rechecked_after_source_catalog_read(migration, capsys):
    expected = migration.backend.catalog_etag
    read = migration.backend.read_catalog
    reads = 0

    def changing_catalog():
        nonlocal reads
        reads += 1
        if reads > 1:
            migration.backend.catalog_etag = '"catalog-changed-during-preflight"'
        return read()

    migration.backend.read_catalog = changing_catalog
    code, result = invoke("resume", migration, capsys, extra=("--expected-catalog-etag", expected))

    assert code == 2
    assert result["error_code"] == "encryption-migration-conflict"
    assert migration.backend.controls == {}
    assert migration.backend.puts == []
