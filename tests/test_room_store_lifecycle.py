from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from josh_room.catalog import Catalog
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat
from josh_room.restic_store import (
    BackupSummary,
    ForgetPlan,
    MaintenanceResult,
    RepositoryInfo,
    SnapshotEntry,
    SnapshotInfo,
    SnapshotInventoryItem,
)
from josh_room.room_store_bridge import ExistingRoomStoreContext
from josh_room.room_store_lifecycle import (
    complete_logical_catalog_removal,
    copy_logical_jat_as_new,
    copy_logical_jat_to_dimension,
    export_logical_jat,
    extract_logical_jat,
    inspect_logical_jat,
    inspect_selected_room_store,
    optimize_room_store,
    reconcile_room_store,
    remove_logical_catalog_records,
    serve_logical_jat,
    verify_room_store,
)
from josh_room.room_store_references import ResticSnapshotRef
from josh_room.workspace_policy import load_capture_policy

DIMENSION = "archive"
DOMAIN = "00000000-0000-4000-8000-000000000001"
REPOSITORY = "a" * 64
WORKSPACE_SNAPSHOT = "b" * 64
WORKSPACE_TREE = "c" * 64
SIGNATURE = "d" * 64


class _NoAgentReferenceBackend:
    @staticmethod
    def read_control(_key, _max_bytes):
        return None, None


class _Context(ExistingRoomStoreContext):
    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


