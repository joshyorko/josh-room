from __future__ import annotations

import base64
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import room_store_operations
from josh_room.cancellation import CLICancelled
from josh_room.logical_jat import LogicalJat
from josh_room.private_paths import (
    protect_private_directory,
    secure_private_file,
    validate_private_directory,
    verify_private_path,
)
from josh_room.restic_store import (
    BackupSummary,
    RepositoryInfo,
    ResticStoreError,
    ResticStoreErrorCode,
    SnapshotEntry,
    SnapshotInfo,
)
from josh_room.room_store_operations import (
    RoomStoreOperations,
    RoomStoreOperationsError,
    RoomStorePublicationError,
    _require_same_device,
    _validate_snapshot_entries,
)
from josh_room.workspace_policy import load_capture_policy

REPOSITORY_ID = "a" * 64
SNAPSHOT_ID = "b" * 64
TREE_ID = "c" * 64
NEW_SNAPSHOT_ID = "e" * 64
NEW_TREE_ID = "f" * 64
PASSWORD = base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("=")


def _descriptor(
    *,
    snapshot_id: str = SNAPSHOT_ID,
    parent: str | None = None,
    policy_sha: str = "d" * 64,
) -> LogicalJat:
    body = {
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": "jat-parent",
        "dimension_id": "dimension-test",
        "encryption_domain_id": "domain-test",
        "room_id": "room-test",
        "created_at": "2026-10-02T12:00:00Z",
        "capture_policy_sha256": policy_sha,
        "workspace": {
            "engine": "restic",
            "repository_id": REPOSITORY_ID,
            "repository_format": 2,
            "snapshot_id": snapshot_id,
            "tree_id": TREE_ID,
            "source_path": ".",
            "logical_bytes": 5,
            "data_added": 5,
            "data_added_packed": 5,
        },
        "components": {
            "rcc_environment": None,
            "homebrew_recovery": None,
            "hauler_content": None,
        },
        "source": {},
        "producer": {
            "josh_room_version": "0.1.26",
            "restic_version": "0.19.1",
            "source_platform": "linux-x64",
            "restore_platforms": ["linux-x64", "win32-x64"],
        },
    }
    if parent:
        body["parent_logical_jat_id"] = parent
        body["workspace"]["parent_snapshot_id"] = parent
    return LogicalJat.from_dict(body)


@dataclass
class _Keyset:
    room_store: object


@dataclass
class _RoomStoreSecret:
    secret: str = PASSWORD
    generation: int = 3
    repository_id: str | None = None


class _Store:
    def __init__(self, *, summary: BackupSummary | None = None, entries=None):
        self.summary = summary or BackupSummary(NEW_SNAPSHOT_ID, 1, 0, 0, 5, 5, 5, 0)
        self.entry_rows = list(
            entries
            or [
                SnapshotEntry(".", "dir", 0, 0o700, None),
                SnapshotEntry("file.txt", "file", 5, 0o600, None),
            ]
        )
        self.parents = []
        self.restored = None
        self.initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self):
        self.initialized = True
        return RepositoryInfo(REPOSITORY_ID, 2)

    def open_existing(self):
        self.initialized = True
        return RepositoryInfo(REPOSITORY_ID, 2)

    def backup(
        self,
        workspace,
        *,
        parent=None,
        excludes=None,
        on_progress=None,
        cancellation=None,
    ):
        self.parents.append(None if self.summary.force_scan else parent)
        return BackupSummary(
            self.summary.snapshot_id,
            self.summary.files_new,
            self.summary.files_changed,
            self.summary.files_unmodified,
            self.summary.data_added,
            self.summary.data_added_packed,
            self.summary.total_bytes_processed,
            self.summary.errors,
            self.summary.force_scan,
            None if self.summary.force_scan else parent,
        )

    def snapshot(self, snapshot_id):
        tree_id = NEW_TREE_ID if snapshot_id == NEW_SNAPSHOT_ID else TREE_ID
        parent_id = (
            self.parents[-1]
            if self.parents and snapshot_id != self.parents[-1]
            else None
        )
        return SnapshotInfo(
            snapshot_id, tree_id, parent_id, "2026-10-02T12:00:00Z", ("/workspace",)
        )

    def entries(self, snapshot_id, *, expected_tree_id=None):
        yield from self.entry_rows

    def restore(self, snapshot_id, destination):
        self.restored = Path(destination)
        self.restored.mkdir()
        (self.restored / "file.txt").write_text("hello", encoding="utf-8")

    def check(self, read_data=False):
        return None


