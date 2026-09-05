import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import operations
from josh_room.catalog import Catalog, CatalogConflict
from josh_room.envelope import build_envelope_file
from josh_room.local_store import ObjectRef

SOURCE_DOMAIN = "11111111-1111-4111-8111-111111111111"
DESTINATION_DOMAIN = "22222222-2222-4222-8222-222222222222"


def _material(domain, generation=1):
    return SimpleNamespace(
        encryption_domain_id=domain,
        key_generation=generation,
        identity=Path("/synthetic/identity"),
        recipient="destination-recipient",
        keyset=SimpleNamespace(recovery_recipients=("destination-recovery",)),
    )


def _envelope(tmp_path, snapshot_id="legacy-snapshot", payload=b"JAT"):
    manifest = json.loads(
        (Path(__file__).parent / "fixtures" / "encryption_migration_manifest.json").read_text()
    )
    manifest["snapshot_id"] = snapshot_id
    manifest["payload"]["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["payload"]["size"] = len(payload)
    payload_path = tmp_path / f"{snapshot_id}.payload"
    envelope_path = tmp_path / f"{snapshot_id}.jroom"
    payload_path.write_bytes(payload)
    build_envelope_file(manifest, payload_path, envelope_path)
    return envelope_path.read_bytes()


def _snapshot(snapshot_id, ciphertext):
    digest = hashlib.sha256(ciphertext).hexdigest()
    return {
        "snapshot_id": snapshot_id,
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": len(ciphertext),
        "created_at": "2026-09-04T00:00:00+00:00",
        "workspace_fingerprint": "a" * 64,
        "origin_project_id": "migration-room",
        "jat_metadata": {"marker": "preserve-me"},
    }


class FakeStore:
    def __init__(self, objects=None, domain=None, generation=1):
        self.objects = dict(objects or {})
        self.domain = domain
        self.generation = generation
        self.controls = {}
        self.catalog_body = None
        self.catalog_etag = "catalog-1"
        self.downloads = []
        self.puts = []
        self.catalog_puts = 0
        self.fail_put_after = None
        self.fail_catalog = None

    def download_file(self, key, destination, digest, size):
        self.downloads.append(key)
        body = self.objects[key]
        assert hashlib.sha256(body).hexdigest() == digest
        assert len(body) == size
        destination.write_bytes(body)

    def put_file(self, key, path):
        body = path.read_bytes()
        if self.fail_put_after is not None and len(self.puts) >= self.fail_put_after:
            raise KeyboardInterrupt("synthetic interruption")
        self.puts.append((key, body))
        self.objects.setdefault(key, body)
        return ObjectRef(key, hashlib.sha256(body).hexdigest(), len(body))

    def verify_object(self, key, digest, size):
        body = self.objects[key]
        assert hashlib.sha256(body).hexdigest() == digest
        assert len(body) == size
        return ObjectRef(key, digest, size)

    def read_control(self, key, _max_bytes):
        value = self.controls.get(key)
        return (None, None) if value is None else value

    def create_control(self, key, body):
        if key in self.controls:
            raise CatalogConflict("control exists")
        self.controls[key] = (body, f"control-{len(self.controls) + 1}")
        return self.controls[key][1]

    def replace_control(self, key, body, expected_etag):
        current = self.controls[key]
        if current[1] != expected_etag:
            raise CatalogConflict("control conflict")
        self.controls[key] = (body, f"control-{len(self.controls) + 1}")
        return self.controls[key][1]

    def conditional_catalog_put(self, body, expected_etag):
        self.catalog_puts += 1
        if self.fail_catalog:
            raise self.fail_catalog
        if expected_etag != self.catalog_etag:
            raise CatalogConflict("catalog conflict")
        self.catalog_body = body
        self.catalog_etag = f"catalog-{self.catalog_puts + 1}"
        return self.catalog_etag

    def read_catalog(self):
        return self.catalog_body, self.catalog_etag

    def record_orphan(self, _ref):
        return None


def _catalog(snapshots, dimension="legacy"):
    catalog = Catalog.empty(dimension_id=dimension, encryption_domain_id=SOURCE_DOMAIN)
    for snapshot in snapshots:
        catalog = catalog.add_snapshot("migration-room", "Migration Room", snapshot)
    return catalog


def test_plan_deduplicates_shared_ciphertext_and_exposes_only_safe_metadata(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot, {**snapshot, "snapshot_id": "two"}])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)

    plan = operations.plan_encryption_migration(
        source_catalog,
        source,
        destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_domain_id=SOURCE_DOMAIN,
        destination_material=_material(DESTINATION_DOMAIN, 3),
        source_catalog_revision=source_catalog.body["revision"],
    )

    assert plan["object_count"] == 1
    assert plan["snapshot_count"] == 2
    assert plan["total_bytes"] == len(b"old:" + envelope)
    assert plan["largest_object_bytes"] == len(b"old:" + envelope)
    assert plan["temporary_disk_bytes"] >= len(b"old:" + envelope)
    assert plan["transport_state"] == "unknown"
    assert plan["journal_status"] == "planned"
    assert plan["source_catalog_revision"] == source_catalog.body["revision"]
    assert set(plan) >= {"migration_id", "status", "mappings", "source_dimension", "destination_dimension"}
    assert "AGE-SECRET-KEY" not in json.dumps(plan)
    assert str(tmp_path) not in json.dumps(plan)