def _descriptor(
    *, room_id="source", logical_id="jat-source", workspace_snapshot=WORKSPACE_SNAPSHOT,
    capture_policy_sha256="e" * 64, logical_bytes=10, components=None,
):
    return LogicalJat.from_dict({
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": logical_id,
        "dimension_id": DIMENSION,
        "encryption_domain_id": DOMAIN,
        "room_id": room_id,
        "created_at": "2026-10-02T12:00:00Z",
        "capture_policy_sha256": capture_policy_sha256,
        "workspace": {
            "engine": "restic",
            "repository_id": REPOSITORY,
            "repository_format": 2,
            "snapshot_id": workspace_snapshot,
            "tree_id": WORKSPACE_TREE,
            "source_path": ".",
            "logical_bytes": logical_bytes,
            "data_added": 2,
            "data_added_packed": 1,
        },
        "components": components or {
            "rcc_environment": None,
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
    })


def _catalog(descriptor=None):
    descriptor = descriptor or _descriptor()
    catalog = Catalog.empty(DIMENSION, DOMAIN)
    digest = hashlib.sha256(b"descriptor").hexdigest()
    return catalog.add_logical_snapshot(
        descriptor.to_dict()["room_id"],
        "Source Room",
        descriptor,
        __import__("josh_room.local_store", fromlist=["ObjectRef"]).ObjectRef(
            f"objects/sha256/{digest}", digest, 50,
        ),
        SIGNATURE,
    )


def _context(*, descriptor=None, catalog=None, publish=None, store=None, repo_id=REPOSITORY):
    descriptor = descriptor or _descriptor()
    catalog = catalog or _catalog(descriptor)
    record = catalog.resolve_snapshot(descriptor.to_dict()["room_id"], descriptor.to_dict()["logical_jat_id"])
    return _Context(
        instance=None,
        backend=_NoAgentReferenceBackend(),
        catalog=catalog,
        catalog_etag="etag-before",
        store=store or SimpleNamespace(),
        private_dir=None,
        material=SimpleNamespace(
            recipient="recipient",
            encryption_domain_id=DOMAIN,
            keyset=SimpleNamespace(recovery_recipients=("recovery",), key_generation=1),
        ),
        dimension=SimpleNamespace(
            dimension_id=DIMENSION,
            provider="minio",
            endpoint="https://minio.example.invalid",
            bucket="room-bucket",
        ),
        physical_binding="minio:https://minio.example.invalid:room-bucket",
        project_id=descriptor.to_dict()["room_id"],
        repository_info=RepositoryInfo(repo_id, 2),
        selected_record=record,
        selected_descriptor=descriptor,
        publish_descriptor=publish,
    )


def test_inspect_returns_descriptor_metadata_without_materializing_or_exporting(monkeypatch):
    descriptor = _descriptor()
    record = _catalog(descriptor).resolve_snapshot("source", "jat-source")
    monkeypatch.setattr(
        "josh_room.room_store_lifecycle.export_portable_jat",
        lambda **_kwargs: pytest.fail("Inspect must not export"),
    )

    result = inspect_logical_jat(record, descriptor)

    assert result["logical_jat_id"] == "jat-source"
    assert result["room_id"] == "source"
    assert result["workspace_snapshot_id"] == WORKSPACE_SNAPSHOT
    assert result["logical_bytes"] == 10
    assert result["data_added_bytes"] == 2
    assert result["components"] == {
        "rcc_environment": None,
        "homebrew_recovery": None,
        "hauler_content": None,
    }
    assert result["export_available"] is True


def test_same_dimension_copy_reseals_descriptor_and_reuses_exact_snapshot_refs():
    source = _descriptor()
    catalog = _catalog(source)
    captured = {}

    def publish(descriptor, *, expected_etag, workspace_signature, signature_algorithm):
        captured.update(
            descriptor=descriptor,
            expected_etag=expected_etag,
            workspace_signature=workspace_signature,
            signature_algorithm=signature_algorithm,
        )

    source_context = _context(descriptor=source, catalog=catalog)
    destination_context = _context(descriptor=source, catalog=catalog, publish=publish)

    result = copy_logical_jat_as_new(
        source_context,
        destination_context,
        "destination",
        "Destination Room",
    )

    copied = captured["descriptor"].to_dict()
    original = source.to_dict()
    assert copied["logical_jat_id"] != original["logical_jat_id"]
    assert copied["room_id"] == "destination"
    assert copied["origin_room_id"] == "source"
    assert copied["workspace"] == original["workspace"]
    assert copied["components"] == original["components"]
    assert captured["expected_etag"] == "etag-before"
    assert captured["workspace_signature"] == SIGNATURE
    assert result["status"] == "published"
    assert result["workspace_snapshot_id"] == WORKSPACE_SNAPSHOT


def test_copy_fails_closed_when_source_and_destination_catalog_revisions_differ():
    source = _descriptor()
    source_context = _context(descriptor=source)
    destination_context = _context(
        descriptor=source,
        publish=lambda *_args, **_kwargs: pytest.fail("stale copy must not publish"),
    )
    destination_context.catalog_etag = "etag-newer"

    with pytest.raises(ValueError, match="catalog revision"):
        copy_logical_jat_as_new(source_context, destination_context, "destination", "Destination Room")


def test_same_dimension_copy_refuses_different_physical_buckets():
    source = _descriptor()
    source_context = _context(descriptor=source)
    destination_context = _context(
        descriptor=source,
        publish=lambda *_args, **_kwargs: pytest.fail("cross-bucket copy must not reuse refs"),
    )
    destination_context.dimension.bucket = "another-bucket"
    destination_context.physical_binding = "minio:https://minio.example.invalid:another-bucket"

    with pytest.raises(ValueError, match="physical Room Store"):
        copy_logical_jat_as_new(source_context, destination_context, "destination", "Destination Room")


def test_copy_refuses_legacy_or_unknown_payloads():
    source = _descriptor()
    source_context = _context(descriptor=source)
    source_context.selected_record = {"snapshot_id": "legacy"}
    destination_context = _context(descriptor=source)
    with pytest.raises(ValueError, match="portable JAT"):
        copy_logical_jat_as_new(source_context, destination_context, "destination", "Destination Room")


def test_reconcile_is_read_only_and_reports_reference_categories(tmp_path):
    descriptor = _descriptor()
    catalog = _catalog(descriptor)
    inventory = (
        SnapshotInventoryItem(WORKSPACE_SNAPSHOT, WORKSPACE_TREE, "2026-10-02T12:00:00Z", None),
        SnapshotInventoryItem("f" * 64, "1" * 64, "2026-10-02T12:00:00Z", None),
    )
    store = SimpleNamespace(snapshots=lambda: inventory)
    context = replace(_context(descriptor=descriptor, catalog=catalog, store=store), instance=tmp_path)

    result = reconcile_room_store(context, {("source", "jat-source"): descriptor})

    assert result["restic_only_orphans"] == ["f" * 64]
    assert result["missing_from_restic"] == []
    assert result["catalog_referenced"] == [WORKSPACE_SNAPSHOT]
    assert result["component_only"] == []


@pytest.mark.parametrize("agent_failure", [False, True])
def test_cross_dimension_copy_restores_and_rebacks_workspace_under_destination_binding(tmp_path, monkeypatch, agent_failure):
    source_workspace = tmp_path / "source-workspace"
    source_workspace.mkdir()
    (source_workspace / "hello.txt").write_text("hello", encoding="utf-8")
    policy_hash = load_capture_policy(source_workspace).sha256
    source = _descriptor(capture_policy_sha256=policy_hash, logical_bytes=5)
    source_catalog = _catalog(source)
    source_private = tmp_path / "source-private"
    destination_private = tmp_path / "destination-private"
    source_private.mkdir(mode=0o700)
    destination_private.mkdir(mode=0o700)
    source_tree = source.to_dict()["workspace"]["tree_id"]

    class SourceStore:
        def snapshot(self, _snapshot_id):
            return SnapshotInfo(WORKSPACE_SNAPSHOT, source_tree, None, "2026-10-02T12:00:00Z", ("/workspace",))

        def entries(self, _snapshot_id):
            return iter((SnapshotEntry(".", "dir", 0, 0o755, None), SnapshotEntry("hello.txt", "file", 5, 0o644, None)))

        def restore(self, _snapshot_id, destination):
            destination.mkdir()
            (destination / "hello.txt").write_text("hello", encoding="utf-8")

    destination_repo_id = "b" * 64
    backed_up = {}

    class DestinationStore:
        def backup(self, workspace, *, parent=None, cancellation=None):
            backed_up["workspace"] = Path(workspace)
            backed_up["parent"] = parent
            backed_up["workspace_bytes"] = (Path(workspace) / "hello.txt").read_bytes()
            return BackupSummary("f" * 64, 1, 0, 0, 5, 5, 5, 0)

        def snapshot(self, snapshot_id):
            assert snapshot_id == "f" * 64
            return SnapshotInfo(snapshot_id, "c" * 64, None, "2026-10-03T12:00:00Z", ("/workspace",))

        def entries(self, _snapshot_id):
            return iter((SnapshotEntry(".", "dir", 0, 0o755, None), SnapshotEntry("hello.txt", "file", 5, 0o644, None)))

        def restore(self, _snapshot_id, destination):
            destination.mkdir()
            (destination / "hello.txt").write_text("hello", encoding="utf-8")

    source_context = _context(descriptor=source, catalog=source_catalog, store=SourceStore())
    source_context.private_dir = source_private
    destination_context = SimpleNamespace(
        instance=tmp_path / "instance",
        backend=SimpleNamespace(),
        catalog=Catalog.empty("backup", "00000000-0000-4000-8000-000000000002"),
        catalog_etag=None,
        store=DestinationStore(),
        private_dir=destination_private,
        material=SimpleNamespace(encryption_domain_id="00000000-0000-4000-8000-000000000002"),
        dimension=SimpleNamespace(
            dimension_id="backup", provider="minio", endpoint="https://minio.example.invalid", bucket="backup-bucket",
        ),
        physical_binding="minio:https://minio.example.invalid:backup-bucket",
        project_id="copied",
        repository_info=RepositoryInfo(destination_repo_id, 2),
        selected_record=None,
        selected_descriptor=None,
        writable=True,
        publish_descriptor=lambda descriptor, **kwargs: backed_up.update(descriptor=descriptor, publish_kwargs=kwargs),
    )
    source_context.dimension.endpoint = "https://minio.example.invalid"
    source_context.dimension.bucket = "source-bucket"

    if agent_failure:
        monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.preflight_copy", lambda *_args: None)

        def fail_agent_copy(*_args):
            raise ValueError("untrusted private provider details")

        monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.copy_reference", fail_agent_copy)
        with pytest.raises(ValueError) as raised:
            copy_logical_jat_to_dimension(source_context, destination_context, "copied", "Copied Room")
        assert raised.value.result["publication_state"] == "committed"
        assert raised.value.result["agent_recovery_state"] == "uncertain"
        assert backed_up["descriptor"].to_dict()["room_id"] == "copied"
        receipt = json.loads(next((destination_context.instance / "receipts").glob("*.json")).read_text())
        assert receipt["publication_state"] == "committed"
        assert "unpublished_restic_snapshot_ids" not in receipt
        return

    result = copy_logical_jat_to_dimension(
        source_context,
        destination_context,
        "copied",
        "Copied Room",
    )

    copied = backed_up["descriptor"].to_dict()
    assert result["status"] == "published"
    assert copied["workspace"]["repository_id"] == destination_repo_id
    assert copied["workspace"]["snapshot_id"] == "f" * 64
    assert copied["components"] == source.to_dict()["components"]
    assert copied["room_id"] == "copied"
    assert copied["origin_room_id"] == "source"
    assert backed_up["workspace_bytes"] == b"hello"


def test_cross_dimension_copy_rebacks_components_with_destination_snapshot_bindings(tmp_path, monkeypatch):
    from josh_room import room_store_lifecycle

    archive_bytes = b"verified RCCA"
    metadata_bytes = b'{"format_version":1}\n'
    archive_path = tmp_path / "rcc-environment.rcca"
    metadata_path = tmp_path / "metadata.json"
    archive_path.write_bytes(archive_bytes)
    metadata_path.write_bytes(metadata_bytes)
    component_snapshot = "7" * 64
    component = {
        "kind": "rcca",
        "snapshot": {
            "repository_id": REPOSITORY,
            "repository_format": 2,
            "snapshot_id": component_snapshot,
            "tree_id": "8" * 64,
        },
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "archive_size": len(archive_bytes),
        "member_basename": "rcc-environment.rcca",
        "artifact_digest": "sha256:" + "3" * 64,
        "specification_digest": "sha256:" + "4" * 64,
        "platform": "linux-x64",
        "rcc_version": "v18.19.5",
        "robot_relative_path": "robot.yaml",
    }
    source_workspace = tmp_path / "source-workspace"
    source_workspace.mkdir()
    (source_workspace / "hello.txt").write_text("hello", encoding="utf-8")
    policy_hash = load_capture_policy(source_workspace).sha256
    source = _descriptor(
        capture_policy_sha256=policy_hash,
        logical_bytes=5,
        components={"rcc_environment": component, "homebrew_recovery": None, "hauler_content": None},
    )
    source_catalog = _catalog(source)
    source_private = tmp_path / "source-private"
    destination_private = tmp_path / "destination-private"
    source_private.mkdir(mode=0o700)
    destination_private.mkdir(mode=0o700)
    source_tree = source.to_dict()["workspace"]["tree_id"]

    class Lease:
        rcc_archive = archive_path
        rcc_metadata = metadata_path
        jat_rcc_metadata = None
        brew_archive = None
        hauler_archive = None
        manifest: ClassVar[dict] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(room_store_lifecycle, "materialize_components", lambda *_args: Lease())

    class SourceStore:
        def snapshot(self, snapshot_id):
            if snapshot_id == component_snapshot:
                return SnapshotInfo(snapshot_id, "8" * 64, None, "2026-10-02T12:00:00Z", ("/component",))
            return SnapshotInfo(snapshot_id, source_tree, None, "2026-10-02T12:00:00Z", ("/workspace",))

        def entries(self, snapshot_id):
            if snapshot_id == component_snapshot:
                return iter((SnapshotEntry(".", "dir", 0, 0o700, None), SnapshotEntry("rcc-environment.rcca", "file", len(archive_bytes), 0o600, None), SnapshotEntry("metadata.json", "file", len(metadata_bytes), 0o600, None)))
            return iter((SnapshotEntry(".", "dir", 0, 0o755, None), SnapshotEntry("hello.txt", "file", 5, 0o644, None)))

        def restore(self, snapshot_id, destination):
            destination.mkdir()
            if snapshot_id == component_snapshot:
                (destination / "rcc-environment.rcca").write_bytes(archive_bytes)
                (destination / "metadata.json").write_bytes(metadata_bytes)
            else:
                (destination / "hello.txt").write_text("hello", encoding="utf-8")

    dest_repo_id = "b" * 64
    destination_files = {}
    next_ids = iter(("9" * 64, "a" * 64))

    class DestinationStore:
        def backup(self, workspace, *, parent=None, cancellation=None):
            snapshot_id = next(next_ids)
            root = Path(workspace)
            files = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
            destination_files[snapshot_id] = files
            return BackupSummary(snapshot_id, len(files), 0, 0, 10, 8, 10, 0)

        def snapshot(self, snapshot_id):
            return SnapshotInfo(snapshot_id, "b" * 64 if snapshot_id == "9" * 64 else "c" * 64, None, "2026-10-03T12:00:00Z", ("/destination",))

        def entries(self, snapshot_id):
            files = destination_files[snapshot_id]
            return iter((SnapshotEntry(".", "dir", 0, 0o700, None), *(SnapshotEntry(name, "file", len(data), 0o600, None) for name, data in files.items())))

        def restore(self, snapshot_id, destination):
            destination.mkdir()
            for name, data in destination_files[snapshot_id].items():
                (destination / name).write_bytes(data)

    published = {}
    source_context = _context(descriptor=source, catalog=source_catalog, store=SourceStore())
    source_context.private_dir = source_private
    destination_context = SimpleNamespace(
        instance=tmp_path / "instance",
        backend=SimpleNamespace(),
        catalog=Catalog.empty("backup", "00000000-0000-4000-8000-000000000002"),
        catalog_etag=None,
        store=DestinationStore(),
        private_dir=destination_private,
        material=SimpleNamespace(encryption_domain_id="00000000-0000-4000-8000-000000000002"),
        dimension=SimpleNamespace(dimension_id="backup", provider="minio", endpoint="https://minio.example.invalid", bucket="backup-bucket"),
        physical_binding="minio:https://minio.example.invalid:backup-bucket",
        project_id="copied",
        repository_info=RepositoryInfo(dest_repo_id, 2),
        selected_record=None,
        selected_descriptor=None,
        writable=True,
        publish_descriptor=lambda descriptor, **kwargs: published.update(descriptor=descriptor, **kwargs),
    )
    source_context.dimension.endpoint = "https://minio.example.invalid"
    source_context.dimension.bucket = "source-bucket"

    copy_logical_jat_to_dimension(source_context, destination_context, "copied", "Copied Room")

    copied_component = published["descriptor"].to_dict()["components"]["rcc_environment"]
    assert copied_component["snapshot"]["repository_id"] == dest_repo_id
    assert copied_component["snapshot"]["snapshot_id"] == "9" * 64
    assert copied_component["archive_sha256"] == component["archive_sha256"]
    assert copied_component["snapshot"]["snapshot_id"] != component_snapshot


def test_cross_copy_uncertain_catalog_publication_receipt_does_not_call_snapshots_orphans(tmp_path):
    source_workspace = tmp_path / "source-workspace"
    source_workspace.mkdir()
    (source_workspace / "hello.txt").write_text("hello", encoding="utf-8")
    source = _descriptor(
        capture_policy_sha256=load_capture_policy(source_workspace).sha256,
        logical_bytes=5,
    )
    source_private = tmp_path / "source-private"
    destination_private = tmp_path / "destination-private"
    source_private.mkdir(mode=0o700)
    destination_private.mkdir(mode=0o700)
    source_tree = source.to_dict()["workspace"]["tree_id"]

    class SourceStore:
        def snapshot(self, snapshot_id):
            return SnapshotInfo(snapshot_id, source_tree, None, "2026-10-02T12:00:00Z", ("/workspace",))

        def entries(self, _snapshot_id):
            return iter((SnapshotEntry(".", "dir", 0, 0o755, None), SnapshotEntry("hello.txt", "file", 5, 0o644, None)))

        def restore(self, _snapshot_id, destination):
            destination.mkdir()
            (destination / "hello.txt").write_text("hello", encoding="utf-8")

    class DestinationStore:
        def backup(self, *_args, **_kwargs):
            return BackupSummary("f" * 64, 1, 0, 0, 5, 5, 5, 0)

        def snapshot(self, snapshot_id):
            return SnapshotInfo(snapshot_id, "c" * 64, None, "2026-10-03T12:00:00Z", ("/workspace",))

        def entries(self, _snapshot_id):
            return iter((SnapshotEntry(".", "dir", 0, 0o755, None), SnapshotEntry("hello.txt", "file", 5, 0o644, None)))

        def restore(self, _snapshot_id, destination):
            destination.mkdir()
            (destination / "hello.txt").write_text("hello", encoding="utf-8")

    class PublicationError(RuntimeError):
        published = True
        descriptor_object_key = "objects/sha256/" + "6" * 64

    source_context = _context(descriptor=source, catalog=_catalog(source), store=SourceStore())
    source_context.private_dir = source_private
    destination_context = SimpleNamespace(
        instance=tmp_path / "instance",
        backend=SimpleNamespace(),
        catalog=Catalog.empty("backup", "00000000-0000-4000-8000-000000000002"),
        catalog_etag=None,
        store=DestinationStore(),
        private_dir=destination_private,
        material=SimpleNamespace(encryption_domain_id="00000000-0000-4000-8000-000000000002"),
        dimension=SimpleNamespace(dimension_id="backup", provider="minio", endpoint="https://minio.example.invalid", bucket="backup-bucket"),
        physical_binding="minio:https://minio.example.invalid:backup-bucket",
        project_id="copied",
        repository_info=RepositoryInfo("b" * 64, 2),
        selected_record=None,
        selected_descriptor=None,
        writable=True,
        publish_descriptor=lambda *_args, **_kwargs: (_ for _ in ()).throw(PublicationError()),
    )
    source_context.dimension.endpoint = "https://minio.example.invalid"
    source_context.dimension.bucket = "source-bucket"

    with pytest.raises(ValueError) as raised:
        copy_logical_jat_to_dimension(source_context, destination_context, "copied", "Copied Room")

    receipt_id = raised.value.result["receipt_id"]
    receipt = json.loads((destination_context.instance / "receipts" / f"{receipt_id}.json").read_text())
    assert receipt["publication_state"] == "committed-verification-unknown"
    assert receipt["candidate_restic_snapshot_ids"] == ["f" * 64]
    assert receipt["descriptor_object_key"] == PublicationError.descriptor_object_key
    assert "unpublished_restic_snapshot_ids" not in receipt


@pytest.mark.parametrize(
    ("read_data", "read_data_subset", "expected"),
    [
        (False, None, {"read_data": False, "read_data_subset": None}),
        (True, None, {"read_data": True, "read_data_subset": None}),
        (False, "10%", {"read_data": False, "read_data_subset": "10%"}),
    ],
)
def test_verify_uses_only_explicit_read_scope(read_data, read_data_subset, expected):
    calls = []
    store = SimpleNamespace(
        check=lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(read_data=kwargs["read_data"], read_data_subset=kwargs["read_data_subset"]),
    )

    result = verify_room_store(store, read_data=read_data, read_data_subset=read_data_subset)

    assert calls == [expected]
    assert result["status"] == "verified"


def test_optimize_defaults_to_dry_run_and_requires_explicit_confirmation():
    calls = []
    store = SimpleNamespace(
        prune=lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(operation="prune", dry_run=kwargs["dry_run"]),
    )

    plan = optimize_room_store(store)
    completed = optimize_room_store(store, confirmed=True)

    assert calls == [
        {"dry_run": True, "confirmed": False},
        {"dry_run": False, "confirmed": True},
    ]
    assert plan["status"] == "planned"
    assert completed["status"] == "completed"


def test_export_delegates_only_when_explicitly_called(monkeypatch, tmp_path):
    descriptor = _descriptor()
    context = _context(descriptor=descriptor)
    calls = []
    monkeypatch.setattr(
        "josh_room.room_store_lifecycle.export_portable_jat",
        lambda **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            logical_jat_id="jat-source",
            status="exported",
            output_size=10,
            output_sha256="f" * 64,
            workspace_entry_count=1,
            verified_components=(),
            estimated_required_bytes=10,
            available_bytes=100,
        ),
    )

    result = export_logical_jat(context, jat_root=tmp_path / "jat", output=tmp_path / "out.haul")

    assert calls[0]["descriptor"] is descriptor
    assert result["status"] == "exported"
    assert "output" not in result


