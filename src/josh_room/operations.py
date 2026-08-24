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
from .jat import run_build, run_restore
from .local_store import ImmutableLocalStore


def create_snapshot(instance: Path, project_id: str, source: Path, jat_root: Path, recipients: list[str], backend=None) -> dict:
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=instance) as work:
        haul = Path(work) / "payload.haul.tar.zst"
        producer = run_build(jat_root, source, haul)
        payload_size, payload_digest = _file_metadata(haul)
        manifest = {"format_version": 1, "project_id": project_id, "snapshot_id": _snapshot_id(), "created_at": datetime.now(UTC).isoformat(), "payload": {"format": "jat-hauler", "sha256": payload_digest, "size": payload_size, "producer_version": producer["version"]}, "source": _source_metadata(source)}
        envelope = Path(work) / "snapshot.jroom"
        build_envelope_file(manifest, haul, envelope)
        encrypted = Path(work) / "snapshot.jroom.age"
        encrypt_file(envelope, recipients, encrypted)
        ciphertext_size, ciphertext_digest = _file_metadata(encrypted)
        if backend:
            ref = backend.put_file(f"objects/sha256/{ciphertext_digest}", encrypted)
        else:
            ref = ImmutableLocalStore(instance).put_file(encrypted)
        if ref.sha256 != ciphertext_digest or ref.size != ciphertext_size:
            raise ValueError("published ciphertext metadata mismatch")
        catalog_path = instance / "catalog.jroom.age"
        identity_value = os.environ.get("JOSH_ROOM_IDENTITY")
        if backend:
            catalog, catalog_etag = _read_remote_catalog(backend, identity_value, instance)
        else:
            catalog_file = CatalogFile(catalog_path, Path(identity_value) if identity_value else None)
            catalog = catalog_file.read()
            catalog_etag = None
        observed_revision = catalog.body["revision"]
        catalog = catalog.add_snapshot(project_id, _display_name(project_id), {"snapshot_id": manifest["snapshot_id"], "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "created_at": manifest["created_at"]})
        try:
            if backend:
                backend.conditional_catalog_put(_encrypt_catalog(catalog, recipients, instance), catalog_etag)
            else:
                catalog_file.update_if_revision(observed_revision, catalog, recipients)
        except BaseException:
            if backend:
                backend.record_orphan(ref)
            raise
        return {"project_id": project_id, "snapshot_id": manifest["snapshot_id"], "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size, "producer": producer}


def hydrate(instance: Path, project_id: str, destination: Path, identity: Path, jat_root: Path, backend=None) -> dict:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError("destination must be empty or absent")
    destination.parent.mkdir(parents=True, exist_ok=True)
    operation_id = uuid.uuid4().hex
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.josh-room-", dir=destination.parent))
    receipt = instance / "receipts" / f"{operation_id}.json"
    try:
        if backend:
            catalog, _catalog_etag = _read_remote_catalog(backend, identity, instance)
        else:
            catalog = CatalogFile(instance / "catalog.jroom.age", identity).read()
        snapshot = catalog.latest(project_id)
        if backend:
            encrypted = stage / "snapshot.jroom.age"
            backend.download_file(snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"])
        else:
            encrypted = stage / "snapshot.jroom.age"
            ImmutableLocalStore(instance).download_file(
                snapshot["object_key"], encrypted, snapshot["ciphertext_sha256"], snapshot["ciphertext_size"]
            )
        envelope = stage / "snapshot.jroom"
        decrypt_file(encrypted, [identity], envelope)
        haul = stage / "payload.haul.tar.zst"
        manifest = read_envelope_file(envelope, haul)
        if manifest["project_id"] != project_id:
            raise ValueError("manifest project mismatch")
        workspace_stage = stage / "restore"
        jat_result = run_restore(jat_root, haul, workspace_stage)
        restored_root = workspace_stage / "workspace"
        if not restored_root.is_dir() or not any(restored_root.iterdir()):
            raise ValueError("JAT restore did not produce an expected workspace root")
        backup = None
        if destination.exists():
            backup = destination.parent / f".{destination.name}.josh-room-backup-{operation_id}"
            os.replace(destination, backup)
            if any(backup.iterdir()):
                os.replace(backup, destination)
                raise FileExistsError("destination became non-empty before promotion")
        try:
            os.replace(workspace_stage, destination)
        except BaseException:
            if backup and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup and backup.exists():
            backup.rmdir()
        result = {"project_id": project_id, "snapshot_id": snapshot["snapshot_id"], "destination": str(destination), "receipt": str(receipt), "jat": jat_result}
        _write_receipt(receipt, {"operation": "hydrate", "operation_id": operation_id, "status": "success", **result})
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


def _read_remote_catalog(backend, identity_value, instance: Path):
    encrypted, etag = backend.read_catalog()
    if encrypted is None:
        return Catalog.empty(), None
    with tempfile.NamedTemporaryFile(prefix=".catalog-read.", delete=False) as handle:
        path = Path(handle.name)
        handle.write(encrypted)
    try:
        return Catalog(json.loads(decrypt(path, [Path(identity_value)]))), etag
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