def test_migration_preserves_exact_envelope_and_never_calls_jat(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path, payload=b"JAT")
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    destination.catalog_body = b"old-catalog"
    destination.catalog_etag = "catalog-1"
    calls = []
    monkeypatch.setattr("josh_room.operations.run_build", lambda *a, **k: calls.append("build"))
    monkeypatch.setattr("josh_room.operations.run_restore", lambda *a, **k: calls.append("restore"))

    def decrypt_old(_source, _identities, output):
        output.write_bytes(source.objects[snapshot["object_key"]][4:])

    def encrypt_new(source_path, _recipients, output):
        output.write_bytes(b"new:" + source_path.read_bytes())

    monkeypatch.setattr("josh_room.operations.decrypt_file", decrypt_old)
    monkeypatch.setattr("josh_room.operations.encrypt_file", encrypt_new)
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    result = operations.migrate_encryption(
        tmp_path / "instance",
        source_catalog,
        destination_catalog=None,
        source_backend=source,
        destination_backend=destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_identity=tmp_path / "legacy.identity",
        destination_material=_material(DESTINATION_DOMAIN),
        source_catalog_etag="catalog-1",
        destination_catalog_etag="catalog-1",
    )

    assert calls == []
    assert source.downloads == [snapshot["object_key"]]
    assert len(destination.puts) == 1
    assert destination.puts[0][1] == b"new:" + envelope
    copied = result["catalog"].resolve_snapshot("migration-room", "one")
    assert copied["jat_metadata"] == snapshot["jat_metadata"]
    assert copied["ciphertext_size"] == len(b"new:" + envelope)


def test_migration_rejects_ciphertext_corruption_before_decryption(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])

    class CorruptingStore(FakeStore):
        def download_file(self, key, destination, _digest, _size):
            self.downloads.append(key)
            destination.write_bytes(b"corrupt")

    source = CorruptingStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda *_args: pytest.fail("corrupt ciphertext was decrypted"))

    with pytest.raises(ValueError, match="source ciphertext metadata"):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
        )


def test_migration_resume_uses_journal_mapping_and_retains_old_objects(tmp_path, monkeypatch):
    first = _envelope(tmp_path, "one", b"ONE")
    second = _envelope(tmp_path, "two", b"TWO")
    first_snapshot = _snapshot("one", b"old:" + first)
    second_snapshot = _snapshot("two", b"old:" + second)
    source_catalog = _catalog([first_snapshot, second_snapshot])
    source = FakeStore({first_snapshot["object_key"]: b"old:" + first, second_snapshot["object_key"]: b"old:" + second})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    destination.fail_put_after = 1
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda source_path, _ids, output: output.write_bytes(source_path.read_bytes()[4:]))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda source_path, _recipients, output: output.write_bytes(b"new:" + source_path.read_bytes()))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")

    with pytest.raises(KeyboardInterrupt):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            source_catalog_etag="catalog-1",
            destination_catalog_etag="catalog-1",
        )
    journal_body, _etag = destination.controls["control/migration-journal.v1.json"]
    journal = json.loads(journal_body)
    assert sum(item["status"] == "verified" for item in journal["mappings"]) == 1
    assert journal["status"] == "interrupted"
    destination.fail_put_after = None
    result = operations.migrate_encryption(
        tmp_path / "instance",
        source_catalog,
        source_backend=source,
        destination_backend=destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_identity=tmp_path / "legacy.identity",
        destination_material=_material(DESTINATION_DOMAIN),
        source_catalog_etag="catalog-1",
        destination_catalog_etag="catalog-1",
        resume=True,
    )
    assert len(destination.puts) == 2
    assert first_snapshot["object_key"] in source.objects
    assert second_snapshot["object_key"] in source.objects
    assert result["status"] == "committed"