def test_serve_and_extract_materialize_only_for_the_explicit_actions(monkeypatch, tmp_path):
    descriptor = _descriptor()
    context = _context(descriptor=descriptor)
    context.private_dir = tmp_path / "private"
    context.private_dir.mkdir(mode=0o700)
    calls = []

    def export(**kwargs):
        kwargs["output"].write_bytes(b"verified-haul")
        return SimpleNamespace()

    monkeypatch.setattr("josh_room.room_store_lifecycle.export_portable_jat", export)
    monkeypatch.setattr("josh_room.room_store_lifecycle.run_serve", lambda *_args, **kwargs: calls.append(("serve", kwargs)) or {"served": True})
    monkeypatch.setattr("josh_room.room_store_lifecycle.run_extract", lambda *_args: calls.append(("extract", _args)) or {"extracted": True})

    served = serve_logical_jat(context, jat_root=tmp_path / "jat", mode="files")
    extracted = extract_logical_jat(context, "hauler/app:latest", tmp_path / "extracted", jat_root=tmp_path / "jat")

    assert served["served"] is True
    assert extracted["extracted"] is True
    assert [call[0] for call in calls] == ["serve", "extract"]
    assert calls[0][1]["mode"] == "files"
    assert calls[1][1][2] == "hauler/app:latest"