class _Catalog:
    def __init__(self, latest=None):
        self.latest = latest
        self.etag = "etag-1"
        self.workspace_signature = None
        self.published = []
        self.marker_rows = []
        self.order = []

    def read_latest(self):
        return self.latest, self.etag

    def publish(
        self, descriptor, *, expected_etag, workspace_signature, signature_algorithm
    ):
        assert expected_etag == self.etag
        assert signature_algorithm == "josh-room-stat-v1"
        assert len(workspace_signature) == 64
        self.order.append("publish")
        self.published.append(descriptor)
        self.workspace_signature = workspace_signature
        self.latest = descriptor
        self.etag = "etag-2"

    def write_marker(
        self,
        descriptor,
        *,
        clean,
        workspace_signature,
        signature_algorithm,
        capture_policy_sha256,
    ):
        self.order.append("marker")
        self.marker_rows.append(
            (
                descriptor,
                clean,
                workspace_signature,
                signature_algorithm,
                capture_policy_sha256,
            )
        )


def _operations(tmp_path, workspace, store, catalog, *, binding=None):
    private_dir = tmp_path / "private"
    cache_dir = tmp_path / "cache"
    private_dir.mkdir(exist_ok=True, mode=0o700)
    cache_dir.mkdir(exist_ok=True, mode=0o700)
    protect_private_directory(private_dir)
    protect_private_directory(cache_dir)
    keyset = _Keyset(_RoomStoreSecret(repository_id=binding))
    if catalog.latest is not None and catalog.workspace_signature is None:
        catalog.workspace_signature = room_store_operations._scan_workspace(
            workspace, None
        ).signature

    def bind(_dimension, _backend, repository_id, *, expected_generation):
        assert expected_generation == keyset.room_store.generation
        keyset.room_store.repository_id = repository_id
        return keyset

    return RoomStoreOperations(
        workspace=workspace,
        repository="https://restic.example.test/bucket/room-store/v1",
        cache_dir=cache_dir,
        password_dir=private_dir,
        store_factory=lambda **_kwargs: store,
        dimension=object(),
        backend=object(),
        ensure_keyset=lambda *_args: keyset,
        bind_repository=bind,
        read_latest=catalog.read_latest,
        read_catalog_signature=lambda: (
            (
                catalog.workspace_signature,
                "josh-room-stat-v1",
                catalog.latest.to_dict()["capture_policy_sha256"],
            )
            if catalog.latest is not None and catalog.workspace_signature is not None
            else None
        ),
        read_snapshot_entries=lambda _snapshot_id: store.entry_rows,
        publish_descriptor=catalog.publish,
        write_marker=catalog.write_marker,
        descriptor_metadata={
            "dimension_id": "dimension-test",
            "encryption_domain_id": "domain-test",
            "room_id": "room-test",
            "components": {
                "rcc_environment": None,
                "homebrew_recovery": None,
                "hauler_content": None,
            },
            "source": {},
            "producer": {
                "josh_room_version": "0.1.26",
                "restic_version": "0.19.1",
                "source_platform": "linux-x64",
                "restore_platforms": ["linux-x64", "win32-x64"],
            },
        },
        secure_private_file=secure_private_file,
        validate_private_directory=validate_private_directory,
    )


@pytest.mark.parametrize("fail_at", ["factory", "context-entry"])
def test_save_restic_open_failure_preserves_safe_stage_diagnostic(tmp_path, fail_at):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("synthetic workspace\n", encoding="utf-8")
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, _Store(), catalog)
    failure = ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)

    if fail_at == "factory":
        def fail_open(**_kwargs):
            raise failure

        operations.store_factory = fail_open
    else:
        class FailingContext:
            def __enter__(self):
                raise failure

            def __exit__(self, *_args):
                return False

        operations.store_factory = lambda **_kwargs: FailingContext()
    with pytest.raises(RoomStoreOperationsError) as caught:
        operations.save()

    assert caught.value.result == {
        "stage": "restic-store-open",
        "command": "restic-store.open",
        "restic_error_code": "invalid-configuration",
        "cause": "restic store configuration is invalid",
    }
    assert catalog.published == []
    assert catalog.marker_rows == []