def test_migration_catalog_conflict_leaves_old_catalog_authoritative_and_objects_retained(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    destination.fail_catalog = CatalogConflict("catalog conflict")
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda source_path, _ids, output: output.write_bytes(source_path.read_bytes()[4:]))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda source_path, _recipients, output: output.write_bytes(b"new:" + source_path.read_bytes()))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")

    with pytest.raises(CatalogConflict):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            source_catalog_etag="catalog-1",
            destination_catalog_etag="catalog-1",
        )
    assert destination.catalog_body is None
    assert destination.puts
    source_snapshot_key = snapshot["object_key"]
    assert source_snapshot_key in source.objects


def test_migration_resume_reconciles_crash_after_catalog_cutover(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    destination.fail_catalog = RuntimeError("crash after cutover")
    destination.fail_catalog.published = True
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda source_path, _ids, output: output.write_bytes(source_path.read_bytes()[4:]))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda source_path, _recipients, output: output.write_bytes(b"new:" + source_path.read_bytes()))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")

    original_put = destination.conditional_catalog_put

    def publish_then_crash(body, expected_etag):
        destination.fail_catalog = None
        original_put(body, expected_etag)
        error = RuntimeError("crash after cutover")
        error.published = True
        raise error

    destination.conditional_catalog_put = publish_then_crash
    with pytest.raises(RuntimeError, match="crash after cutover"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            source_catalog_etag="catalog-1", destination_catalog_etag="catalog-1",
        )
    journal_body, _journal_etag = destination.controls["control/migration-journal.v1.json"]
    journal = json.loads(journal_body)
    candidate = operations._migration_catalog(source_catalog, None, journal["mappings"], "minio", DESTINATION_DOMAIN)
    monkeypatch.setattr(operations, "_read_remote_catalog", lambda *_args, **_kwargs: (candidate, "catalog-2"))
    result = operations.migrate_encryption(
        tmp_path / "instance", source_catalog,
        source_backend=source, destination_backend=destination,
        source_dimension="legacy", destination_dimension="minio",
        source_identity=tmp_path / "legacy.identity",
        destination_material=_material(DESTINATION_DOMAIN),
        source_catalog_etag="catalog-1", destination_catalog_etag="catalog-1",
        resume=True,
    )
    assert result["status"] == "committed"


def _seed_reconciliation_journal(tmp_path, status):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    destination.catalog_body = b"old-catalog"
    operations.plan_encryption_migration(
        source_catalog,
        source,
        destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_domain_id=SOURCE_DOMAIN,
        destination_material=_material(DESTINATION_DOMAIN),
    )
    body, etag = destination.controls["control/migration-journal.v1.json"]
    journal = json.loads(body)
    destination_body = b"new-ciphertext"
    destination_digest = hashlib.sha256(destination_body).hexdigest()
    journal["mappings"][0].update({
        "destination_object_key": f"objects/sha256/{destination_digest}",
        "destination_sha256": destination_digest,
        "destination_size": len(destination_body),
        "status": "verified",
    })
    journal["status"] = status
    destination.objects[journal["mappings"][0]["destination_object_key"]] = destination_body
    destination.controls["control/migration-journal.v1.json"] = (json.dumps(journal).encode(), etag)
    return source_catalog, source, destination, journal