def test_legacy_portable_export_decrypts_verified_payload_and_never_overwrites(tmp_path, monkeypatch):
    from josh_room import operations
    from josh_room.catalog import Catalog

    digest = "9" * 64
    record = {
        "snapshot_id": "legacy-jat",
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": 11,
        "created_at": "2026-10-02T12:00:00+00:00",
        "workspace_fingerprint": "8" * 64,
    }
    catalog = Catalog.empty(DIMENSION, DOMAIN).add_snapshot("legacy-room", "Legacy", record)

    class CatalogFileStub:
        def __init__(self, *_args):
            pass

        def read(self):
            return catalog

    class LocalStoreStub:
        def __init__(self, *_args):
            pass

        def download_file(self, _key, destination, *_metadata):
            destination.write_bytes(b"encrypted")

    def read_envelope(_envelope, haul):
        haul.write_bytes(b"portable JAT")
        return {"project_id": "legacy-room"}

    monkeypatch.setattr(operations, "CatalogFile", CatalogFileStub)
    monkeypatch.setattr(operations, "ImmutableLocalStore", LocalStoreStub)
    monkeypatch.setattr(operations, "decrypt_file", lambda _source, _ids, destination: destination.write_bytes(b"envelope"))
    monkeypatch.setattr(
        operations,
        "read_envelope_file",
        read_envelope,
    )
    monkeypatch.setattr(operations, "_manifest_matches_snapshot", lambda *_args: True)
    monkeypatch.setattr(operations, "report_progress", lambda *_args: None)
    target = tmp_path / "legacy.haul.tar.zst"

    result = operations.export_snapshot(
        tmp_path / "instance",
        "legacy-room",
        "legacy-jat",
        tmp_path / "identity",
        target,
    )

    assert result["status"] == "exported"
    assert target.read_bytes() == b"portable JAT"
    with pytest.raises(FileExistsError):
        operations.export_snapshot(
            tmp_path / "instance",
            "legacy-room",
            "legacy-jat",
            tmp_path / "identity",
            target,
        )