def test_save_publishes_complete_descriptor_before_marker_and_uses_explicit_parent(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    parent = _descriptor()
    store = _Store()
    catalog = _Catalog(parent)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)

    result = operations.save()

    assert result.status == "saved"
    assert store.parents == [SNAPSHOT_ID]
    assert len(catalog.published) == 1
    published = catalog.published[0]
    assert isinstance(published, LogicalJat)
    assert published.to_dict()["workspace"]["snapshot_id"] == NEW_SNAPSHOT_ID
    assert (
        published.to_dict()["parent_logical_jat_id"]
        == parent.to_dict()["logical_jat_id"]
    )
    assert catalog.order == ["publish", "marker"]


def test_changed_save_reuses_preflight_scan_but_keeps_post_capture_checks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello")
    store, catalog = _Store(), _Catalog(_descriptor())
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    policy = load_capture_policy(workspace)
    before = room_store_operations._scan_workspace(workspace, policy)
    binding = __import__("hashlib").sha256(str(workspace.resolve()).encode()).hexdigest()
    native_scan = room_store_operations._scan_workspace
    scans = []
    def traced_scan(root, policy):
        scans.append(root)
        return native_scan(root, policy)
    monkeypatch.setattr(room_store_operations, "_scan_workspace", traced_scan)
    result = operations.save(preflight_scan=(binding, before))
    assert result.status == "saved"
    assert len(scans) == 2  # Post-backup and post-publication checks remain.
    assert len(catalog.published) == 1


def test_reused_scan_cannot_report_stale_noop(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "file.txt"
    source.write_text("hello")
    policy = load_capture_policy(workspace)
    parent = _descriptor(policy_sha=policy.sha256)
    store, catalog = _Store(), _Catalog(parent)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    before = room_store_operations._scan_workspace(workspace, policy)
    binding = __import__("hashlib").sha256(str(workspace.resolve()).encode()).hexdigest()
    source.write_text("world")
    with pytest.raises(RoomStoreOperationsError, match="changed after Save preflight"):
        operations.save(preflight_scan=(binding, before))
    assert catalog.published == []
    assert catalog.marker_rows == []


def test_unchanged_save_publishes_no_descriptor_or_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store = _Store(summary=BackupSummary(None, 0, 0, 1, 0, 0, 5, 0))
    catalog = _Catalog(latest)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)

    result = operations.save()

    assert result.status == "already-saved"
    assert result.data_added_bytes == 0
    assert catalog.published == []
    assert catalog.marker_rows == []


def test_noop_requires_matching_catalog_signature(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store = _Store(summary=BackupSummary(None, 0, 0, 1, 0, 0, 5, 0))
    catalog = _Catalog(latest)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    catalog.workspace_signature = "0" * 64

    result = operations.save()

    assert result.status == "saved"
    assert len(catalog.published) == 1


def test_forced_scan_noop_fails_closed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store = _Store(summary=BackupSummary(None, 0, 0, 1, 0, 0, 5, 0, force_scan=True))
    catalog = _Catalog(latest)
    catalog.workspace_signature = "0" * 64
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)

    with pytest.raises(RoomStoreOperationsError, match="forced content scan"):
        operations.save()


def test_forced_scan_records_the_restic_parent_that_was_actually_used(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store = _Store(
        summary=BackupSummary(NEW_SNAPSHOT_ID, 1, 0, 0, 5, 5, 5, 0, force_scan=True)
    )
    catalog = _Catalog(latest)
    catalog.workspace_signature = "0" * 64
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)

    result = operations.save()

    assert result.status == "saved"
    body = catalog.published[0].to_dict()
    assert "parent_logical_jat_id" not in body
    assert "parent_snapshot_id" not in body["workspace"]