def test_resume_does_not_call_an_old_readable_catalog_committed(tmp_path, monkeypatch):
    source_catalog, source, destination, _journal = _seed_reconciliation_journal(tmp_path, "cutover-published")
    monkeypatch.setattr(
        operations,
        "_read_remote_catalog",
        lambda *_args, **_kwargs: (Catalog.empty(dimension_id="minio", encryption_domain_id=DESTINATION_DOMAIN), "catalog-1"),
    )

    with pytest.raises(CatalogConflict, match="migration candidate"):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            resume=True,
        )
    body, _etag = destination.controls["control/migration-journal.v1.json"]
    assert json.loads(body)["status"] == "conflict"


def test_resume_marks_a_readable_candidate_catalog_committed(tmp_path, monkeypatch):
    source_catalog, source, destination, journal = _seed_reconciliation_journal(tmp_path, "cutover-published")
    candidate = operations._migration_catalog(
        source_catalog, None, journal["mappings"], "minio", DESTINATION_DOMAIN
    )
    calls = []

    def read_candidate(*_args, **_kwargs):
        calls.append(True)
        return candidate, "catalog-2"

    monkeypatch.setattr(operations, "_read_remote_catalog", read_candidate)
    result = operations.migrate_encryption(
        tmp_path / "instance",
        source_catalog,
        source_backend=source,
        destination_backend=destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_identity=tmp_path / "legacy.identity",
        destination_material=_material(DESTINATION_DOMAIN),
        resume=True,
    )

    assert result["status"] == "committed"
    assert calls


def test_resume_recovers_ready_to_commit_after_journal_update_crash(tmp_path, monkeypatch):
    source_catalog, source, destination, _journal = _seed_reconciliation_journal(tmp_path, "planned")

    class CrashOnCommitJournal(FakeStore):
        crashed = False

        def replace_control(self, key, body, expected_etag):
            if json.loads(body).get("status") == "committed" and not self.crashed:
                self.crashed = True
                error = RuntimeError("crash after journal update boundary")
                error.published = False
                raise error
            return super().replace_control(key, body, expected_etag)

    crashing_destination = CrashOnCommitJournal(destination.objects, domain=DESTINATION_DOMAIN)
    crashing_destination.controls = destination.controls
    monkeypatch.setattr(operations, "_encrypt_catalog", lambda *_args: b"catalog")
    monkeypatch.setattr(
        operations,
        "decrypt_file",
        lambda source_path, _ids, output: output.write_bytes(source_path.read_bytes()[4:]),
    )
    monkeypatch.setattr(
        operations,
        "encrypt_file",
        lambda source_path, _recipients, output: output.write_bytes(b"new:" + source_path.read_bytes()),
    )

    with pytest.raises(RuntimeError, match="journal update boundary"):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=crashing_destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            source_catalog_etag="catalog-1",
            destination_catalog_etag="catalog-1",
        )
    body, _etag = crashing_destination.controls["control/migration-journal.v1.json"]
    ready = json.loads(body)
    assert ready["status"] == "ready-to-commit"
    candidate = operations._migration_catalog(source_catalog, None, ready["mappings"], "minio", DESTINATION_DOMAIN)
    crashing_destination.catalog_body = b"catalog"
    monkeypatch.setattr(operations, "_read_remote_catalog", lambda *_args, **_kwargs: (candidate, "catalog-2"))
    result = operations.migrate_encryption(
        tmp_path / "instance",
        source_catalog,
        source_backend=source,
        destination_backend=crashing_destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_identity=tmp_path / "legacy.identity",
        destination_material=_material(DESTINATION_DOMAIN),
        source_catalog_etag="catalog-1",
        destination_catalog_etag="catalog-1",
        resume=True,
    )
    assert result["status"] == "committed"


def test_resume_rejects_a_destination_catalog_that_does_not_match_candidate(tmp_path, monkeypatch):
    source_catalog, source, destination, _journal = _seed_reconciliation_journal(tmp_path, "ready-to-commit")
    monkeypatch.setattr(
        operations,
        "_read_remote_catalog",
        lambda *_args, **_kwargs: (Catalog.empty(dimension_id="other", encryption_domain_id=DESTINATION_DOMAIN), "catalog-1"),
    )

    with pytest.raises(CatalogConflict, match="migration candidate"):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            resume=True,
        )