@pytest.mark.parametrize("agent_state", [None, "removed", "pending", "error"])
def test_native_snapshot_delete_cas_precedes_descriptor_object_delete_and_forget(tmp_path, monkeypatch, agent_state):
    descriptor = _descriptor()
    catalog = _catalog(descriptor)
    object_key = catalog.resolve_snapshot("source", "jat-source")["object_key"]
    deleted_objects = []
    published = {}
    events = []

    class Backend(_NoAgentReferenceBackend):
        def conditional_catalog_put(self, body, expected_etag):
            events.append("catalog")
            published.update(body=body, expected_etag=expected_etag)

        def delete_object(self, key):
            events.append("descriptor")
            deleted_objects.append(key)

    class Store:
        def snapshots(self):
            events.append("inventory")
            return (SnapshotInventoryItem(WORKSPACE_SNAPSHOT, WORKSPACE_TREE, "2026-10-02T12:00:00Z", None),)

        def plan_forget(self, snapshot_ids):
            return ForgetPlan(REPOSITORY, tuple(snapshot_ids), (WORKSPACE_TREE,))

        def forget(self, snapshot_ids, *, plan, confirmed=False):
            events.append("forget")
            published["forget"] = (tuple(snapshot_ids), plan, confirmed)
            return MaintenanceResult("forget", False, tuple(snapshot_ids))

        def prune(self, **_kwargs):
            pytest.fail("delete must never prune")

    context = _context(descriptor=descriptor, catalog=catalog, store=Store())
    context.instance = tmp_path / "instance"
    context.backend = Backend()
    context.catalog_etag = "etag-before"
    context.writable = True

    def encrypted(candidate, *_args):
        published["candidate"] = candidate
        return b"encrypted-catalog"

    monkeypatch.setattr("josh_room.room_store_lifecycle._encrypt_catalog", encrypted)
    if agent_state is not None:
        def close_agents(*_args):
            events.append("agent-close")
            return ({"room_id": "source", "logical_jat_id": "jat-source", "objects": []},)

        def complete_agents(*_args, **_kwargs):
            events.append("agent-cleanup")
            if agent_state == "error":
                raise ValueError("untrusted private provider details")
            return {"state": agent_state, "reconciliation_required": agent_state == "pending"}

        monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.close_for_records", close_agents)
        monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.complete_removed_references", complete_agents)

    pending = remove_logical_catalog_records(
        context,
        {("source", "jat-source")},
        descriptors={("source", "jat-source"): descriptor},
    )

    assert published["expected_etag"] == "etag-before"
    assert deleted_objects == [object_key]
    assert pending.catalog_revision == catalog.body["revision"] + 1
    assert set(pending.snapshot_trees) == {ResticSnapshotRef(REPOSITORY, WORKSPACE_SNAPSHOT)}

    fresh_catalog = published["candidate"]
    fresh_context = SimpleNamespace(
        instance=context.instance,
        backend=context.backend,
        catalog=fresh_catalog,
        catalog_etag="etag-after",
        store=Store(),
        private_dir=tmp_path / "fresh-private",
        material=context.material,
        dimension=context.dimension,
        project_id=None,
        repository_info=context.repository_info,
    )

    result = complete_logical_catalog_removal(fresh_context, pending, descriptors={})

    if agent_state is not None:
        assert events.index("agent-close") < events.index("catalog") < events.index("descriptor")
        assert events.index("inventory") < events.index("agent-cleanup")
        assert pending.agent_reference_receipts
        receipt = json.loads((context.instance / "receipts" / f"{pending.operation_id}.json").read_text())
        assert receipt["agent_reference_receipts"]
    if agent_state in {"pending", "error"}:
        assert result["status"] == "cleanup-pending" and result["reconciliation_required"] is True
        assert result["restic_snapshots_forgotten"] == []
        assert "forget" not in events
        return
    assert result["restic_snapshots_forgotten"] == [WORKSPACE_SNAPSHOT]
    assert published["forget"][2] is True


