import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import Catalog, CatalogConflict, CatalogFile
from .crypto import decrypt, decrypt_file, encrypt, encrypt_file
from .encryption_domain import (
    MIGRATION_JOURNAL_KEY,
    EncryptionKeyset,
    EncryptionMaterial,
)
from .envelope import build_envelope_file, read_envelope_file, verify_envelope_file
from .jat import run_build, run_restore, run_serve
from .local_store import ImmutableLocalStore, ObjectRef
from .progress import report_progress
from .workspace_state import (
    read_workspace_marker,
    workspace_fingerprint,
    write_workspace_marker,
)


class CopyPublicationError(RuntimeError, ValueError):
    def __init__(self, cause: BaseException, receipt: Path):
        self.result = {
            "ok": False,
            "error_type": type(cause).__name__,
            "orphan_receipt": str(receipt),
        }
        super().__init__(f"{cause}; orphan receipt: {receipt}")


class SavePublicationError(RuntimeError):
    def __init__(self, cause: BaseException, marker: Path):
        self.result = {
            "ok": False,
            "publication_state": "published_verification_unknown",
            "marker_state": "committed",
            "marker": str(marker),
        }
        super().__init__(f"{cause}; catalog publication may be visible; marker committed: {marker}")


def create_snapshot(
    instance: Path,
    project_id: str,
    source: Path,
    jat_root: Path,
    recipients: list[str],
    backend=None,
    display_name: str | None = None,
    images: list[str] | None = None,
    all_images: bool = False,
    *,
    selected_material: EncryptionMaterial | None = None,
    selected_domain: EncryptionKeyset | None = None,
) -> dict:
    recipients = _selected_recipients(recipients, selected_material)
    instance.mkdir(parents=True, exist_ok=True)
    source_fingerprint = workspace_fingerprint(source)
    with tempfile.TemporaryDirectory(dir=instance) as work:
        haul = Path(work) / "payload.haul.tar.zst"
        report_progress("build", "Building portable Room haul")
        producer = run_build(jat_root, source, haul, images=images, all_images=all_images, rcc_environment="auto")
        payload_size, payload_digest = _file_metadata(haul)
        manifest = {"format_version": 1, "project_id": project_id, "snapshot_id": _snapshot_id(), "created_at": datetime.now(UTC).isoformat(), "payload": {"format": "jat-hauler", "sha256": payload_digest, "size": payload_size, "producer_version": producer["version"]}, "source": _source_metadata(source, source_fingerprint)}
        if isinstance(producer.get("environment_artifact"), dict):
            manifest["environment_artifact"] = producer["environment_artifact"]
        envelope = Path(work) / "snapshot.jroom"
        report_progress("package", "Packaging the trusted snapshot envelope")
        build_envelope_file(manifest, haul, envelope)
        encrypted = Path(work) / "snapshot.jroom.age"
        report_progress("encrypt", "Encrypting Room with age")
        encrypt_file(envelope, recipients, encrypted)
        ciphertext_size, ciphertext_digest = _file_metadata(encrypted)
        if workspace_fingerprint(source) != source_fingerprint:
            raise ValueError("source workspace changed during snapshot capture")
        if backend:
            report_progress("upload", "Uploading encrypted Room to the selected storage Dimension")
            ref = backend.put_file(f"objects/sha256/{ciphertext_digest}", encrypted)
        else:
            report_progress("store", "Writing encrypted Room to local storage")
            ref = ImmutableLocalStore(instance).put_file(encrypted)
        if ref.sha256 != ciphertext_digest or ref.size != ciphertext_size:
            raise ValueError("published ciphertext metadata mismatch")
        catalog_path = instance / "catalog.jroom.age"
        dimension_id = getattr(getattr(backend, "config", None), "dimension_id", None) if backend else None
        encryption_domain_id = _selected_domain_id(selected_material, selected_domain)
        identity_value = str(selected_material.identity) if selected_material else os.environ.get("JOSH_ROOM_IDENTITY")
        marker_path = source / ".josh-room.json"
        previous_marker = None
        marker_written = False
        try:
            if marker_path.is_file():
                with marker_path.open("rb") as marker_file:
                    previous_marker = marker_file.read()
            if workspace_fingerprint(source) != source_fingerprint:
                raise ValueError("source workspace changed during snapshot capture")
            report_progress("catalog", "Loading encrypted Room catalog")
            if backend:
                catalog, catalog_etag = _read_remote_catalog(backend, identity_value, instance, dimension_id, encryption_domain_id)
            else:
                catalog_file = CatalogFile(catalog_path, Path(identity_value) if identity_value else None, dimension_id)
                catalog = catalog_file.read()
                catalog_etag = None
            observed_revision = catalog.body["revision"]
            if workspace_fingerprint(source) != source_fingerprint:
                raise ValueError("source workspace changed during snapshot capture")
            snapshot_record = {"snapshot_id": manifest["snapshot_id"], "origin_project_id": project_id, "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "created_at": manifest["created_at"]}
            if dimension_id:
                snapshot_record["workspace_fingerprint"] = source_fingerprint
            catalog = catalog.add_snapshot(project_id, display_name or _display_name(project_id), snapshot_record)
            if workspace_fingerprint(source) != source_fingerprint:
                raise ValueError("source workspace changed during snapshot capture")
            _write_room_marker(source, project_id, display_name or _display_name(project_id), dimension_id=dimension_id, snapshot_id=manifest["snapshot_id"], workspace_fp=source_fingerprint)
            marker_written = True
            report_progress("catalog", "Publishing the new latest Room snapshot")
            if backend:
                backend.conditional_catalog_put(_encrypt_catalog(catalog, recipients, instance), catalog_etag)
            else:
                catalog_file.update_if_revision(observed_revision, catalog, recipients)
        except BaseException as error:
            published = bool(getattr(error, "published", False))
            if marker_written and not published:
                _restore_marker(marker_path, previous_marker)
            if backend and not published:
                backend.record_orphan(ref)
            if published:
                raise SavePublicationError(error, marker_path) from error
            raise
        report_progress("complete", "Room saved safely")
        return {"project_id": project_id, "snapshot_id": manifest["snapshot_id"], "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "producer": producer}


def hydrate(instance: Path, project_id: str, destination: Path, identity: Path, jat_root: Path, backend=None, snapshot_id: str = "latest", *, selected_material: EncryptionMaterial | None = None, selected_domain: EncryptionKeyset | None = None) -> dict:
    identity = _selected_identity(identity, selected_material)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError("destination must be empty or absent")
    destination.parent.mkdir(parents=True, exist_ok=True)
    operation_id = uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.josh-room-", dir=destination.parent))
    receipt = instance / "receipts" / f"{operation_id}.json"
    try:
        report_progress("catalog", "Loading encrypted Room catalog")
        if backend:
            domain_id = _selected_domain_id(selected_material, selected_domain)
            if domain_id:
                catalog, _catalog_etag = _read_remote_catalog(backend, identity, instance, encryption_domain_id=domain_id)
            else:
                catalog, _catalog_etag = _read_remote_catalog(backend, identity, instance)
        else:
            catalog = CatalogFile(instance / "catalog.jroom.age", identity).read()
        project = catalog.body["projects"][project_id]
        snapshot = catalog.resolve_snapshot(project_id, snapshot_id)
        report_progress("download", "Downloading encrypted Room snapshot")
        if backend:
            encrypted = stage / "snapshot.jroom.age"
            backend.download_file(snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"])
        else:
            encrypted = stage / "snapshot.jroom.age"
            ImmutableLocalStore(instance).download_file(
                snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"]
            )
        envelope = stage / "snapshot.jroom"
        report_progress("decrypt", "Decrypting Room with age")
        decrypt_file(encrypted, [identity], envelope)
        haul = stage / "payload.haul.tar.zst"
        report_progress("verify", "Verifying the trusted snapshot envelope")
        manifest = read_envelope_file(envelope, haul)
        if not _manifest_matches_snapshot(manifest, project_id, snapshot):
            raise ValueError("manifest project mismatch")
        workspace_stage = stage / "restore"
        report_progress("restore", "Restoring workspace through JAT and RCC")
        jat_result = run_restore(jat_root, haul, workspace_stage)
        workspace_wrapper = workspace_stage / "workspace"
        workspace_roots = list(workspace_wrapper.iterdir()) if workspace_wrapper.is_dir() else []
        if len(workspace_roots) != 1 or not workspace_roots[0].is_dir() or workspace_roots[0].is_symlink():
            raise ValueError("JAT restore did not produce an expected workspace root")
        restored_root = workspace_roots[0]
        marker_fingerprint = snapshot.get("workspace_fingerprint")
        if marker_fingerprint == "0" * 64:
            # A v1 catalog has no saved fingerprint. Hydrate is the one point
            # where trusted ciphertext has just been restored and verified, so
            # establish the authoritative baseline from those restored bytes.
            marker_fingerprint = workspace_fingerprint(restored_root)
        _write_room_marker(restored_root, project_id, project["display_name"], dimension_id=catalog.dimension_id, snapshot_id=snapshot["snapshot_id"], workspace_fp=marker_fingerprint, path_binding=destination)
        backup = None
        if destination.exists():
            backup = destination.parent / f".{destination.name}.josh-room-backup-{operation_id}"
            os.replace(destination, backup)
            if any(backup.iterdir()):
                os.replace(backup, destination)
                raise FileExistsError("destination became non-empty before promotion")
        try:
            report_progress("promote", "Promoting restored workspace atomically")
            os.replace(restored_root, destination)
        except BaseException:
            if backup and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup and backup.exists():
            backup.rmdir()
        result = {"project_id": project_id, "snapshot_id": snapshot["snapshot_id"], "destination": str(destination), "receipt": str(receipt), "jat": jat_result}
        _write_receipt(receipt, {"operation": "hydrate", "operation_id": operation_id, "status": "success", **result})
        report_progress("complete", "Room restored and ready to open")
        return result
    except BaseException as error:
        failure = {"operation": "hydrate", "operation_id": operation_id, "status": "failed", "error_type": type(error).__name__}
        if getattr(error, "result", None):
            failure["jat"] = error.result
        _write_receipt(receipt, failure)
        raise
    finally:
        if stage.exists() and stage.is_dir() and not stage.is_symlink():
            shutil.rmtree(stage)


def remove_room(instance: Path, project_id: str, identity: Path, recipients: list[str], backend=None, *, selected_material: EncryptionMaterial | None = None, selected_domain: EncryptionKeyset | None = None) -> dict:
    identity = _selected_identity(identity, selected_material)
    recipients = _selected_recipients(recipients, selected_material)
    operation_id = uuid.uuid4().hex
    receipt = instance / "receipts" / f"{operation_id}.json"
    report_progress("catalog", "Loading encrypted Room catalog")
    if backend:
        domain_id = _selected_domain_id(selected_material, selected_domain)
        if domain_id:
            catalog, etag = _read_remote_catalog(backend, identity, instance, encryption_domain_id=domain_id)
        else:
            catalog, etag = _read_remote_catalog(backend, identity, instance)
    else:
        catalog_file = CatalogFile(instance / "catalog.jroom.age", identity)
        catalog = catalog_file.read()
        etag = None
    updated, removable, snapshot_count = catalog.remove_project(project_id)
    report_progress("catalog", "Removing Room from the encrypted catalog")
    if backend:
        backend.conditional_catalog_put(_encrypt_catalog(updated, recipients, instance), etag)
    else:
        catalog_file.update_if_revision(catalog.body["revision"], updated, recipients)
    cleanup_failed = []
    report_progress("cleanup", "Removing unreferenced encrypted snapshots")
    for key in removable:
        try:
            if backend:
                backend.delete_object(key)
            else:
                ImmutableLocalStore(instance).delete(key)
        except Exception:  # noqa: BLE001 - catalog removal is durable; cleanup is retried from receipt
            cleanup_failed.append(key)
    result = {
        "deleted_objects": len(removable) - len(cleanup_failed),
        "project_id": project_id,
        "snapshot_count": snapshot_count,
    }
    if cleanup_failed:
        result["cleanup_pending"] = len(cleanup_failed)
    _write_receipt(receipt, {"operation": "remove-room", "operation_id": operation_id, "status": "success", **result})
    report_progress("complete", "Room removed")
    return result


def remove_snapshot(
    instance: Path,
    project_id: str,
    snapshot_id: str,
    identity: Path,
    recipients: list[str],
    backend=None,
    *,
    selected_material: EncryptionMaterial | None = None,
    selected_domain: EncryptionKeyset | None = None,
) -> dict:
    identity = _selected_identity(identity, selected_material)
    recipients = _selected_recipients(recipients, selected_material)
    operation_id = uuid.uuid4().hex
    receipt = instance / "receipts" / f"{operation_id}.json"
    report_progress("catalog", "Loading encrypted Room catalog")
    if backend:
        domain_id = _selected_domain_id(selected_material, selected_domain)
        if domain_id:
            catalog, etag = _read_remote_catalog(backend, identity, instance, encryption_domain_id=domain_id)
        else:
            catalog, etag = _read_remote_catalog(backend, identity, instance)
    else:
        catalog_file = CatalogFile(instance / "catalog.jroom.age", identity)
        catalog = catalog_file.read()
        etag = None
    latest_promoted = catalog.body["projects"][project_id]["latest"] == snapshot_id
    updated, removable, room_removed = catalog.remove_snapshot(project_id, snapshot_id)
    report_progress("catalog", "Removing selected recovery point")
    if backend:
        backend.conditional_catalog_put(_encrypt_catalog(updated, recipients, instance), etag)
    else:
        catalog_file.update_if_revision(catalog.body["revision"], updated, recipients)
    cleanup_failed = []
    for key in removable:
        try:
            if backend:
                backend.delete_object(key)
            else:
                ImmutableLocalStore(instance).delete(key)
        except Exception:  # noqa: BLE001 - catalog removal is durable; cleanup is recorded for retry
            cleanup_failed.append(key)
    result = {
        "deleted_objects": len(removable) - len(cleanup_failed),
        "latest": None if room_removed else updated.body["projects"][project_id]["latest"],
        "latest_promoted": latest_promoted,
        "project_id": project_id,
        "room_removed": room_removed,
        "snapshot_id": snapshot_id,
    }
    if cleanup_failed:
        result["cleanup_pending"] = len(cleanup_failed)
    _write_receipt(receipt, {"operation": "remove-snapshot", "operation_id": operation_id, "status": "success", **result})
    report_progress("complete", "Recovery point removed")
    return result


def serve_snapshot(instance: Path, project_id: str, snapshot_id: str, identity: Path, jat_root: Path, backend=None, *, selected_material: EncryptionMaterial | None = None, selected_domain: EncryptionKeyset | None = None) -> dict:
    identity = _selected_identity(identity, selected_material)
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serve-", dir=instance) as work:
        stage = Path(work)
        report_progress("catalog", "Loading encrypted Room catalog")
        if backend:
            domain_id = _selected_domain_id(selected_material, selected_domain)
            if domain_id:
                catalog, _etag = _read_remote_catalog(backend, identity, instance, encryption_domain_id=domain_id)
            else:
                catalog, _etag = _read_remote_catalog(backend, identity, instance)
        else:
            catalog = CatalogFile(instance / "catalog.jroom.age", identity).read()
        snapshot = catalog.resolve_snapshot(project_id, snapshot_id)
        encrypted = stage / "snapshot.jroom.age"
        report_progress("download", "Downloading encrypted Room snapshot")
        if backend:
            backend.download_file(snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"])
        else:
            ImmutableLocalStore(instance).download_file(
                snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"]
            )
        envelope = stage / "snapshot.jroom"
        report_progress("decrypt", "Decrypting Room with age")
        decrypt_file(encrypted, [identity], envelope)
        haul = stage / "payload.haul.tar.zst"
        report_progress("verify", "Verifying the trusted snapshot envelope")
        manifest = read_envelope_file(envelope, haul)
        if not _manifest_matches_snapshot(manifest, project_id, snapshot):
            raise ValueError("manifest project mismatch")
        report_progress("serve", "Starting read-only Hauler registry")
        return run_serve(jat_root, haul)


def _write_receipt(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json_body = json.dumps(body, sort_keys=True)
            handle.write(json_body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _write_room_marker(workspace: Path, project_id: str, display_name: str, *, dimension_id: str | None = None, snapshot_id: str | None = None, workspace_fp: str | None = None, path_binding: Path | None = None) -> None:
    if dimension_id and snapshot_id and workspace_fp and workspace_fp != "0" * 64:
        write_workspace_marker(workspace, dimension_id=dimension_id, project_id=project_id, display_name=display_name, snapshot_id=snapshot_id, workspace_fingerprint=workspace_fp, path_binding=path_binding)
        return
    marker = workspace / ".josh-room.json"
    marker.write_text(json.dumps({"display_name": display_name, "format_version": 1, "project_id": project_id}, sort_keys=True) + "\n")


def _restore_marker(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(previous)


def _read_remote_catalog(backend, identity_value, instance: Path, dimension_id: str | None = None, encryption_domain_id: str | None = None):
    dimension_id = dimension_id or getattr(getattr(backend, "config", None), "dimension_id", None)
    encrypted, etag = backend.read_catalog()
    if encrypted is None:
        return Catalog.empty(dimension_id, encryption_domain_id), None
    with tempfile.NamedTemporaryFile(prefix=".catalog-read.", delete=False) as handle:
        path = Path(handle.name)
        handle.write(encrypted)
    try:
        return Catalog.from_body(json.loads(decrypt(path, [Path(identity_value)])), dimension_id, encryption_domain_id), etag
    finally:
        path.unlink(missing_ok=True)


def _encrypt_catalog(catalog: Catalog, recipients: list[str], instance: Path) -> bytes:
    with tempfile.NamedTemporaryFile(prefix=".catalog-write.", delete=False) as handle:
        path = Path(handle.name)
    try:
        encrypt(json.dumps(catalog.body, sort_keys=True).encode(), recipients, path)
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _selected_identity(identity: Path, selected_material: EncryptionMaterial | None) -> Path:
    return Path(selected_material.identity) if selected_material else Path(identity)


def _selected_recipients(recipients: list[str], selected_material: EncryptionMaterial | None) -> list[str]:
    if selected_material is None:
        return recipients
    return [selected_material.recipient, *selected_material.keyset.recovery_recipients]


def _selected_domain_id(selected_material: EncryptionMaterial | None, selected_domain: EncryptionKeyset | None) -> str | None:
    if selected_material is not None:
        return selected_material.encryption_domain_id
    return selected_domain.encryption_domain_id if selected_domain is not None else os.environ.get("JOSH_ROOM_SELECTED_DOMAIN")


def _snapshot_id() -> str:
    return uuid.uuid4().hex


def _display_name(project_id: str) -> str:
    return project_id.replace("-", " ").replace("_", " ").title()


def _source_metadata(source: Path, workspace_fingerprint_value: str | None = None) -> dict:
    commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if commit.returncode != 0:
        metadata = {}
        if workspace_fingerprint_value:
            metadata["workspace_fingerprint"] = workspace_fingerprint_value
        return metadata
    status = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True, check=False)
    if status.returncode != 0:
        metadata = {"git_commit": commit.stdout.strip()}
    else:
        metadata = {"git_commit": commit.stdout.strip(), "dirty": bool(status.stdout)}
    if workspace_fingerprint_value:
        metadata["workspace_fingerprint"] = workspace_fingerprint_value
    return metadata


def _file_metadata(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()



def _evidence_matches(snapshot: dict, object_evidence: dict | None) -> bool:
    if not isinstance(object_evidence, dict):
        return False
    for field in ("snapshot_id", "object_key", "ciphertext_sha256", "ciphertext_size"):
        if field in object_evidence and object_evidence[field] != snapshot.get(field):
            return False
    return all(field in object_evidence for field in ("snapshot_id", "ciphertext_sha256", "ciphertext_size"))


def _workspace_binding(workspace: Path, catalog: Catalog, object_evidence: dict | None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None, verified_workspace_fingerprint: str | None = None):
    workspace = Path(workspace)
    if not object_evidence:
        raise ValueError("object evidence is required")
    marker = None
    marker_path = workspace / ".josh-room.json"
    explicit = any(value is not None for value in (project_id, snapshot_id, dimension_id))
    if marker_path.is_file():
        try:
            marker = read_workspace_marker(workspace)
        except ValueError:
            if not explicit:
                raise
    project_id = project_id or (object_evidence or {}).get("project_id") or (marker or {}).get("project_id")
    snapshot_id = snapshot_id or (object_evidence or {}).get("snapshot_id") or (marker or {}).get("snapshot_id")
    dimension_id = dimension_id or (marker or {}).get("dimension_id") or catalog.dimension_id
    if not project_id or not snapshot_id or not dimension_id:
        raise ValueError("catalog evidence does not identify workspace")
    if catalog.dimension_id != dimension_id:
        raise ValueError("catalog Dimension mismatch")
    snapshot = catalog.resolve_snapshot(project_id, snapshot_id)
    if not _evidence_matches(snapshot, object_evidence):
        raise ValueError("object evidence does not corroborate catalog")
    expected_fingerprint = snapshot.get("workspace_fingerprint")
    if expected_fingerprint == "0" * 64:
        # Legacy catalog migration uses the zero digest only as a sentinel.
        # A v2 marker written by verified hydrate may supply the real baseline;
        # without that marker, missing-ledger Repair remains fail-closed.
        marker_fingerprint = (marker or {}).get("workspace_fingerprint")
        if (marker or {}).get("format_version") == 2 and marker_fingerprint != "0" * 64:
            expected_fingerprint = marker_fingerprint
    if expected_fingerprint in {None, "0" * 64} and verified_workspace_fingerprint is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", verified_workspace_fingerprint) or verified_workspace_fingerprint == "0" * 64:
            raise ValueError("verified workspace fingerprint is invalid")
        expected_fingerprint = verified_workspace_fingerprint
    if not expected_fingerprint:
        raise ValueError("catalog workspace fingerprint is unavailable")
    if expected_fingerprint == "0" * 64:
        raise ValueError("legacy catalog workspace fingerprint is unavailable")
    if workspace_fingerprint(workspace) != expected_fingerprint:
        raise ValueError("workspace fingerprint does not corroborate catalog")
    if marker and not explicit:
        if marker.get("format_version") != 2:
            raise ValueError("workspace marker v2 is required")
        if marker.get("dimension_id") != dimension_id or marker.get("project_id") != project_id or marker.get("snapshot_id") != snapshot_id:
            raise ValueError("workspace marker does not corroborate catalog")
        if marker.get("workspace_fingerprint") != expected_fingerprint:
            raise ValueError("workspace fingerprint does not corroborate catalog")
        from .workspace_state import canonical_workspace_path_sha256
        if marker.get("workspace_path_sha256") != canonical_workspace_path_sha256(workspace):
            raise ValueError("workspace path does not match marker")
    if snapshot.get("workspace_fingerprint") != expected_fingerprint:
        snapshot = {**snapshot, "workspace_fingerprint": expected_fingerprint}
    return marker, project_id, snapshot_id, dimension_id, snapshot


def link_workspace(workspace: Path, catalog: Catalog, object_evidence: dict | None = None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None, verified_workspace_fingerprint: str | None = None) -> dict:
    _marker, project_id, snapshot_id, dimension_id, snapshot = _workspace_binding(workspace, catalog, object_evidence, project_id=project_id, snapshot_id=snapshot_id, dimension_id=dimension_id, verified_workspace_fingerprint=verified_workspace_fingerprint)
    display_name = catalog.body["projects"][project_id]["display_name"]
    write_workspace_marker(workspace, dimension_id=dimension_id, project_id=project_id, display_name=display_name, snapshot_id=snapshot_id, workspace_fingerprint=snapshot["workspace_fingerprint"])
    return {"ok": True, "dimension_id": dimension_id, "project_id": project_id, "snapshot_id": snapshot_id, "marker": read_workspace_marker(workspace)}


def repair_workspace(workspace: Path, catalog: Catalog, object_evidence: dict | None = None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None) -> dict:
    if not catalog.body.get("projects"):
        raise ValueError("catalog evidence is required")
    _marker, project_id, snapshot_id, dimension_id, snapshot = _workspace_binding(workspace, catalog, object_evidence, project_id=project_id, snapshot_id=snapshot_id, dimension_id=dimension_id)
    write_workspace_marker(workspace, dimension_id=dimension_id, project_id=project_id, display_name=catalog.body["projects"][project_id]["display_name"], snapshot_id=snapshot_id, workspace_fingerprint=snapshot["workspace_fingerprint"])
    return {"ok": True, "dimension_id": dimension_id, "project_id": project_id, "snapshot_id": snapshot_id}


def _record_orphan(store, ref):
    if ref is not None and hasattr(store, "record_orphan"):
        return store.record_orphan(ref)
    return None


def _destination_identity(store) -> dict:
    config = getattr(store, "config", None)
    if config is None:
        return {"provider": "local"}
    provider = getattr(config, "provider", None)
    if provider is None:
        provider = "minio" if store.__class__.__module__.endswith("minio") else "r2"
    identity = {"provider": provider}
    for name in ("dimension_id", "endpoint", "bucket"):
        value = getattr(config, name, None)
        if isinstance(value, str) and value:
            identity[name] = value
    return identity


def _write_copy_orphan_receipt(instance: Path, ref, store, error: BaseException) -> Path:
    path = Path(instance) / "receipts" / f"orphan-copy-{uuid.uuid4().hex}.json"
    _write_receipt(path, {
        "operation": "copy",
        "status": "uploaded-unreferenced",
        "error_type": type(error).__name__,
        "destination": _destination_identity(store),
        "object_key": ref.key,
        "sha256": ref.sha256,
        "size": ref.size,
    })
    return path


def copy_snapshot(source_catalog: Catalog, destination_catalog: Catalog, source_store, destination_store, project_id: str, snapshot_id: str = "latest", destination_project_id: str | None = None) -> dict:
    """Copy verified ciphertext as a new logical JAT without decrypting it."""
    source = source_catalog.resolve_snapshot(project_id, snapshot_id)
    payload = source_store.get_bytes(source["object_key"], source["ciphertext_sha256"], source["ciphertext_size"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != source["ciphertext_sha256"] or len(payload) != source["ciphertext_size"]:
        raise ValueError("source ciphertext evidence mismatch")
    put = None
    try:
        try:
            put = destination_store.put_bytes(source["object_key"], payload)
        except Exception:  # noqa: BLE001 - a failed create-only write is corroborated by a read-back
            verified = destination_store.get_bytes(source["object_key"], source["ciphertext_sha256"], source["ciphertext_size"])
            if hashlib.sha256(verified).hexdigest() != source["ciphertext_sha256"] or len(verified) != source["ciphertext_size"]:
                raise ValueError("destination ciphertext evidence mismatch")
            put = type("ObjectRef", (), {"key": source["object_key"], "sha256": source["ciphertext_sha256"], "size": source["ciphertext_size"]})()
        if put.key != source["object_key"] or put.sha256 != source["ciphertext_sha256"] or put.size != source["ciphertext_size"]:
            raise ValueError("destination ciphertext metadata mismatch")
        new_snapshot = dict(source)
        new_snapshot["snapshot_id"] = _snapshot_id()
        new_snapshot["created_at"] = datetime.now(UTC).isoformat()
        new_snapshot["origin_project_id"] = source.get("origin_project_id", project_id)
        if destination_catalog.body["format_version"] == 2:
            new_snapshot.setdefault("workspace_fingerprint", source.get("workspace_fingerprint", "0" * 64))
        project = destination_project_id or project_id
        updated = destination_catalog.add_snapshot(project, source_catalog.body["projects"][project_id]["display_name"], new_snapshot)
        return {"ok": True, "project_id": project, "snapshot_id": new_snapshot["snapshot_id"], "object_key": new_snapshot["object_key"], "ciphertext_sha256": new_snapshot["ciphertext_sha256"], "ciphertext_size": new_snapshot["ciphertext_size"], "catalog": updated}
    except BaseException:
        _record_orphan(destination_store, put)
        raise


def _material_value(material, name, default=None):
    return getattr(material, name, default) if material is not None else default


def _domain_value(material, explicit, catalog=None):
    material_value = _material_value(material, "encryption_domain_id")
    catalog_value = getattr(catalog, "encryption_domain_id", None)
    values = [value for value in (explicit, material_value, catalog_value) if value is not None]
    if len(set(values)) > 1:
        raise ValueError("encryption domain mismatch")
    return values[0] if values else None


def _generation_value(material, explicit):
    material_value = _material_value(material, "key_generation")
    if explicit is not None and material_value is not None and explicit != material_value:
        raise ValueError("encryption generation mismatch")
    return explicit if explicit is not None else material_value


def _material_recipients(material, fallback):
    if material is None:
        return list(fallback)
    keyset = getattr(material, "keyset", None)
    return [material.recipient, *getattr(keyset, "recovery_recipients", ())]


def _material_identity(material, fallback):
    return Path(material.identity) if material is not None and getattr(material, "identity", None) else Path(fallback)


def _authority(backend, dimension, bucket=None):
    config = getattr(backend, "config", None)
    endpoint = getattr(config, "endpoint", None)
    endpoint_authority = None
    if isinstance(endpoint, str):
        endpoint_authority = urlsplit(endpoint).netloc or None
    return {
        "dimension": getattr(dimension, "dimension_id", dimension),
        "bucket": bucket or getattr(config, "bucket", None),
        "endpoint_authority": endpoint_authority,
    }


def _transport_state(*backends):
    endpoints = [getattr(getattr(backend, "config", None), "endpoint", None) for backend in backends]
    endpoints = [endpoint for endpoint in endpoints if isinstance(endpoint, str)]
    if not endpoints:
        return "unknown"
    return "secure" if all(urlsplit(endpoint).scheme == "https" for endpoint in endpoints) else "insecure"


def _journal_bytes(journal):
    return json.dumps(journal, sort_keys=True, separators=(",", ":")).encode()


def _read_journal(backend):
    body, etag = backend.read_control(MIGRATION_JOURNAL_KEY, 64 * 1024)
    if body is None:
        return None, None
    try:
        journal = json.loads(body)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("migration journal is invalid") from error
    if not isinstance(journal, dict) or journal.get("format_version") != 1:
        raise ValueError("migration journal is invalid")
    return journal, etag


def _publish_journal(backend, journal, etag=None):
    body = _journal_bytes(journal)
    if etag is None:
        return backend.create_control(MIGRATION_JOURNAL_KEY, body)
    return backend.replace_control(MIGRATION_JOURNAL_KEY, body, etag)


def _new_journal(source_catalog, source_backend, destination_backend, source_dimension, destination_dimension, source_domain_id, destination_domain_id, source_generation, destination_generation, migration_id, source_catalog_etag=None, journal_exists=False):
    seen = {}
    for project in source_catalog.body["projects"].values():
        for snapshot in project["snapshots"].values():
            digest = snapshot["ciphertext_sha256"]
            seen.setdefault(digest, {
                "source_object_key": snapshot["object_key"],
                "source_sha256": digest,
                "source_size": snapshot["ciphertext_size"],
                "status": "pending",
            })
    source = _authority(source_backend, source_dimension)
    destination = _authority(destination_backend, destination_dimension)
    now = datetime.now(UTC).isoformat()
    return {
        "format_version": 1,
        "migration_id": migration_id or uuid.uuid4().hex,
        "source_dimension": source["dimension"],
        "source_dimension_display_name": getattr(source_dimension, "display_name", None),
        "source_bucket": source["bucket"],
        "source_endpoint_authority": source["endpoint_authority"],
        "destination_dimension": destination["dimension"],
        "destination_dimension_display_name": getattr(destination_dimension, "display_name", None),
        "destination_bucket": destination["bucket"],
        "destination_endpoint_authority": destination["endpoint_authority"],
        "source_encryption_domain_id": source_domain_id,
        "destination_encryption_domain_id": destination_domain_id,
        "source_key_generation": source_generation,
        "destination_key_generation": destination_generation,
        "source_catalog_revision": source_catalog.body["revision"],
        "source_catalog_etag": source_catalog_etag,
        "room_count": len(source_catalog.body["projects"]),
        "snapshot_count": sum(len(project["snapshots"]) for project in source_catalog.body["projects"].values()),
        "object_count": len(seen),
        "total_bytes": sum(item["source_size"] for item in seen.values()),
        "largest_object_bytes": max((item["source_size"] for item in seen.values()), default=0),
        "temporary_disk_bytes": 3 * max((item["source_size"] for item in seen.values()), default=0),
        "transport_state": _transport_state(source_backend, destination_backend),
        "journal_exists": journal_exists,
        "mappings": list(seen.values()),
        "status": "planned",
        "created_at": now,
        "updated_at": now,
    }


def _safe_journal_update(backend, journal, etag, status=None, error=None):
    if status is not None:
        journal["status"] = status
    if error is not None:
        journal["error_type"] = type(error).__name__
    elif "error_type" in journal and status in {"running", "committed"}:
        journal.pop("error_type", None)
    journal["updated_at"] = datetime.now(UTC).isoformat()
    return _publish_journal(backend, journal, etag)


def plan_encryption_migration(
    source_catalog: Catalog,
    source_backend,
    destination_backend,
    *,
    source_dimension,
    destination_dimension,
    source_domain_id=None,
    destination_domain_id=None,
    source_generation=None,
    destination_generation=None,
    destination_material=None,
    source_catalog_revision=None,
    source_catalog_etag=None,
    migration_id=None,
):
    """Create and persist a non-secret, unique-object migration plan."""
    destination_domain_id = _domain_value(destination_material, destination_domain_id)
    if not destination_domain_id:
        raise ValueError("destination encryption domain is required")
    if source_catalog_revision is not None and source_catalog.body["revision"] != source_catalog_revision:
        raise CatalogConflict("stale source catalog revision")
    existing, existing_etag = _read_journal(destination_backend)
    if existing is not None and existing.get("status") not in {"committed", "cancelled", "conflict", "failed"}:
        raise RuntimeError("an encryption migration is already active")
    journal = _new_journal(
        source_catalog,
        source_backend,
        destination_backend,
        source_dimension,
        destination_dimension,
        source_domain_id or source_catalog.encryption_domain_id,
        destination_domain_id,
        source_generation,
        _generation_value(destination_material, destination_generation),
        migration_id,
        source_catalog_etag,
        existing is not None,
    )
    if existing is None:
        _publish_journal(destination_backend, journal)
    else:
        _publish_journal(destination_backend, journal, existing_etag)
    result = dict(journal)
    result["journal_status"] = result["status"]
    return result


def _migration_catalog(source_catalog, destination_catalog, mappings, destination_dimension, destination_domain_id):
    body = json.loads(json.dumps(source_catalog.body))
    body["encryption_domain_id"] = destination_domain_id
    if destination_dimension is not None:
        body["format_version"] = 2
        body["dimension_id"] = getattr(destination_dimension, "dimension_id", destination_dimension)
        for project in body["projects"].values():
            for snapshot in project["snapshots"].values():
                snapshot.setdefault("created_at", "1970-01-01T00:00:00+00:00")
                snapshot.setdefault("workspace_fingerprint", "0" * 64)
    mapping_by_source = {item["source_sha256"]: item for item in mappings}
    for project in body["projects"].values():
        for snapshot in project["snapshots"].values():
            mapping = mapping_by_source[snapshot["ciphertext_sha256"]]
            snapshot["object_key"] = mapping["destination_object_key"]
            snapshot["ciphertext_sha256"] = mapping["destination_sha256"]
            snapshot["ciphertext_size"] = mapping["destination_size"]
    return Catalog.from_body(body, body.get("dimension_id"), destination_domain_id)


def _validate_journal_mapping_set(source_catalog, journal):
    expected = {
        snapshot["ciphertext_sha256"]
        for project in source_catalog.body["projects"].values()
        for snapshot in project["snapshots"].values()
    }
    expected_bindings = {
        snapshot["ciphertext_sha256"]: (snapshot["object_key"], snapshot["ciphertext_size"])
        for project in source_catalog.body["projects"].values()
        for snapshot in project["snapshots"].values()
    }
    mappings = journal.get("mappings")
    if not isinstance(mappings, list) or len(mappings) != len(expected):
        raise ValueError("migration journal mapping set does not match source catalog")
    actual = set()
    for item in mappings:
        if not isinstance(item, dict):
            raise TypeError("migration journal mapping set does not match source catalog")
        digest = item.get("source_sha256")
        if digest in actual or expected_bindings.get(digest) != (item.get("source_object_key"), item.get("source_size")):
            raise ValueError("migration journal mapping set does not match source catalog")
        actual.add(digest)
    if actual != expected:
        raise ValueError("migration journal mapping set does not match source catalog")


def _verify_destination_object(backend, ref):
    verifier = getattr(backend, "verify_object", None) or getattr(backend, "verify", None)
    if verifier is None:
        raise ValueError("destination object verification is unavailable")
    return verifier(ref.key, ref.sha256, ref.size)


def _has_object_verifier(backend):
    return getattr(backend, "verify_object", None) or getattr(backend, "verify", None)


def _validate_journal_identity(journal, source_catalog, source_backend, source_dimension, destination_backend, destination_dimension, source_domain_id, destination_domain_id, source_generation, destination_generation, source_catalog_etag):
    expected = {
        "source_dimension": getattr(source_dimension, "dimension_id", source_dimension),
        "destination_dimension": getattr(destination_dimension, "dimension_id", destination_dimension),
        "source_encryption_domain_id": source_domain_id or source_catalog.encryption_domain_id,
        "destination_encryption_domain_id": destination_domain_id,
        "destination_key_generation": destination_generation,
    }
    for prefix, backend, dimension in (
        ("source", source_backend, source_dimension),
        ("destination", destination_backend, destination_dimension),
    ):
        authority = _authority(backend, dimension)
        expected[f"{prefix}_bucket"] = authority["bucket"]
        expected[f"{prefix}_endpoint_authority"] = authority["endpoint_authority"]
    for field, value in expected.items():
        if journal.get(field) != value:
            label = " authority" if field.endswith(("_bucket", "_endpoint_authority")) else ""
            raise ValueError(f"migration journal {field}{label} does not match the requested migration")
    if source_generation is not None and journal.get("source_key_generation") != source_generation:
        raise ValueError("migration journal source_key_generation does not match the requested migration")
    if source_catalog_etag is not None and journal.get("source_catalog_etag") not in {None, source_catalog_etag}:
        raise CatalogConflict("migration journal source catalog ETag mismatch")


def _destination_catalog_matches_migration(destination_backend, destination_identity, instance, destination_dimension, destination_domain_id, candidate):
    observed, _ = destination_backend.read_catalog()
    if observed is None:
        return False
    try:
        actual, _ = _read_remote_catalog(
            destination_backend,
            destination_identity,
            instance,
            getattr(destination_dimension, "dimension_id", destination_dimension),
            destination_domain_id,
        )
    except Exception as error:
        raise CatalogConflict("destination catalog could not be verified") from error
    if actual.body != candidate.body or actual.encryption_domain_id != destination_domain_id:
        raise CatalogConflict("destination catalog does not match migration candidate")
    return True


def _record_migration_state(destination_backend, journal, journal_etag, status, error):
    try:
        return _safe_journal_update(destination_backend, journal, journal_etag, status, error)
    except Exception:  # noqa: BLE001 - the previous non-active state is safer than masking the failure
        return journal_etag


def _record_migration_failure(destination_backend, journal, journal_etag, error):
    return _record_migration_state(destination_backend, journal, journal_etag, "failed", error)


def migrate_encryption(
    instance: Path,
    source_catalog: Catalog,
    destination_catalog=None,
    *,
    source_backend,
    destination_backend,
    source_dimension,
    destination_dimension,
    source_identity=None,
    destination_material=None,
    source_material=None,
    source_domain_id=None,
    destination_domain_id=None,
    source_generation=None,
    destination_generation=None,
    source_catalog_etag=None,
    destination_catalog_etag=None,
    resume=False,
):
    """Re-encrypt exact verified envelopes and conditionally publish one catalog."""
    instance = Path(instance)
    instance.mkdir(parents=True, exist_ok=True)
    destination_domain_id = _domain_value(destination_material, destination_domain_id, destination_catalog)
    journal, journal_etag = _read_journal(destination_backend)
    if journal is None:
        journal = plan_encryption_migration(
            source_catalog,
            source_backend,
            destination_backend,
            source_dimension=source_dimension,
            destination_dimension=destination_dimension,
            source_domain_id=source_domain_id,
            destination_domain_id=destination_domain_id,
            source_generation=source_generation,
            destination_generation=destination_generation,
            destination_material=destination_material,
            source_catalog_etag=source_catalog_etag,
        )
        journal, journal_etag = _read_journal(destination_backend)
    elif resume and journal.get("status") not in {"planned", "running", "interrupted", "ready-to-commit", "cutover-published"}:
        raise RuntimeError("migration journal is not resumable")
    elif not resume and journal.get("status") not in {"planned", "interrupted"}:
        raise RuntimeError("an encryption migration is already active")
    try:
        _validate_journal_identity(
            journal,
            source_catalog,
            source_backend,
            source_dimension,
            destination_backend,
            destination_dimension,
            source_domain_id,
            destination_domain_id,
            source_generation,
            _generation_value(destination_material, destination_generation),
            source_catalog_etag,
        )
        _validate_journal_mapping_set(source_catalog, journal)
    except BaseException as error:
        _record_migration_failure(destination_backend, journal, journal_etag, error)
        raise
    if journal.get("status") in {"ready-to-commit", "cutover-published"}:
        candidate = _migration_catalog(source_catalog, destination_catalog, journal["mappings"], destination_dimension, destination_domain_id)
        destination_identity = Path(destination_material.identity) if destination_material is not None else None
        try:
            if _destination_catalog_matches_migration(destination_backend, destination_identity, instance, destination_dimension, destination_domain_id, candidate):
                journal_etag = _safe_journal_update(destination_backend, journal, journal_etag, "committed")
                return {"ok": True, "status": "committed", "journal_status": "committed", "catalog": candidate, "migration_id": journal["migration_id"]}
            if journal.get("status") == "cutover-published":
                raise CatalogConflict("published migration catalog is unavailable")
        except CatalogConflict as error:
            _record_migration_state(destination_backend, journal, journal_etag, "conflict", error)
            raise
    try:
        source_identity = _material_identity(source_material, source_identity) if source_material is not None else (Path(source_identity) if source_identity else None)
        if source_identity is None:
            raise ValueError("source encryption identity is required")
        recipients = _material_recipients(destination_material, [])
        if len(recipients) < 2:
            raise ValueError("destination encryption recipients are required")
        required_disk = journal.get("temporary_disk_bytes", 0)
        if type(required_disk) is not int or required_disk < 0 or shutil.disk_usage(instance).free < required_disk:
            raise ValueError("insufficient temporary disk for encryption migration")
        journal_etag = _safe_journal_update(destination_backend, journal, journal_etag, "running")
    except BaseException as error:
        _record_migration_failure(destination_backend, journal, journal_etag, error)
        raise
    catalog_published = False
    try:
        with tempfile.TemporaryDirectory(prefix=f"migration-{journal['migration_id']}-", dir=instance) as work:
            work = Path(work)
            for mapping in journal["mappings"]:
                if mapping.get("status") == "verified":
                    try:
                        _verify_destination_object(destination_backend, ObjectRef(mapping["destination_object_key"], mapping["destination_sha256"], mapping["destination_size"]))
                        continue
                    except Exception:  # noqa: BLE001 - reprocess a journal mapping whose object is unavailable
                        mapping["status"] = "pending"
                encrypted = work / "source.age"
                envelope = work / "snapshot.jroom"
                destination_encrypted = work / "destination.age"
                source_backend.download_file(mapping["source_object_key"], encrypted, mapping["source_sha256"], mapping["source_size"])
                source_size, source_digest = _file_metadata(encrypted)
                if source_size != mapping["source_size"] or source_digest != mapping["source_sha256"]:
                    raise ValueError("source ciphertext metadata mismatch")
                decrypt_file(encrypted, [source_identity], envelope)
                verify_envelope_file(envelope)
                encrypt_file(envelope, recipients, destination_encrypted)
                destination_size, destination_digest = _file_metadata(destination_encrypted)
                destination_key = f"objects/sha256/{destination_digest}"
                try:
                    ref = destination_backend.put_file(destination_key, destination_encrypted)
                except Exception:  # noqa: BLE001 - create-only conflicts are corroborated by read-back
                    ref = _verify_destination_object(destination_backend, ObjectRef(destination_key, destination_digest, destination_size))
                if ref.key != destination_key or ref.sha256 != destination_digest or ref.size != destination_size:
                    raise ValueError("destination ciphertext metadata mismatch")
                if _has_object_verifier(destination_backend):
                    _verify_destination_object(destination_backend, ref)
                mapping.update({"destination_object_key": ref.key, "destination_sha256": ref.sha256, "destination_size": ref.size, "status": "verified"})
                journal_etag = _safe_journal_update(destination_backend, journal, journal_etag)
            candidate = _migration_catalog(source_catalog, destination_catalog, journal["mappings"], destination_dimension, destination_domain_id)
            if destination_catalog_etag is None and hasattr(destination_backend, "read_catalog"):
                _old_body, destination_catalog_etag = destination_backend.read_catalog()
                if isinstance(destination_catalog_etag, tuple):
                    destination_catalog_etag = destination_catalog_etag[1]
            body = _encrypt_catalog(candidate, recipients, instance)
            if hasattr(source_backend, "read_catalog"):
                _source_body, observed_source_etag = source_backend.read_catalog()
                expected_source_etag = source_catalog_etag or journal.get("source_catalog_etag")
                if expected_source_etag is not None and observed_source_etag != expected_source_etag:
                    raise CatalogConflict("stale source catalog revision")
                if _source_body is not None and source_identity is not None:
                    observed_catalog, _ = _read_remote_catalog(
                        source_backend,
                        source_identity,
                        instance,
                        getattr(source_dimension, "dimension_id", source_dimension),
                        source_domain_id or source_catalog.encryption_domain_id,
                    )
                    if observed_catalog.body["revision"] != journal["source_catalog_revision"]:
                        raise CatalogConflict("stale source catalog revision")
            journal_etag = _safe_journal_update(destination_backend, journal, journal_etag, "ready-to-commit")
            try:
                destination_backend.conditional_catalog_put(body, destination_catalog_etag)
                catalog_published = True
            except BaseException as error:
                if getattr(error, "published", False):
                    _safe_journal_update(destination_backend, journal, journal_etag, "cutover-published", error)
                else:
                    _safe_journal_update(destination_backend, journal, journal_etag, "conflict" if isinstance(error, CatalogConflict) else "failed", error)
                raise
            journal_etag = _safe_journal_update(destination_backend, journal, journal_etag, "committed")
            return {"ok": True, "status": "committed", "journal_status": "committed", "catalog": candidate, "migration_id": journal["migration_id"]}
    except BaseException as error:
        if catalog_published:
            raise
        if isinstance(error, CatalogConflict):
            _safe_journal_update(destination_backend, journal, journal_etag, "conflict", error)
        elif journal.get("status") not in {"conflict", "failed", "cutover-published"}:
            _safe_journal_update(destination_backend, journal, journal_etag, "interrupted" if isinstance(error, (KeyboardInterrupt, InterruptedError)) else "failed", error)
        raise


def copy_snapshot_stream(instance: Path, source_catalog: Catalog, destination_catalog: Catalog, source_backend, destination_backend, source_project: str, destination_project: str, snapshot_id: str, recipients: list[str], *, destination_etag: str | None = None, source_domain_id=None, destination_domain_id=None, source_key_generation=None, destination_key_generation=None, source_identity=None, destination_material=None, source_material=None) -> dict:
    """Transfer verified ciphertext, or re-encrypt only its exact envelope across domains."""
    instance = Path(instance)
    instance.mkdir(parents=True, exist_ok=True)
    source = source_catalog.resolve_snapshot(source_project, snapshot_id)
    operation_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=f"copy-{operation_id}-", dir=instance) as work:
        staged = Path(work) / "snapshot.jroom.age"
        if source_backend is None:
            ImmutableLocalStore(instance).download_file(source["object_key"], staged, source["ciphertext_sha256"], source["ciphertext_size"])
        else:
            source_backend.download_file(source["object_key"], staged, source["ciphertext_sha256"], source["ciphertext_size"])
        size, digest = _file_metadata(staged)
        if size != source["ciphertext_size"] or digest != source["ciphertext_sha256"]:
            raise ValueError("source ciphertext metadata mismatch")
        ref = None
        try:
            source_domain_id = _domain_value(source_material, source_domain_id, source_catalog)
            destination_domain_id = _domain_value(destination_material, destination_domain_id, destination_catalog)
            comparable_domains = any(value is not None for value in (source_domain_id, destination_domain_id, source_key_generation, destination_key_generation))
            same_domain = not comparable_domains or (
                source_domain_id is not None
                and destination_domain_id is not None
                and source_key_generation is not None
                and destination_key_generation is not None
                and source_domain_id == destination_domain_id
                and source_key_generation == destination_key_generation
            )
            object_key = source["object_key"]
            publish_path = staged
            if not same_domain:
                if source_identity is None and source_material is not None:
                    source_identity = source_material.identity
                if source_identity is None:
                    raise ValueError("source encryption identity is required for cross-domain copy")
                envelope = Path(work) / "snapshot.jroom"
                destination_encrypted = Path(work) / "destination.jroom.age"
                decrypt_file(staged, [Path(source_identity)], envelope)
                verify_envelope_file(envelope)
                encrypt_file(envelope, _material_recipients(destination_material, recipients), destination_encrypted)
                publish_path = destination_encrypted
                size, digest = _file_metadata(publish_path)
                object_key = f"objects/sha256/{digest}"
            if same_domain and _has_object_verifier(destination_backend or ImmutableLocalStore(instance)):
                try:
                    ref = _verify_destination_object(
                        destination_backend or ImmutableLocalStore(instance),
                        ObjectRef(object_key, digest, size),
                    )
                except Exception:  # noqa: BLE001 - a missing immutable object needs publication
                    ref = None
            if ref is None:
                if destination_backend is None:
                    ref = ImmutableLocalStore(instance).put_file(publish_path)
                else:
                    ref = destination_backend.put_file(object_key, publish_path)
            if ref.key != object_key or ref.sha256 != digest or ref.size != size:
                raise ValueError("destination ciphertext metadata mismatch")
            new_snapshot = dict(source)
            new_snapshot["snapshot_id"] = _snapshot_id()
            new_snapshot["created_at"] = datetime.now(UTC).isoformat()
            new_snapshot["origin_project_id"] = source.get("origin_project_id", source_project)
            new_snapshot["object_key"] = ref.key
            new_snapshot["ciphertext_sha256"] = ref.sha256
            new_snapshot["ciphertext_size"] = ref.size
            if destination_catalog.body["format_version"] == 2:
                new_snapshot.setdefault("workspace_fingerprint", source.get("workspace_fingerprint", "0" * 64))
            updated = destination_catalog.add_snapshot(destination_project, source_catalog.body["projects"][source_project]["display_name"], new_snapshot)
            if destination_backend is not None and hasattr(destination_backend, "conditional_catalog_put"):
                body = _encrypt_catalog(updated, _material_recipients(destination_material, recipients), instance)
                destination_backend.conditional_catalog_put(body, destination_etag)
            elif destination_backend is None:
                CatalogFile(instance / "catalog.jroom.age", Path(os.environ["JOSH_ROOM_IDENTITY"]), destination_catalog.dimension_id).update_if_revision(destination_catalog.body["revision"], updated, recipients)
        except BaseException as error:
            _record_orphan(destination_backend, ref)
            if ref is None:
                raise
            receipt = _write_copy_orphan_receipt(instance, ref, destination_backend, error)
            raise CopyPublicationError(error, receipt) from error
    return {"ok": True, "project_id": destination_project, "snapshot_id": new_snapshot["snapshot_id"], "object_key": new_snapshot["object_key"], "ciphertext_sha256": new_snapshot["ciphertext_sha256"], "ciphertext_size": new_snapshot["ciphertext_size"], "catalog": updated}


def _manifest_matches_snapshot(manifest: dict, project_id: str, snapshot: dict) -> bool:
    return manifest.get("project_id") == snapshot.get("origin_project_id", project_id)