def test_resume_rejects_same_dimension_id_bound_to_a_different_bucket(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    source.config = SimpleNamespace(endpoint="https://source.example.invalid", bucket="source-bucket")
    destination.config = SimpleNamespace(endpoint="https://destination.example.invalid", bucket="destination-bucket")
    operations.plan_encryption_migration(
        source_catalog, source, destination, source_dimension="legacy", destination_dimension="minio",
        source_domain_id=SOURCE_DOMAIN, destination_material=_material(DESTINATION_DOMAIN),
    )
    source.config = SimpleNamespace(endpoint="https://rebound.example.invalid", bucket="rebound-bucket")

    with pytest.raises(ValueError, match="authority"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN), resume=True,
        )


def test_prerequisite_failure_does_not_leave_a_running_journal(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)

    with pytest.raises(ValueError, match="source encryption identity"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            destination_material=_material(DESTINATION_DOMAIN),
        )
    body, _etag = destination.controls["control/migration-journal.v1.json"]
    assert json.loads(body)["status"] == "failed"


def test_failed_journal_is_not_implicitly_resumable(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    with pytest.raises(ValueError, match="source encryption identity"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            destination_material=_material(DESTINATION_DOMAIN),
        )

    with pytest.raises(RuntimeError, match="not resumable"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN), resume=True,
        )


def test_migration_rejects_a_journal_with_an_unmapped_source_object(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    operations.plan_encryption_migration(
        source_catalog, source, destination, source_dimension="legacy", destination_dimension="minio",
        source_domain_id=SOURCE_DOMAIN, destination_material=_material(DESTINATION_DOMAIN),
    )
    body, etag = destination.controls["control/migration-journal.v1.json"]
    journal = json.loads(body)
    journal["mappings"] = []
    destination.controls["control/migration-journal.v1.json"] = (json.dumps(journal).encode(), etag)
    with pytest.raises(ValueError, match="journal mapping"):
        operations.migrate_encryption(
            tmp_path / "instance", source_catalog,
            source_backend=source, destination_backend=destination,
            source_dimension="legacy", destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN), resume=True,
        )


def test_migration_rejects_a_journal_with_a_wrong_source_object_binding(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot])
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    operations.plan_encryption_migration(
        source_catalog,
        source,
        destination,
        source_dimension="legacy",
        destination_dimension="minio",
        source_domain_id=SOURCE_DOMAIN,
        destination_material=_material(DESTINATION_DOMAIN),
    )
    body, etag = destination.controls["control/migration-journal.v1.json"]
    journal = json.loads(body)
    journal["mappings"][0]["source_object_key"] = "objects/sha256/" + "f" * 64
    destination.controls["control/migration-journal.v1.json"] = (json.dumps(journal).encode(), etag)

    with pytest.raises(ValueError, match="journal mapping"):
        operations.migrate_encryption(
            tmp_path / "instance",
            source_catalog,
            source_backend=source,
            destination_backend=destination,
            source_dimension="legacy",
            destination_dimension="minio",
            source_identity=tmp_path / "legacy.identity",
            destination_material=_material(DESTINATION_DOMAIN),
            resume=True,
        )


def test_migration_projects_source_catalog_into_an_empty_destination_catalog(tmp_path):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", envelope)
    source_catalog = _catalog([snapshot])
    destination_digest = hashlib.sha256(b"new-ciphertext").hexdigest()

    migrated = operations._migration_catalog(
        source_catalog,
        Catalog.empty(dimension_id="minio", encryption_domain_id=DESTINATION_DOMAIN),
        [{
            "source_sha256": snapshot["ciphertext_sha256"],
            "destination_object_key": f"objects/sha256/{destination_digest}",
            "destination_sha256": destination_digest,
            "destination_size": len(b"new-ciphertext"),
        }],
        "minio",
        DESTINATION_DOMAIN,
    )

    copied = migrated.resolve_snapshot("migration-room", "one")
    assert copied["jat_metadata"] == snapshot["jat_metadata"]
    assert copied["object_key"] == f"objects/sha256/{destination_digest}"