def test_delete_rechecks_after_cas_and_skips_newly_referenced_snapshot(tmp_path, monkeypatch):
    descriptor = _descriptor()
    catalog = _catalog(descriptor)
    published = {}

    class Backend(_NoAgentReferenceBackend):
        def conditional_catalog_put(self, body, _expected_etag):
            published["body"] = body

        def delete_object(self, _key):
            return None

    class Store:
        def __init__(self):
            self.forgotten = False

        def snapshots(self):
            return (SnapshotInventoryItem(WORKSPACE_SNAPSHOT, WORKSPACE_TREE, "2026-10-02T12:00:00Z", None),)

        def plan_forget(self, *_args):
            return ForgetPlan(REPOSITORY, (WORKSPACE_SNAPSHOT,), (WORKSPACE_TREE,))

        def forget(self, *_args, **_kwargs):
            self.forgotten = True
            return MaintenanceResult("forget", False, (WORKSPACE_SNAPSHOT,))

    store = Store()
    context = _context(descriptor=descriptor, catalog=catalog, store=store)
    context.instance = tmp_path / "instance"
    context.backend = Backend()
    context.catalog_etag = "etag-before"

    def encrypted(candidate, *_args):
        published["candidate"] = candidate
        return b"encrypted-catalog"

    monkeypatch.setattr("josh_room.room_store_lifecycle._encrypt_catalog", encrypted)
    pending = remove_logical_catalog_records(
        context,
        {("source", "jat-source")},
        descriptors={("source", "jat-source"): descriptor},
    )

    alias = _descriptor(room_id="other-room", logical_id="jat-alias")
    digest = hashlib.sha256(b"alias-descriptor").hexdigest()
    after_delete = published["candidate"].add_logical_snapshot(
        "other-room",
        "Other Room",
        alias,
        ObjectRef(f"objects/sha256/{digest}", digest, 50),
        SIGNATURE,
    )
    fresh_context = _context(descriptor=alias, catalog=after_delete, store=store)
    fresh_context.instance = context.instance
    fresh_context.backend = context.backend
    fresh_context.catalog_etag = "etag-after-alias"

    result = complete_logical_catalog_removal(
        fresh_context,
        pending,
        descriptors={("other-room", "jat-alias"): alias},
    )

    assert result["restic_snapshots_forgotten"] == []
    assert result["still_referenced_snapshot_ids"] == [WORKSPACE_SNAPSHOT]
    assert store.forgotten is False