@pytest.mark.parametrize("windows", [False, True])
def test_verified_native_signature_skips_restic_backup_before_any_scan(
    tmp_path, monkeypatch, windows
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store = _Store()
    store.backup = lambda *_args, **_kwargs: pytest.fail(
        "verified native signature must avoid the Restic scan"
    )
    catalog = _Catalog(latest)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    monkeypatch.setattr(room_store_operations, "_WINDOWS_HOST", windows)
    monkeypatch.setattr(
        room_store_operations,
        "_windows_change_time_ns",
        lambda _path, metadata: metadata.st_ctime_ns,
    )

    result = operations.save()

    assert result.status == "already-saved"
    assert catalog.published == []


def test_verified_noop_honors_cancellation_without_backup_or_publication(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store, catalog = _Store(), _Catalog(latest)
    store.backup = lambda *_args, **_kwargs: pytest.fail("cancelled no-op must not back up")
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    with pytest.raises(ResticStoreError) as error:
        operations.save(cancellation=SimpleNamespace(cancelled=True))
    assert error.value.code is ResticStoreErrorCode.CANCELLED
    assert catalog.published == []
    assert catalog.marker_rows == []


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX change-time metadata")
def test_noop_preflight_detects_same_size_edit_with_restored_mtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "file.txt"
    source.write_text("hello")
    latest = _descriptor(policy_sha=load_capture_policy(workspace).sha256)
    store, catalog = _Store(), _Catalog(latest)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    before = source.stat()
    source.write_text("other")
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert operations.save().status == "saved"
    assert store.parents == [SNAPSHOT_ID]
    assert len(catalog.published) == 1


def test_save_rejects_external_symlink_before_restic_or_publication(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("secret", encoding="utf-8")
    (workspace / "outside.txt").symlink_to(external)
    store = _Store()
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)

    with pytest.raises(RoomStoreOperationsError, match="symlink"):
        operations.save()

    assert store.parents == []
    assert catalog.published == []


def test_restore_validates_then_promotes_a_new_staged_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _Store()
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    destination = tmp_path / "restored"

    result = operations.restore(
        _descriptor(),
        destination,
        write_restore_marker=lambda stage, final, descriptor: assert_marker_path(
            stage, final, descriptor
        ),
    )

    assert result.destination == destination
    assert (destination / "file.txt").read_text(encoding="utf-8") == "hello"
    assert not list(tmp_path.glob(".restored.josh-room-*"))


def test_restore_reuses_open_read_only_store_and_runs_callbacks_before_promotion(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = _Store()
    catalog = _Catalog(_descriptor())
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    destination = tmp_path / "restore-target"
    events = []

    def prepare(stage, descriptor, opened):
        assert opened is store
        events.append(("prepare", stage, descriptor))

    def marker(stage, target, descriptor):
        assert not target.exists()
        (stage / ".josh-room.json").write_text("staged", encoding="utf-8")
        events.append(("marker", stage, target, descriptor))

    operations.restore(
        _descriptor(),
        destination,
        prepare_restored_workspace=prepare,
        write_restore_marker=marker,
        restic_store=store,
        repository_info=RepositoryInfo(REPOSITORY_ID, 2),
    )

    assert [event[0] for event in events] == ["prepare", "marker"]
    assert (
        destination.joinpath(".josh-room.json").read_text(encoding="utf-8") == "staged"
    )


def test_windows_scan_uses_authoritative_identity_with_cached_direntry_stat(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    file = nested / "file.txt"
    file.write_bytes(b"synthetic")
    native_scandir = os.scandir
    seen = []

    def windows_scandir(directory):
        with native_scandir(directory) as entries:
            result = []
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                cached = SimpleNamespace(
                    st_dev=0,
                    st_ino=0,
                    st_nlink=0,
                    st_mode=metadata.st_mode,
                    st_size=metadata.st_size,
                    st_mtime_ns=metadata.st_mtime_ns,
                    st_ctime_ns=metadata.st_ctime_ns,
                )
                result.append(
                    SimpleNamespace(
                        path=entry.path,
                        name=entry.name,
                        stat=lambda cached=cached, **kwargs: cached,
                    )
                )
            return result

    def native_change_time(path, metadata):
        authoritative = path.stat(follow_symlinks=False)
        assert metadata.st_dev == authoritative.st_dev
        assert metadata.st_ino == authoritative.st_ino
        seen.append(path)
        return 123456789

    monkeypatch.setattr(room_store_operations, "_WINDOWS_HOST", True)
    monkeypatch.setattr(room_store_operations.os, "scandir", windows_scandir)
    monkeypatch.setattr(
        room_store_operations, "_windows_change_time_ns", native_change_time
    )
    scan = room_store_operations._scan_workspace(workspace, None)
    assert scan.paths == frozenset({"nested", "nested/file.txt"})
    assert scan.logical_bytes == len(b"synthetic")
    assert seen == [file]


def test_windows_scan_still_rejects_authoritative_cross_device_metadata(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "file.txt"
    file.write_bytes(b"synthetic")
    native_stat = Path.stat

    def cross_device_stat(path, **kwargs):
        metadata = native_stat(path, **kwargs)
        if path != file:
            return metadata
        return SimpleNamespace(
            st_dev=metadata.st_dev + 1,
            st_mode=metadata.st_mode,
            st_size=metadata.st_size,
        )

    monkeypatch.setattr(room_store_operations, "_WINDOWS_HOST", True)
    monkeypatch.setattr(Path, "stat", cross_device_stat)
    with pytest.raises(RoomStoreOperationsError, match="filesystem boundary"):
        room_store_operations._scan_workspace(workspace, None)


def test_windows_workspace_signature_uses_native_change_time(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file_path = workspace / "file.txt"
    file_path.write_text("hello", encoding="utf-8")
    observed = []

    change_times = iter((123456789, 987654321))

    def change_time(path, expected_stat):
        observed.append((path, expected_stat))
        return next(change_times)

    monkeypatch.setattr(room_store_operations, "_WINDOWS_HOST", True)
    monkeypatch.setattr(room_store_operations, "_windows_change_time_ns", change_time)

    first = room_store_operations._scan_workspace(workspace, None)
    second = room_store_operations._scan_workspace(workspace, None)

    assert observed and observed[0][0] == file_path
    assert observed[0][1].st_ino > 0
    assert first.signature != second.signature


def assert_marker_path(stage, final, descriptor):
    assert not final.exists()
    assert stage.parent.parent == final.parent
    (stage / ".josh-room.json").write_text(
        descriptor.to_dict()["logical_jat_id"], encoding="utf-8"
    )


def test_incomplete_restic_backup_never_publishes_or_records_an_orphan(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    store.backup = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        ResticStoreError(
            ResticStoreErrorCode.INCOMPLETE_BACKUP,
            exit_code=3,
            orphan_snapshot_id=SNAPSHOT_ID,
        )
    )
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)

    with pytest.raises(ResticStoreError):
        operations.save()

    assert catalog.published == []
    assert catalog.marker_rows == []


def test_uncertain_catalog_failure_keeps_marker_unchanged(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    catalog = _Catalog()

    def uncertain(_descriptor, *, expected_etag):
        raise OSError("transport failed")

    catalog.publish = uncertain
    operations = _operations(tmp_path, workspace, store, catalog)

    with pytest.raises(RoomStorePublicationError) as failure:
        operations.save()

    assert failure.value.publication_state == "uncertain"
    assert failure.value.reconciliation_required
    assert catalog.marker_rows == []


def test_marker_failure_after_catalog_commit_reports_committed_stale_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    catalog = _Catalog()

    def fail_marker(*_args, **_kwargs):
        raise OSError("marker replacement failed")

    catalog.write_marker = fail_marker
    operations = _operations(tmp_path, workspace, store, catalog)

    with pytest.raises(RoomStorePublicationError) as failure:
        operations.save()

    assert len(catalog.published) == 1
    assert failure.value.publication_state == "committed-marker-stale"
    assert failure.value.marker_state == "stale"


def test_preview_requires_confirmation_for_mass_deletion_and_reports_scanned_bytes(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("12345", encoding="utf-8")
    previous_entries = [
        SnapshotEntry(f"removed-{index}.txt", "file", 2, 0o600, None)
        for index in range(25)
    ]
    previous_entries.append(SnapshotEntry("keep.txt", "file", 5, 0o600, None))
    parent = _descriptor()
    store = _Store(entries=previous_entries)
    catalog = _Catalog(parent)
    operations = _operations(tmp_path, workspace, store, catalog, binding=REPOSITORY_ID)
    ensure_keyset = operations.ensure_keyset
    operations.ensure_keyset = lambda *_args: pytest.fail(
        "preview must not enroll a Room Store keyset"
    )

    preview = operations.preview()

    assert preview.scanned_bytes == 5
    assert preview.restic_added_bytes is None
    assert len(preview.deleted_paths) == 25
    assert preview.deletion_confirmation_token
    assert store.initialized is False
    operations.ensure_keyset = ensure_keyset
    operations.resolve_components = lambda *_args: pytest.fail(
        "unconfirmed mass deletion must not capture any component"
    )

    with pytest.raises(RoomStoreOperationsError, match="confirmation token") as failure:
        operations.save()
    assert (
        failure.value.deletion_confirmation_token == preview.deletion_confirmation_token
    )
    assert store.parents == []


def test_edit_during_publication_is_reported_as_saved_but_dirty(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)

    def publish(descriptor, *, expected_etag, workspace_signature, signature_algorithm):
        (workspace / "file.txt").write_text("edited after backup", encoding="utf-8")
        catalog.publish(
            descriptor,
            expected_etag=expected_etag,
            workspace_signature=workspace_signature,
            signature_algorithm=signature_algorithm,
        )

    operations.publish_descriptor = publish

    result = operations.save()

    assert result.status == "saved-but-dirty"
    assert catalog.marker_rows[0][1] is False


def test_sigterm_during_catalog_commit_finalizes_marker_before_cancellation(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)

    @contextmanager
    def cancel_after_commit():
        yield
        raise CLICancelled()

    monkeypatch.setattr(
        room_store_operations, "defer_sigterm_cancellation", cancel_after_commit
    )

    with pytest.raises(CLICancelled) as cancelled:
        operations.save()

    assert catalog.order == ["publish", "marker"]
    assert cancelled.value.result["publication_state"] == "committed"
    assert cancelled.value.result["marker_state"] == "updated"
    assert cancelled.value.result["signature_algorithm"] == "josh-room-stat-v1"


def test_restic_password_is_a_private_transient_ascii_file(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store = _Store()
    catalog = _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)
    captured = {}

    def create_store(**kwargs):
        password_file = kwargs["password_file"]
        verify_private_path(password_file, directory=False)
        captured.update(
            path=password_file,
            content=password_file.read_bytes(),
            mode=stat.S_IMODE(password_file.stat().st_mode),
        )
        return store

    operations.store_factory = create_store

    operations.save()

    assert captured["content"] == PASSWORD.encode("ascii") + b"\n"
    if os.name != "nt":
        assert captured["mode"] == 0o600
    assert not captured["path"].exists()
    assert workspace not in captured["path"].parents


def test_native_root_entry_and_symlink_chain_through_parent_directories_are_safe():
    entries = _validate_snapshot_entries(
        [
            SnapshotEntry(".", "dir", 0, 0o755, None),
            SnapshotEntry("real", "dir", 0, 0o755, None),
            SnapshotEntry("real/sub", "dir", 0, 0o755, None),
            SnapshotEntry("real/sub/file.txt", "file", 5, 0o644, None),
            SnapshotEntry("alias", "symlink", 4, 0o777, "real"),
            SnapshotEntry("through-parent", "symlink", 16, 0o777, "alias/sub/file.txt"),
        ]
    )

    assert entries["."].entry_type == "dir"


def test_snapshot_symlink_cycle_and_special_entries_fail_closed():
    with pytest.raises(RoomStoreOperationsError, match="cyclic symlink"):
        _validate_snapshot_entries(
            [
                SnapshotEntry(".", "dir", 0, 0o755, None),
                SnapshotEntry("a", "symlink", 1, 0o777, "b"),
                SnapshotEntry("b", "symlink", 1, 0o777, "a"),
            ]
        )

    with pytest.raises(RoomStoreOperationsError, match="special file"):
        _validate_snapshot_entries([SnapshotEntry("socket", "socket", 0, 0o600, None)])


@pytest.mark.skipif(os.name == "nt", reason="FIFO creation is POSIX-only")
def test_nfc_casefold_collisions_and_special_workspace_files_fail_closed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "é.txt").write_text("one", encoding="utf-8")
    (workspace / "e\u0301.TXT").write_text("two", encoding="utf-8")

    with pytest.raises(RoomStoreOperationsError, match="conflict on Windows"):
        room_store_operations._scan_workspace(workspace, None)

    (workspace / "é.txt").unlink()
    (workspace / "e\u0301.TXT").unlink()
    os.mkfifo(workspace / "special.fifo")
    with pytest.raises(RoomStoreOperationsError, match="special file"):
        room_store_operations._scan_workspace(workspace, None)


def test_device_boundary_validation_is_fail_closed():
    _require_same_device(42, 42)
    with pytest.raises(RoomStoreOperationsError, match="filesystem boundary"):
        _require_same_device(42, 43)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires pinned restic")
def test_real_local_restic_save_noop_incremental_restore_vertical(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "file.txt"
    source.write_text("first version\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    cache_dir = tmp_path / "cache"
    private_dir.mkdir(mode=0o700)
    cache_dir.mkdir(mode=0o700)
    protect_private_directory(private_dir)
    protect_private_directory(cache_dir)
    repository = tmp_path / "repository"
    keyset = _Keyset(_RoomStoreSecret())
    state = {"latest": None, "etag": "first", "workspace_signature": None}
    markers = []

    def bind(_dimension, _backend, repository_id, *, expected_generation):
        assert expected_generation == keyset.room_store.generation
        keyset.room_store.repository_id = repository_id
        return keyset

    def publish(
        descriptor,
        *,
        expected_etag,
        workspace_signature,
        signature_algorithm,
    ):
        assert expected_etag == state["etag"]
        assert signature_algorithm == "josh-room-stat-v1"
        assert len(workspace_signature) == 64
        state["latest"] = descriptor
        state["workspace_signature"] = workspace_signature
        state["etag"] = f"etag-{len(markers) + 1}"

    operations = RoomStoreOperations(
        workspace=workspace,
        repository=repository,
        cache_dir=cache_dir,
        password_dir=private_dir,
        dimension=object(),
        backend=object(),
        ensure_keyset=lambda *_args: keyset,
        bind_repository=bind,
        read_latest=lambda: (state["latest"], state["etag"]),
        read_catalog_signature=lambda: (
            (
                state["workspace_signature"],
                "josh-room-stat-v1",
                state["latest"].to_dict()["capture_policy_sha256"],
            )
            if state["latest"] is not None and state["workspace_signature"] is not None
            else None
        ),
        read_snapshot_entries=lambda _snapshot_id: (),
        publish_descriptor=publish,
        write_marker=lambda *args, **kwargs: markers.append((args, kwargs)),
        descriptor_metadata={
            "dimension_id": "dimension-test",
            "encryption_domain_id": "domain-test",
            "room_id": "room-test",
            "components": {
                "rcc_environment": None,
                "homebrew_recovery": None,
                "hauler_content": None,
            },
            "source": {},
            "producer": {
                "josh_room_version": "0.1.26",
                "restic_version": "0.19.1",
                "source_platform": "linux-x64",
                "restore_platforms": ["linux-x64", "win32-x64"],
            },
        },
        secure_private_file=secure_private_file,
        validate_private_directory=validate_private_directory,
    )

    first = operations.save()
    unchanged = operations.save()
    source.write_text("second version with another block\n", encoding="utf-8")
    second = operations.save()
    restored = tmp_path / "restored-first"
    operations.restore(
        first.descriptor,
        restored,
        write_restore_marker=lambda stage, _target, _descriptor: (
            stage / ".josh-room.json"
        ).write_text("staged marker", encoding="utf-8"),
    )

    assert first.status == "saved"
    assert first.descriptor is not None
    assert unchanged.status == "already-saved"
    assert unchanged.descriptor is first.descriptor
    assert unchanged.data_added_bytes == 0
    assert second.status == "saved"
    assert second.descriptor is not None
    assert (
        second.descriptor.to_dict()["workspace"]["parent_snapshot_id"]
        == first.snapshot_id
    )
    assert (restored / "file.txt").read_text(encoding="utf-8") == "first version\n"
    assert len(markers) == 2


@pytest.mark.parametrize("upgraded", [False, True])
def test_headless_save_uses_remote_keyset_without_native_secret_cache(tmp_path, monkeypatch, upgraded):
    from test_minio_encryption_flow import DOMAIN_ID, dimension
    from test_room_store_material import RoomStoreBackend, v1_keyset

    from josh_room import auth, keyring
    from josh_room.encryption_domain import EncryptionKeyset

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("hello", encoding="utf-8")
    store, catalog = _Store(), _Catalog()
    operations = _operations(tmp_path, workspace, store, catalog)
    original = v1_keyset()
    if upgraded:
        original = original.upgrade_for_room_store()
    backend = RoomStoreBackend(control=original.to_json())
    operations.dimension = dimension(encryption_domain_id=DOMAIN_ID)
    operations.backend = backend
    operations.ensure_keyset = auth.ensure_room_store_keyset
    operations.bind_repository = auth.bind_room_store_repository
    monkeypatch.setattr(keyring, "available", lambda: False)
    monkeypatch.setattr(keyring, "secure_store", lambda *_args: pytest.fail("no fallback write"))

    result = operations.save()

    assert result.status == "saved"
    assert catalog.order == ["publish", "marker"]
    assert EncryptionKeyset.from_json(backend.control).room_store.repository_id == REPOSITORY_ID
    assert not list((tmp_path / "private").iterdir())