def test_cli_copy_resolves_each_dimension_provider(monkeypatch, tmp_path):
    from josh_room import cli

    args = cli.build_parser().parse_args([
        "snapshot", "copy", "migration-room", "--source-dimension", "source",
        "--destination-dimension", "destination", "--destination-room", "copy",
    ])
    dimensions = {
        "source": SimpleNamespace(provider="minio", dimension_id="source", encryption_domain_id=SOURCE_DOMAIN),
        "destination": SimpleNamespace(provider="minio", dimension_id="destination", encryption_domain_id=DESTINATION_DOMAIN),
    }
    calls = []
    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "DimensionRegistry", lambda _config: SimpleNamespace(select=lambda name: dimensions[name]))
    monkeypatch.setattr(cli, "_backend", lambda provider, _instance, dimension: calls.append((provider, dimension)) or object())
    monkeypatch.setattr(cli, "_identity", lambda: tmp_path / "legacy.identity")
    monkeypatch.setattr(cli, "_recipients", lambda: ["recipient-1", "recipient-2"])
    monkeypatch.setattr(cli, "_read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty("dimension"), "catalog-1"))
    monkeypatch.setattr(cli, "resolve_encryption_material", lambda dimension, _backend, **_kwargs: _material(dimension.encryption_domain_id))
    monkeypatch.setattr(cli, "copy_snapshot_stream", lambda *args, **kwargs: {"ok": True, "kwargs": kwargs})
    result = cli.dispatch(args, tmp_path / "instance")
    assert result["ok"] is True
    assert calls == [("minio", "source"), ("minio", "destination")]


def test_copy_reuses_equal_domain_ciphertext_without_jat(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", envelope)
    source_catalog = _catalog([snapshot], dimension="source")
    destination_catalog = Catalog.empty(dimension_id="destination", encryption_domain_id=SOURCE_DOMAIN)
    source = FakeStore({snapshot["object_key"]: envelope})
    destination = FakeStore({snapshot["object_key"]: envelope}, domain=SOURCE_DOMAIN)
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda *a, **k: pytest.fail("same-domain copy decrypted"))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    result = operations.copy_snapshot_stream(
        tmp_path / "instance", source_catalog, destination_catalog, source, destination,
        "migration-room", "copied-room", "latest", ["recipient-1", "recipient-2"],
        destination_etag="catalog-1",
        source_domain_id=SOURCE_DOMAIN, destination_domain_id=SOURCE_DOMAIN,
        source_key_generation=2, destination_key_generation=2,
    )
    assert destination.puts == []
    assert result["ciphertext_sha256"] == snapshot["ciphertext_sha256"]


def test_copy_cross_domain_reencrypts_outer_ciphertext_and_preserves_metadata(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path)
    snapshot = _snapshot("one", b"old:" + envelope)
    source_catalog = _catalog([snapshot], dimension="source")
    destination_catalog = Catalog.empty(dimension_id="destination", encryption_domain_id=DESTINATION_DOMAIN)
    source = FakeStore({snapshot["object_key"]: b"old:" + envelope})
    destination = FakeStore(domain=DESTINATION_DOMAIN)
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda source_path, _ids, output: output.write_bytes(source_path.read_bytes()[4:]))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda source_path, _recipients, output: output.write_bytes(b"new:" + source_path.read_bytes()))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    result = operations.copy_snapshot_stream(
        tmp_path / "instance", source_catalog, destination_catalog, source, destination,
        "migration-room", "copied-room", "latest", ["recipient-1", "recipient-2"],
        destination_etag="catalog-1",
        source_domain_id=SOURCE_DOMAIN, destination_domain_id=DESTINATION_DOMAIN,
        source_identity=tmp_path / "legacy.identity", destination_material=_material(DESTINATION_DOMAIN),
    )
    copied = result["catalog"].resolve_snapshot("copied-room", result["snapshot_id"])
    assert destination.puts[0][1] == b"new:" + envelope
    assert copied["jat_metadata"] == snapshot["jat_metadata"]
    assert copied["workspace_fingerprint"] == snapshot["workspace_fingerprint"]