@pytest.mark.parametrize("agent_closure", [False, True])
def test_catalog_conflict_never_deletes_descriptor_objects_or_restic_snapshots(tmp_path, monkeypatch, agent_closure):
    from josh_room import room_store_lifecycle

    descriptor = _descriptor()
    catalog = _catalog(descriptor)
    deleted_objects = []

    class Conflict(RuntimeError):
        published = False

    class Backend(_NoAgentReferenceBackend):
        def conditional_catalog_put(self, *_args):
            raise Conflict("stale")

        def delete_object(self, key):
            deleted_objects.append(key)

    class Store:
        def snapshots(self):
            return (SnapshotInventoryItem(WORKSPACE_SNAPSHOT, WORKSPACE_TREE, "2026-10-02T12:00:00Z", None),)

        def forget(self, *_args, **_kwargs):
            pytest.fail("catalog conflict must prevent Restic forget")

    context = _context(descriptor=descriptor, catalog=catalog, store=Store())
    context.instance = tmp_path / "instance"
    context.backend = Backend()
    context.catalog_etag = "stale-etag"
    monkeypatch.setattr(room_store_lifecycle, "_encrypt_catalog", lambda *_args: b"encrypted")
    if agent_closure:
        monkeypatch.setattr(
            room_store_lifecycle.native_agent_reference, "close_for_records",
            lambda *_args: ({"room_id": "source", "logical_jat_id": "jat-source", "objects": []},),
        )

    with pytest.raises(ValueError, match="catalog removal was not confirmed") as raised:
        remove_logical_catalog_records(
            context,
            {("source", "jat-source")},
            descriptors={("source", "jat-source"): descriptor},
        )

    assert raised.value.result["publication_state"] == "rejected"
    assert raised.value.result["reconciliation_required"] is agent_closure
    if agent_closure:
        assert raised.value.result["agent_recovery_state"] == "captures-closed"
    assert deleted_objects == []


