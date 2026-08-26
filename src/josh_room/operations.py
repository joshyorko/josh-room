import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .catalog import Catalog, CatalogFile
from .crypto import decrypt, decrypt_file, encrypt, encrypt_file
from .envelope import build_envelope_file, read_envelope_file
from .jat import run_build, run_restore, run_serve
from .local_store import ImmutableLocalStore
from .progress import report_progress
from .workspace_state import (
    read_workspace_marker,
    workspace_fingerprint,
    write_workspace_marker,
)


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
) -> dict:
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=instance) as work:
        haul = Path(work) / "payload.haul.tar.zst"
        report_progress("build", "Building portable Room haul")
        producer = run_build(jat_root, source, haul, images=images, all_images=all_images, rcc_environment="auto")
        payload_size, payload_digest = _file_metadata(haul)
        manifest = {"format_version": 1, "project_id": project_id, "snapshot_id": _snapshot_id(), "created_at": datetime.now(UTC).isoformat(), "payload": {"format": "jat-hauler", "sha256": payload_digest, "size": payload_size, "producer_version": producer["version"]}, "source": _source_metadata(source)}
        if isinstance(producer.get("environment_artifact"), dict):
            manifest["environment_artifact"] = producer["environment_artifact"]
        envelope = Path(work) / "snapshot.jroom"
        report_progress("package", "Packaging the trusted snapshot envelope")
        build_envelope_file(manifest, haul, envelope)
        encrypted = Path(work) / "snapshot.jroom.age"
        report_progress("encrypt", "Encrypting Room with age")
        encrypt_file(envelope, recipients, encrypted)
        ciphertext_size, ciphertext_digest = _file_metadata(encrypted)
        if backend:
            report_progress("upload", "Uploading encrypted Room to private R2")
            ref = backend.put_file(f"objects/sha256/{ciphertext_digest}", encrypted)
        else:
            report_progress("store", "Writing encrypted Room to local storage")
            ref = ImmutableLocalStore(instance).put_file(encrypted)
        if ref.sha256 != ciphertext_digest or ref.size != ciphertext_size:
            raise ValueError("published ciphertext metadata mismatch")
        catalog_path = instance / "catalog.jroom.age"
        dimension_id = getattr(getattr(backend, "config", None), "dimension_id", None) if backend else None
        identity_value = os.environ.get("JOSH_ROOM_IDENTITY")
        report_progress("catalog", "Loading encrypted Room catalog")
        if backend:
            catalog, catalog_etag = _read_remote_catalog(backend, identity_value, instance, getattr(getattr(backend, "config", None), "dimension_id", None))
        else:
            catalog_file = CatalogFile(catalog_path, Path(identity_value) if identity_value else None, dimension_id if "dimension_id" in locals() else None)
            catalog = catalog_file.read()
            catalog_etag = None
        observed_revision = catalog.body["revision"]
        saved_fingerprint = workspace_fingerprint(source)
        snapshot_record = {"snapshot_id": manifest["snapshot_id"], "origin_project_id": project_id, "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "created_at": manifest["created_at"]}
        if dimension_id:
            snapshot_record["workspace_fingerprint"] = saved_fingerprint
        catalog = catalog.add_snapshot(project_id, display_name or _display_name(project_id), snapshot_record)
        try:
            report_progress("catalog", "Publishing the new latest Room snapshot")
            if backend:
                backend.conditional_catalog_put(_encrypt_catalog(catalog, recipients, instance), catalog_etag)
            else:
                catalog_file.update_if_revision(observed_revision, catalog, recipients)
        except BaseException:
            if backend:
                backend.record_orphan(ref)
            raise
        _write_room_marker(source, project_id, display_name or _display_name(project_id), dimension_id=dimension_id, snapshot_id=manifest["snapshot_id"], workspace_fp=saved_fingerprint)
        report_progress("complete", "Room saved safely")
        return {"project_id": project_id, "snapshot_id": manifest["snapshot_id"], "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "producer": producer}


def hydrate(instance: Path, project_id: str, destination: Path, identity: Path, jat_root: Path, backend=None, snapshot_id: str = "latest") -> dict:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError("destination must be empty or absent")
    destination.parent.mkdir(parents=True, exist_ok=True)
    operation_id = uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.josh-room-", dir=destination.parent))
    receipt = instance / "receipts" / f"{operation_id}.json"
    try:
        report_progress("catalog", "Loading encrypted Room catalog")
        if backend:
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


def remove_room(instance: Path, project_id: str, identity: Path, recipients: list[str], backend=None) -> dict:
    operation_id = uuid.uuid4().hex
    receipt = instance / "receipts" / f"{operation_id}.json"
    report_progress("catalog", "Loading encrypted Room catalog")
    if backend:
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
) -> dict:
    operation_id = uuid.uuid4().hex
    receipt = instance / "receipts" / f"{operation_id}.json"
    report_progress("catalog", "Loading encrypted Room catalog")
    if backend:
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


def serve_snapshot(instance: Path, project_id: str, snapshot_id: str, identity: Path, jat_root: Path, backend=None) -> dict:
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serve-", dir=instance) as work:
        stage = Path(work)
        report_progress("catalog", "Loading encrypted Room catalog")
        if backend:
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


def _read_remote_catalog(backend, identity_value, instance: Path, dimension_id: str | None = None):
    dimension_id = dimension_id or getattr(getattr(backend, "config", None), "dimension_id", None)
    encrypted, etag = backend.read_catalog()
    if encrypted is None:
        return Catalog.empty(dimension_id), None
    with tempfile.NamedTemporaryFile(prefix=".catalog-read.", delete=False) as handle:
        path = Path(handle.name)
        handle.write(encrypted)
    try:
        return Catalog.from_body(json.loads(decrypt(path, [Path(identity_value)])), dimension_id), etag
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


def _snapshot_id() -> str:
    return uuid.uuid4().hex


def _display_name(project_id: str) -> str:
    return project_id.replace("-", " ").replace("_", " ").title()


def _source_metadata(source: Path) -> dict:
    commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if commit.returncode != 0:
        return {}
    status = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"], capture_output=True, text=True, check=False)
    if status.returncode != 0:
        return {"git_commit": commit.stdout.strip()}
    return {"git_commit": commit.stdout.strip(), "dirty": bool(status.stdout)}


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


def _workspace_binding(workspace: Path, catalog: Catalog, object_evidence: dict | None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None):
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


def link_workspace(workspace: Path, catalog: Catalog, object_evidence: dict | None = None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None) -> dict:
    _marker, project_id, snapshot_id, dimension_id, snapshot = _workspace_binding(workspace, catalog, object_evidence, project_id=project_id, snapshot_id=snapshot_id, dimension_id=dimension_id)
    display_name = catalog.body["projects"][project_id]["display_name"]
    write_workspace_marker(workspace, dimension_id=dimension_id, project_id=project_id, display_name=display_name, snapshot_id=snapshot_id, workspace_fingerprint=snapshot["workspace_fingerprint"])
    return {"ok": True, "dimension_id": dimension_id, "project_id": project_id, "snapshot_id": snapshot_id, "marker": read_workspace_marker(workspace)}


def repair_workspace(workspace: Path, catalog: Catalog, object_evidence: dict | None = None, *, project_id: str | None = None, snapshot_id: str | None = None, dimension_id: str | None = None) -> dict:
    if not catalog.body.get("projects"):
        raise ValueError("catalog evidence is required")
    _marker, project_id, snapshot_id, dimension_id, snapshot = _workspace_binding(workspace, catalog, object_evidence, project_id=project_id, snapshot_id=snapshot_id, dimension_id=dimension_id)
    write_workspace_marker(workspace, dimension_id=dimension_id, project_id=project_id, display_name=catalog.body["projects"][project_id]["display_name"], snapshot_id=snapshot_id, workspace_fingerprint=snapshot["workspace_fingerprint"])
    return {"ok": True, "dimension_id": dimension_id, "project_id": project_id, "snapshot_id": snapshot_id}


def _record_orphan(store, ref) -> None:
    if ref is not None and hasattr(store, "record_orphan"):
        store.record_orphan(ref)


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
        if destination_catalog.body["format_version"] == 2:
            new_snapshot.setdefault("workspace_fingerprint", source.get("workspace_fingerprint", "0" * 64))
        project = destination_project_id or project_id
        updated = destination_catalog.add_snapshot(project, source_catalog.body["projects"][project_id]["display_name"], new_snapshot)
        return {"ok": True, "project_id": project, "snapshot_id": new_snapshot["snapshot_id"], "object_key": new_snapshot["object_key"], "ciphertext_sha256": new_snapshot["ciphertext_sha256"], "ciphertext_size": new_snapshot["ciphertext_size"], "catalog": updated}
    except BaseException:
        _record_orphan(destination_store, put)
        raise


def copy_snapshot_stream(instance: Path, source_catalog: Catalog, destination_catalog: Catalog, source_backend, destination_backend, source_project: str, destination_project: str, snapshot_id: str, recipients: list[str], *, destination_etag: str | None = None) -> dict:
    """Transfer one immutable ciphertext once, then conditionally publish its new JAT."""
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
            if destination_backend is None:
                ref = ImmutableLocalStore(instance).put_file(staged)
            else:
                ref = destination_backend.put_file(source["object_key"], staged)
            if ref.key != source["object_key"] or ref.sha256 != digest or ref.size != size:
                raise ValueError("destination ciphertext metadata mismatch")
            new_snapshot = dict(source)
            new_snapshot["snapshot_id"] = _snapshot_id()
            new_snapshot["created_at"] = datetime.now(UTC).isoformat()
            if destination_catalog.body["format_version"] == 2:
                new_snapshot.setdefault("workspace_fingerprint", source.get("workspace_fingerprint", "0" * 64))
            updated = destination_catalog.add_snapshot(destination_project, source_catalog.body["projects"][source_project]["display_name"], new_snapshot)
            if destination_backend is not None and hasattr(destination_backend, "conditional_catalog_put"):
                body = _encrypt_catalog(updated, recipients, instance)
                destination_backend.conditional_catalog_put(body, destination_etag)
            elif destination_backend is None:
                CatalogFile(instance / "catalog.jroom.age", Path(os.environ["JOSH_ROOM_IDENTITY"]), destination_catalog.dimension_id).update_if_revision(destination_catalog.body["revision"], updated, recipients)
        except BaseException:
            _record_orphan(destination_backend, ref)
            raise
    return {"ok": True, "project_id": destination_project, "snapshot_id": new_snapshot["snapshot_id"], "object_key": new_snapshot["object_key"], "ciphertext_sha256": new_snapshot["ciphertext_sha256"], "ciphertext_size": new_snapshot["ciphertext_size"], "catalog": updated}


def _manifest_matches_snapshot(manifest: dict, project_id: str, snapshot: dict) -> bool:
    return manifest.get("project_id") == snapshot.get("origin_project_id", project_id)