def test_selected_inspect_reports_agent_metadata_outside_closed_components(monkeypatch):
    context = _context()
    metadata = {"state": "history-only", "native_resume_verified": False}
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.inspect_reference", lambda _context: None)
    absent = inspect_selected_room_store(context)
    assert "agent_recovery" not in absent
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.inspect_reference", lambda _context: metadata)
    present = inspect_selected_room_store(context)
    assert present["agent_recovery"] == metadata
    assert present["components"] == absent["components"]
    assert set(context.selected_descriptor.to_dict()["components"]) == {"rcc_environment", "homebrew_recovery", "hauler_content"}


def test_agent_copy_preflight_refuses_before_descriptor_publication(monkeypatch):
    calls = []
    context = _context(publish=lambda *_args, **_kwargs: calls.append("publish"))

    def refuse(*_args):
        raise ValueError("agent-reference-not-authorized")

    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.preflight_copy", refuse)
    with pytest.raises(ValueError, match="not-authorized"):
        copy_logical_jat_as_new(context, context, "destination", "Destination")
    assert calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_same_dimension_copy_preserves_agent_metadata_or_reports_committed_failure(tmp_path, monkeypatch, failure):
    events = []
    context = _context(publish=lambda *_args, **_kwargs: events.append("workspace-published"))
    context.instance = tmp_path / "instance"
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.preflight_copy", lambda *_args: events.append("agent-preflight"))

    def copy_agent(*_args):
        events.append("agent-copy")
        if failure:
            raise ValueError("secret internal URL or path")
        return {"state": "copied", "mode": "history-only", "data_uploaded": 123}

    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.copy_reference", copy_agent)
    if failure:
        with pytest.raises(ValueError) as raised:
            copy_logical_jat_as_new(context, context, "destination", "Destination")
        assert raised.value.result["publication_state"] == "committed"
        assert raised.value.result["agent_recovery_state"] == "uncertain"
        receipt = json.loads(next((context.instance / "receipts").glob("*.json")).read_text())
        assert receipt["publication_state"] == "committed"
        assert "secret internal" not in json.dumps(receipt)
    else:
        result = copy_logical_jat_as_new(context, context, "destination", "Destination")
        assert result["agent_recovery"]["mode"] == "history-only"
        assert result["data_uploaded"] == 123
    assert events == ["agent-preflight", "workspace-published", "agent-copy"]


def test_agent_export_preflight_prevents_portable_output(monkeypatch, tmp_path):
    calls = []

    def refuse(*_args):
        raise ValueError("agent-reference-export-target-collision")

    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.preflight_export", refuse)
    monkeypatch.setattr("josh_room.room_store_lifecycle.export_portable_jat", lambda **_kwargs: calls.append("portable"))
    with pytest.raises(ValueError, match="target-collision"):
        export_logical_jat(_context(), jat_root=tmp_path / "jat", output=tmp_path / "output.haul")
    assert calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_agent_export_companion_is_explicit_and_partial_output_is_disclosed(monkeypatch, tmp_path, failure):
    events = []
    output = tmp_path / "output.haul"
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.preflight_export", lambda *_args: events.append("agent-preflight"))

    def portable(**_kwargs):
        events.append("portable")
        output.write_bytes(b"synthetic portable bytes")
        return SimpleNamespace(logical_jat_id="jat-source", status="exported", output_size=24,
                               output_sha256="f" * 64, workspace_entry_count=1, verified_components=(),
                               estimated_required_bytes=24, available_bytes=100)

    def companion(*_args):
        events.append("agent-companion")
        if failure:
            raise ValueError("secret internal URL or path")
        return {"state": "exported", "mode": "history-only", "output_size": 10}

    monkeypatch.setattr("josh_room.room_store_lifecycle.export_portable_jat", portable)
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.export_reference", companion)
    if failure:
        with pytest.raises(ValueError) as raised:
            export_logical_jat(_context(), jat_root=tmp_path / "jat", output=output)
        assert raised.value.result["portable_export_committed"] is True
        assert raised.value.result["agent_recovery_state"] == "uncertain"
        assert "secret internal" not in str(raised.value)
    else:
        result = export_logical_jat(_context(), jat_root=tmp_path / "jat", output=output)
        assert result["agent_recovery"]["mode"] == "history-only"
    assert output.read_bytes() == b"synthetic portable bytes"
    assert events == ["agent-preflight", "portable", "agent-companion"]


def test_agent_reachability_is_reported_without_changing_restic_references(monkeypatch):
    context = _context(store=SimpleNamespace(snapshots=lambda: (
        SnapshotInventoryItem(WORKSPACE_SNAPSHOT, WORKSPACE_TREE, "2026-10-02T12:00:00Z", None),
    )))
    metadata = {"reachable_objects": ["objects/sha256/" + "f" * 64], "destructive_cleanup_performed": False}
    monkeypatch.setattr("josh_room.room_store_lifecycle.native_agent_reference.reconcile_references", lambda *_args, **_kwargs: metadata)
    result = reconcile_room_store(context, descriptors={("source", "jat-source"): context.selected_descriptor})
    assert result["agent_recovery"] == metadata
    assert result["catalog_referenced"] == [WORKSPACE_SNAPSHOT]
    assert result["destructive_cleanup_performed"] is False
