"""Selected Room Store operations that do not hide materialization work."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import corroborate_logical_snapshot
from .jat import run_extract, run_serve
from .logical_jat import LogicalJat
from .operations import _encrypt_catalog, snapshot_payload_kind
from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    protect_private_file,
)
from .restic_store import ResticStoreError
from .room_store_export import (
    PortableExportError,
    _semantic_workspace,
    export_portable_jat,
    materialize_components,
)
from .room_store_operations import (
    RoomStoreOperationsError,
    _validate_snapshot_entries,
    scan_workspace_for_status,
)
from .room_store_references import (
    LogicalJatIdentity,
    ResticReachabilityReport,
    ResticSnapshotRef,
    build_reference_index,
    compare_restic_inventory,
)
from .workspace_policy import load_capture_policy

_ROOM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")


class RoomStoreLifecycleError(ValueError):
    """Bounded failure from an explicit Room Store lifecycle operation."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "room-store-lifecycle-failed",
        result: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.result = {"error": code}
        if result:
            self.result.update(result)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LogicalCatalogRemoval:
    operation_id: str
    dimension_id: str
    encryption_domain_id: str
    repository_id: str
    catalog_revision: int
    identities: frozenset[LogicalJatIdentity]
    snapshot_trees: dict[ResticSnapshotRef, str]
    object_cleanup_pending: tuple[str, ...]


def _selected_logical_record(context) -> tuple[dict, LogicalJat]:
    record = getattr(context, "selected_record", None)
    descriptor = getattr(context, "selected_descriptor", None)
    project_id = getattr(context, "project_id", None)
    if not isinstance(project_id, str) or not isinstance(record, dict):
        raise RoomStoreLifecycleError("a selected Room Store JAT is required", code="snapshot-required")
    try:
        if snapshot_payload_kind(record) != "room-store-v1":
            raise RoomStoreLifecycleError("selected recovery point is a portable JAT", code="legacy-snapshot")
        selected = context.catalog.resolve_snapshot(project_id, record["snapshot_id"])
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, RoomStoreLifecycleError):
            raise
        raise RoomStoreLifecycleError("selected recovery point is unavailable", code="snapshot-unavailable") from None
    if selected != record or not isinstance(descriptor, LogicalJat):
        raise RoomStoreLifecycleError("selected logical JAT is unavailable", code="descriptor-unavailable")
    try:
        corroborate_logical_snapshot(project_id, record, descriptor)
    except (TypeError, ValueError):
        raise RoomStoreLifecycleError("selected logical JAT does not match its catalog record", code="descriptor-invalid") from None
    body = descriptor.to_dict()
    if (
        body["dimension_id"] != context.dimension.dimension_id
        or body["encryption_domain_id"] != context.material.encryption_domain_id
        or body["workspace"]["repository_id"] != context.repository_info.repository_id
    ):
        raise RoomStoreLifecycleError("selected logical JAT is outside this Room Store", code="snapshot-scope-mismatch")
    return record, descriptor


def inspect_logical_jat(record: dict, descriptor: LogicalJat) -> dict[str, Any]:
    """Return bounded descriptor metadata without restoring or exporting payloads."""
    if not isinstance(record, dict) or not isinstance(descriptor, LogicalJat):
        raise TypeError("logical JAT inspection input is invalid")
    try:
        if snapshot_payload_kind(record) != "room-store-v1":
            raise RoomStoreLifecycleError("selected recovery point is a portable JAT", code="legacy-snapshot")
        corroborate_logical_snapshot(record["room_id"], record, descriptor)
    except RoomStoreLifecycleError:
        raise
    except (KeyError, TypeError, ValueError):
        raise RoomStoreLifecycleError("logical JAT does not match its catalog record", code="descriptor-invalid") from None
    body = descriptor.to_dict()
    components = {}
    for name, component in body["components"].items():
        if component is None:
            components[name] = None
            continue
        value = {
            "kind": component["kind"],
            "repository_id": component["snapshot"]["repository_id"],
            "snapshot_id": component["snapshot"]["snapshot_id"],
            "tree_id": component["snapshot"]["tree_id"],
            "archive_sha256": component["archive_sha256"],
            "archive_size": component["archive_size"],
        }
        for field in (
            "artifact_digest",
            "specification_digest",
            "platform",
            "rcc_version",
            "source_input_sha256",
            "references",
        ):
            if field in component:
                value[field] = copy.deepcopy(component[field])
        components[name] = value
    return {
        "logical_jat_id": body["logical_jat_id"],
        "payload_kind": body["payload_kind"],
        "dimension_id": body["dimension_id"],
        "encryption_domain_id": body["encryption_domain_id"],
        "room_id": body["room_id"],
        "origin_room_id": body.get("origin_room_id"),
        "created_at": body["created_at"],
        "workspace_snapshot_id": body["workspace"]["snapshot_id"],
        "workspace_tree_id": body["workspace"]["tree_id"],
        "repository_id": body["workspace"]["repository_id"],
        "repository_format": body["workspace"]["repository_format"],
        "logical_bytes": body["workspace"]["logical_bytes"],
        "data_added_bytes": body["workspace"]["data_added"],
        "data_added_packed_bytes": body["workspace"]["data_added_packed"],
        "workspace": {
            "repository_id": body["workspace"]["repository_id"],
            "repository_format": body["workspace"]["repository_format"],
            "snapshot_id": body["workspace"]["snapshot_id"],
            "tree_id": body["workspace"]["tree_id"],
            "logical_bytes": body["workspace"]["logical_bytes"],
            "data_added": body["workspace"]["data_added"],
            "data_added_packed": body["workspace"]["data_added_packed"],
        },
        "components": components,
        "export_state": "available",
        "export_available": True,
        "export_cached": False,
    }


def inspect_selected_room_store(context) -> dict[str, Any]:
    record, descriptor = _selected_logical_record(context)
    return {"ok": True, **inspect_logical_jat(record, descriptor)}


def export_logical_jat(
    context,
    *,
    jat_root,
    output,
    cancellation=None,
) -> dict[str, Any]:
    _record, descriptor = _selected_logical_record(context)
    try:
        result = export_portable_jat(
            descriptor=descriptor,
            restic=context.store,
            staging_parent=context.private_dir,
            output=output,
            jat_root=jat_root,
            cancellation=cancellation,
        )
    except PortableExportError as error:
        raise RoomStoreLifecycleError(str(error), code="portable-export-failed") from None
    return {
        "ok": True,
        "logical_jat_id": result.logical_jat_id,
        "status": result.status,
        "output_size": result.output_size,
        "output_sha256": result.output_sha256,
        "workspace_entry_count": result.workspace_entry_count,
        "verified_components": list(result.verified_components),
        "estimated_required_bytes": result.estimated_required_bytes,
        "available_bytes": result.available_bytes,
    }


@contextmanager
def _materialized_portable_haul(context, jat_root, cancellation=None):
    _record, descriptor = _selected_logical_record(context)
    try:
        with tempfile.TemporaryDirectory(
            prefix="room-store-jat-action-",
            dir=context.private_dir,
        ) as temporary:
            stage = Path(temporary)
            protect_private_directory(stage)
            output = stage / "portable.haul.tar.zst"
            export_portable_jat(
                descriptor=descriptor,
                restic=context.store,
                staging_parent=context.private_dir,
                output=output,
                jat_root=jat_root,
                cancellation=cancellation,
            )
            yield output
    except PortableExportError as error:
        raise RoomStoreLifecycleError(str(error), code="portable-export-failed") from None


def serve_logical_jat(context, *, jat_root, mode: str = "auto", cancellation=None) -> dict[str, Any]:
    if mode not in {"auto", "files", "registry", "both"}:
        raise ValueError("JAT Serve mode is invalid")
    with _materialized_portable_haul(context, jat_root, cancellation) as haul:
        result = run_serve(jat_root, haul, mode=mode)
    return {"ok": True, "logical_jat_id": context.selected_descriptor.to_dict()["logical_jat_id"], **result}


def extract_logical_jat(
    context,
    reference: str,
    destination: Path,
    *,
    jat_root,
    cancellation=None,
) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference:
        raise ValueError("JAT Extract reference is required")
    with _materialized_portable_haul(context, jat_root, cancellation) as haul:
        result = run_extract(jat_root, haul, reference, destination)
    return {"ok": True, "logical_jat_id": context.selected_descriptor.to_dict()["logical_jat_id"], **result}


def copy_logical_jat_as_new(
    source_context,
    destination_context,
    destination_room_id: str,
    display_name: str,
) -> dict[str, Any]:
    source_record, source_descriptor = _selected_logical_record(source_context)
    if not isinstance(destination_room_id, str) or _ROOM_ID.fullmatch(destination_room_id) is None:
        raise ValueError("destination Room ID is invalid")
    if destination_room_id == source_context.project_id:
        raise ValueError("Copy as New requires a different destination Room")
    same_physical_scope = (
        isinstance(source_context.physical_binding, str)
        and bool(source_context.physical_binding)
        and source_context.physical_binding == destination_context.physical_binding
    )
    if (
        not same_physical_scope
        or source_context.dimension.dimension_id != destination_context.dimension.dimension_id
        or source_context.material.encryption_domain_id != destination_context.material.encryption_domain_id
        or source_context.repository_info.repository_id != destination_context.repository_info.repository_id
    ):
        raise RoomStoreLifecycleError("source and destination do not share a physical Room Store", code="copy-scope-mismatch")
    if (
        source_context.catalog.body["revision"] != destination_context.catalog.body["revision"]
        or source_context.catalog_etag != destination_context.catalog_etag
        or not source_context.catalog_etag
    ):
        raise RoomStoreLifecycleError("source and destination catalog revisions do not match", code="catalog-revision-mismatch")
    try:
        same_source_record = destination_context.catalog.resolve_snapshot(
            source_context.project_id,
            source_record["snapshot_id"],
        )
    except (KeyError, ValueError):
        raise RoomStoreLifecycleError("source recovery point is no longer present", code="source-snapshot-unavailable") from None
    if same_source_record != source_record:
        raise RoomStoreLifecycleError("source recovery point changed during Copy", code="source-snapshot-changed")
    publish_descriptor = getattr(destination_context, "publish_descriptor", None)
    if not callable(publish_descriptor):
        raise RoomStoreLifecycleError("destination catalog writer is unavailable", code="destination-writer-unavailable")
    body = source_descriptor.to_dict()
    body["logical_jat_id"] = uuid.uuid4().hex
    body["room_id"] = destination_room_id
    body["origin_room_id"] = body.get("origin_room_id", source_context.project_id)
    body["created_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    descriptor = LogicalJat.from_dict(body)
    operation_id = uuid.uuid4().hex
    try:
        publish_descriptor(
            descriptor,
            expected_etag=source_context.catalog_etag,
            workspace_signature=source_record["workspace_signature"],
            signature_algorithm=source_record["signature_algorithm"],
        )
    except Exception as error:
        published = getattr(error, "published", None)
        publication_state = (
            "committed-verification-unknown"
            if published is True
            else "rejected"
            if published is False
            else "uncertain"
        )
        _write_copy_receipt(
            destination_context.instance,
            operation_id,
            {
                "operation": "same-dimension-copy",
                "operation_id": operation_id,
                "status": "reconciliation-required",
                "publication_state": publication_state,
                "source_room_id": source_context.project_id,
                "source_logical_jat_id": source_descriptor.to_dict()["logical_jat_id"],
                "destination_room_id": destination_room_id,
                "descriptor_object_state": "unknown",
                "descriptor_object_key": getattr(error, "descriptor_object_key", None),
                "reconciliation_required": True,
            },
        )
        failure = RoomStoreLifecycleError(
            "Copy catalog publication requires reconciliation",
            code="copy-publication-uncertain",
            result={
                "reconciliation_required": True,
                "publication_state": publication_state,
                "receipt_id": operation_id,
                "descriptor_object_key": getattr(error, "descriptor_object_key", None),
            },
        )
        raise failure from error
    return {
        "ok": True,
        "status": "published",
        "source_room_id": source_context.project_id,
        "source_logical_jat_id": source_descriptor.to_dict()["logical_jat_id"],
        "destination_room_id": destination_room_id,
        "logical_jat_id": descriptor.to_dict()["logical_jat_id"],
        "workspace_snapshot_id": descriptor.to_dict()["workspace"]["snapshot_id"],
        "data_uploaded": 0,
    }


def _copy_component_files(name: str, materialized) -> tuple[tuple[Path, str], ...]:
    if name == "rcc_environment":
        if materialized.rcc_archive is None or materialized.rcc_metadata is None:
            raise RoomStoreLifecycleError("verified RCC component files are unavailable", code="component-materialization-failed")
        return ((materialized.rcc_archive, "rcc-environment.rcca"), (materialized.rcc_metadata, "metadata.json"))
    if name == "homebrew_recovery":
        if materialized.brew_archive is None:
            raise RoomStoreLifecycleError("verified Homebrew component file is unavailable", code="component-materialization-failed")
        return ((materialized.brew_archive, "homebrew-recovery.tar.zst"),)
    if materialized.hauler_archive is None:
        raise RoomStoreLifecycleError("verified Hauler component files are unavailable", code="component-materialization-failed")
    return (
        (materialized.hauler_archive, "hauler-content.tar.zst"),
        (materialized.hauler_archive.parent / "metadata.json", "metadata.json"),
    )


def _copy_rebased_component(
    source_component: dict,
    name: str,
    materialized,
    destination_context,
    destination_parent: LogicalJat | None,
    stage_root: Path,
    cancellation,
    created_snapshot_ids: list[str],
) -> tuple[dict, int]:
    component_stage = stage_root / f"component-{name}"
    component_stage.mkdir(mode=0o700)
    try:
        protect_private_directory(component_stage)
        expected_names = set()
        for source_path, destination_name in _copy_component_files(name, materialized):
            metadata = source_path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError
            target = component_stage / destination_name
            with source_path.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            protect_private_file(target)
            expected_names.add(destination_name)
    except RoomStoreLifecycleError:
        raise
    except (OSError, PrivatePathError, ValueError):
        raise RoomStoreLifecycleError("verified component could not be staged", code="component-materialization-failed") from None

    parent_component = (
        destination_parent.to_dict()["components"][name]
        if destination_parent is not None
        else None
    )
    parent_snapshot_id = (
        parent_component["snapshot"]["snapshot_id"]
        if parent_component is not None
        else None
    )
    try:
        summary = destination_context.store.backup(
            component_stage,
            parent=parent_snapshot_id,
            cancellation=cancellation,
        )
        snapshot_id = summary.snapshot_id or parent_snapshot_id
        if not snapshot_id:
            raise ValueError
        snapshot = destination_context.store.snapshot(snapshot_id)
        if summary.snapshot_id:
            created_snapshot_ids.append(snapshot_id)
            if snapshot.parent_snapshot_id != parent_snapshot_id:
                raise ValueError
        elif parent_component is None or snapshot.tree_id != parent_component["snapshot"]["tree_id"]:
            raise ValueError
        rows = _validate_snapshot_entries(destination_context.store.entries(snapshot_id))
        if set(rows) - {"."} != expected_names:
            raise ValueError
        verification_stage = stage_root / f"component-{name}-verification"
        destination_context.store.restore(snapshot_id, verification_stage)
        actual_names = {path.name for path in verification_stage.iterdir()}
        if actual_names != expected_names:
            raise ValueError
        for _source_path, filename in _copy_component_files(name, materialized):
            staged_digest = hashlib.sha256((component_stage / filename).read_bytes()).digest()
            restored_digest = hashlib.sha256((verification_stage / filename).read_bytes()).digest()
            if staged_digest != restored_digest:
                raise ValueError
    except RoomStoreLifecycleError:
        raise
    except (OSError, ResticStoreError, RoomStoreOperationsError, TypeError, ValueError) as error:
        raise RoomStoreLifecycleError(
            "destination component backup could not be verified",
            code="component-backup-failed",
            result={
                "candidate_snapshot_ids": [
                    getattr(error, "orphan_snapshot_id", None)
                ] if _SNAPSHOT_ID.fullmatch(str(getattr(error, "orphan_snapshot_id", ""))) else [],
            },
        ) from None

    rebased = copy.deepcopy(source_component)
    rebased["snapshot"] = {
        "repository_id": destination_context.repository_info.repository_id,
        "repository_format": destination_context.repository_info.repository_format,
        "snapshot_id": snapshot.snapshot_id,
        "tree_id": snapshot.tree_id,
    }
    return rebased, summary.data_added


def _write_copy_receipt(instance: Path, operation_id: str, body: dict) -> None:
    receipt_dir = Path(instance) / "receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect_private_directory(receipt_dir)
    path = receipt_dir / f"{operation_id}.json"
    temporary = receipt_dir / f".{operation_id}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            protect_private_file(temporary)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                json.dump(body, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cross_copy_failure_receipt(
    destination_context,
    operation_id: str,
    source_context,
    destination_room_id: str,
    publication_state: str,
    error: BaseException,
    created_snapshot_ids: list[str],
) -> str | None:
    candidates = list(created_snapshot_ids)
    result = getattr(error, "result", None)
    if isinstance(result, dict):
        for snapshot_id in result.get("candidate_snapshot_ids", []):
            if isinstance(snapshot_id, str) and _SNAPSHOT_ID.fullmatch(snapshot_id):
                candidates.append(snapshot_id)
        publication_state = result.get("publication_state", publication_state)
    orphan_snapshot_id = getattr(error, "orphan_snapshot_id", None)
    if isinstance(orphan_snapshot_id, str) and _SNAPSHOT_ID.fullmatch(orphan_snapshot_id):
        candidates.append(orphan_snapshot_id)
    candidates = sorted(set(candidates))
    if not candidates and publication_state == "not-committed":
        return None
    definitely_unpublished = publication_state in {"not-committed", "rejected"}
    body = {
        "operation": "cross-dimension-copy",
        "operation_id": operation_id,
        "status": "failed",
        "publication_state": publication_state,
        "source_room_id": source_context.project_id,
        "source_logical_jat_id": source_context.selected_descriptor.to_dict()["logical_jat_id"],
        "destination_room_id": destination_room_id,
        "reconciliation_required": True,
        "error_type": type(error).__name__,
    }
    descriptor_object_key = getattr(error, "descriptor_object_key", None)
    if isinstance(result, dict):
        descriptor_object_key = descriptor_object_key or result.get("descriptor_object_key")
    if isinstance(descriptor_object_key, str):
        body["descriptor_object_key"] = descriptor_object_key
    body[
        "unpublished_restic_snapshot_ids"
        if definitely_unpublished
        else "candidate_restic_snapshot_ids"
    ] = candidates
    _write_copy_receipt(destination_context.instance, operation_id, body)
    return operation_id


def copy_logical_jat_to_dimension(
    source_context,
    destination_context,
    destination_room_id: str,
    display_name: str,
    *,
    cancellation=None,
) -> dict[str, Any]:
    """Restore and re-back up a logical JAT into an independent Dimension."""
    _source_record, source_descriptor = _selected_logical_record(source_context)
    if not isinstance(destination_room_id, str) or _ROOM_ID.fullmatch(destination_room_id) is None:
        raise ValueError("destination Room ID is invalid")
    if getattr(destination_context, "writable", False) is not True:
        raise RoomStoreLifecycleError("destination Room Store is not writable", code="destination-writer-unavailable")
    if destination_context.project_id not in {None, destination_room_id}:
        raise RoomStoreLifecycleError("destination Room context does not match", code="destination-room-mismatch")
    source_domain = source_context.material.encryption_domain_id
    destination_domain = destination_context.material.encryption_domain_id
    source_repo = source_context.repository_info.repository_id
    destination_repo = destination_context.repository_info.repository_id
    if (
        source_context.dimension.dimension_id == destination_context.dimension.dimension_id
        or source_domain == destination_domain
        or source_repo == destination_repo
    ):
        raise RoomStoreLifecycleError(
            "cross-Dimension Copy requires an independent encryption domain and repository",
            code="copy-scope-mismatch",
        )
    publish_descriptor = getattr(destination_context, "publish_descriptor", None)
    if not callable(publish_descriptor):
        raise RoomStoreLifecycleError("destination catalog writer is unavailable", code="destination-writer-unavailable")

    source_body = source_descriptor.to_dict()
    destination_parent = getattr(destination_context, "selected_descriptor", None)
    if destination_parent is not None and not isinstance(destination_parent, LogicalJat):
        raise RoomStoreLifecycleError("destination parent descriptor is invalid", code="destination-descriptor-invalid")
    if destination_parent is not None:
        parent_body = destination_parent.to_dict()
        if (
            parent_body["room_id"] != destination_room_id
            or parent_body["dimension_id"] != destination_context.dimension.dimension_id
            or parent_body["encryption_domain_id"] != destination_domain
            or parent_body["workspace"]["repository_id"] != destination_repo
        ):
            raise RoomStoreLifecycleError("destination parent belongs to another Room Store", code="destination-descriptor-invalid")

    components_size = sum(
        component["archive_size"]
        for component in source_body["components"].values()
        if component is not None
    )
    estimated_required = 2 * (source_body["workspace"]["logical_bytes"] + components_size)
    try:
        available = min(
            shutil.disk_usage(source_context.private_dir).free,
            shutil.disk_usage(destination_context.private_dir).free,
        )
    except OSError:
        raise RoomStoreLifecycleError("cross-Dimension Copy disk preflight failed", code="copy-preflight-failed") from None
    if available < estimated_required:
        raise RoomStoreLifecycleError("cross-Dimension Copy requires more free disk space", code="copy-disk-space-insufficient")

    operation_id = uuid.uuid4().hex
    created_snapshot_ids: list[str] = []
    publication_state = "not-committed"
    try:
        with tempfile.TemporaryDirectory(prefix="room-store-copy-", dir=destination_context.private_dir) as temporary:
            stage_root = Path(temporary)
            protect_private_directory(stage_root)
            workspace_stage = stage_root / "workspace"
            source_workspace = source_body["workspace"]
            source_snapshot = source_context.store.snapshot(source_workspace["snapshot_id"])
            if source_snapshot.tree_id != source_workspace["tree_id"]:
                raise RoomStoreLifecycleError("source workspace tree identity does not match", code="source-snapshot-invalid")
            source_rows = _validate_snapshot_entries(source_context.store.entries(source_snapshot.snapshot_id))
            source_context.store.restore(source_snapshot.snapshot_id, workspace_stage)
            workspace_scan = scan_workspace_for_status(workspace_stage)
            if (
                set(source_rows) - {"."} != workspace_scan.paths
                or workspace_scan.logical_bytes != source_workspace["logical_bytes"]
                or workspace_scan.capture_policy_sha256 != source_body["capture_policy_sha256"]
            ):
                raise RoomStoreLifecycleError("restored source workspace does not match its descriptor", code="source-workspace-invalid")
            source_policy = load_capture_policy(workspace_stage)
            source_semantics = _semantic_workspace(workspace_stage, source_policy)

            component_result: dict[str, Any] = {name: None for name in source_body["components"]}
            component_data_added = 0
            try:
                component_lease = materialize_components(
                    source_descriptor,
                    source_context.store,
                    destination_context.private_dir,
                    cancellation,
                )
            except PortableExportError as error:
                raise RoomStoreLifecycleError(str(error), code="source-component-invalid") from None
            with component_lease:
                for name, source_component in source_body["components"].items():
                    if source_component is None:
                        continue
                    component_result[name], component_bytes = _copy_rebased_component(
                        source_component,
                        name,
                        component_lease,
                        destination_context,
                        destination_parent,
                        stage_root,
                        cancellation,
                        created_snapshot_ids,
                    )
                    component_data_added += component_bytes
                parent_snapshot_id = (
                    destination_parent.to_dict()["workspace"]["snapshot_id"]
                    if destination_parent is not None
                    else None
                )
                workspace_summary = destination_context.store.backup(
                    workspace_stage,
                    parent=parent_snapshot_id,
                    cancellation=cancellation,
                )
                workspace_snapshot_id = workspace_summary.snapshot_id or parent_snapshot_id
                if not workspace_snapshot_id:
                    raise RoomStoreLifecycleError("destination workspace backup produced no snapshot", code="workspace-backup-failed")
                workspace_snapshot = destination_context.store.snapshot(workspace_snapshot_id)
                if workspace_summary.snapshot_id:
                    created_snapshot_ids.append(workspace_snapshot_id)
                    if workspace_snapshot.parent_snapshot_id != parent_snapshot_id:
                        raise RoomStoreLifecycleError("destination workspace parent does not match", code="workspace-backup-failed")
                elif destination_parent is None or workspace_snapshot.tree_id != destination_parent.to_dict()["workspace"]["tree_id"]:
                    raise RoomStoreLifecycleError("destination workspace no-op has no matching parent", code="workspace-backup-failed")
                destination_rows = _validate_snapshot_entries(
                    destination_context.store.entries(workspace_snapshot_id)
                )
                if set(destination_rows) - {"."} != workspace_scan.paths:
                    raise RoomStoreLifecycleError("destination workspace inventory does not match", code="workspace-backup-failed")
                destination_verify = stage_root / "destination-workspace-verification"
                destination_context.store.restore(workspace_snapshot_id, destination_verify)
                destination_policy = load_capture_policy(destination_verify)
                if (
                    destination_policy.sha256 != source_policy.sha256
                    or _semantic_workspace(destination_verify, destination_policy) != source_semantics
                ):
                    raise RoomStoreLifecycleError("destination workspace content does not match the source", code="workspace-backup-failed")

                descriptor_body = {
                    "format_version": 1,
                    "payload_kind": "room-store-v1",
                    "logical_jat_id": uuid.uuid4().hex,
                    "dimension_id": destination_context.dimension.dimension_id,
                    "encryption_domain_id": destination_domain,
                    "room_id": destination_room_id,
                    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "origin_room_id": source_body.get("origin_room_id", source_body["room_id"]),
                    "capture_policy_sha256": workspace_scan.capture_policy_sha256,
                    "workspace": {
                        "engine": "restic",
                        "repository_id": destination_repo,
                        "repository_format": destination_context.repository_info.repository_format,
                        "snapshot_id": workspace_snapshot_id,
                        "tree_id": workspace_snapshot.tree_id,
                        "source_path": ".",
                        "logical_bytes": workspace_scan.logical_bytes,
                        "data_added": workspace_summary.data_added,
                        "data_added_packed": workspace_summary.data_added_packed,
                    },
                    "components": component_result,
                    "source": source_body["source"],
                    "producer": source_body["producer"],
                }
                if destination_parent is not None:
                    parent_body = destination_parent.to_dict()
                    descriptor_body["parent_logical_jat_id"] = parent_body["logical_jat_id"]
                    descriptor_body["workspace"]["parent_snapshot_id"] = parent_body["workspace"]["snapshot_id"]
                descriptor = LogicalJat.from_dict(descriptor_body)
                try:
                    publish_descriptor(
                        descriptor,
                        expected_etag=destination_context.catalog_etag,
                        workspace_signature=workspace_scan.signature,
                        signature_algorithm=workspace_scan.signature_algorithm,
                    )
                except Exception as error:
                    published = getattr(error, "published", None)
                    publication_state = (
                        "committed-verification-unknown"
                        if published is True
                        else "rejected"
                        if published is False
                        else "uncertain"
                    )
                    raise RoomStoreLifecycleError(
                        "destination catalog publication requires reconciliation",
                        code="copy-publication-uncertain",
                        result={
                            "publication_state": publication_state,
                            "candidate_snapshot_ids": list(created_snapshot_ids),
                            "descriptor_object_key": getattr(error, "descriptor_object_key", None),
                            "reconciliation_required": True,
                        },
                    ) from error
                publication_state = "committed"
                return {
                    "ok": True,
                    "status": "published",
                    "source_room_id": source_context.project_id,
                    "source_logical_jat_id": source_body["logical_jat_id"],
                    "destination_room_id": destination_room_id,
                    "logical_jat_id": descriptor_body["logical_jat_id"],
                    "workspace_snapshot_id": workspace_snapshot_id,
                    "data_added_bytes": workspace_summary.data_added,
                    "component_snapshot_ids": {
                        name: value["snapshot"]["snapshot_id"]
                        for name, value in component_result.items()
                        if value is not None
                    },
                    "data_uploaded": workspace_summary.data_added + component_data_added,
                }
    except RoomStoreLifecycleError as error:
        receipt_id = _cross_copy_failure_receipt(
            destination_context,
            operation_id,
            source_context,
            destination_room_id,
            publication_state,
            error,
            created_snapshot_ids,
        )
        if receipt_id:
            error.result["reconciliation_required"] = True
            error.result["receipt_id"] = receipt_id
        raise
    except Exception as error:
        receipt_id = _cross_copy_failure_receipt(
            destination_context,
            operation_id,
            source_context,
            destination_room_id,
            publication_state,
            error,
            created_snapshot_ids,
        )
        raise RoomStoreLifecycleError(
            f"cross-Dimension Copy failed ({type(error).__name__}); reconcile before retrying",
            code="cross-dimension-copy-failed",
            result={
                "reconciliation_required": receipt_id is not None,
                "receipt_id": receipt_id,
                "cause_type": type(error).__name__,
            },
        ) from error


def reconcile_room_store(context, descriptors=None) -> dict[str, Any]:
    if descriptors is None:
        from .room_store_bridge import load_logical_descriptors

        descriptors = load_logical_descriptors(context)
    index = build_reference_index(context.catalog, descriptors)
    report: ResticReachabilityReport = compare_restic_inventory(
        index,
        context.repository_info,
        context.store.snapshots(),
    )
    to_snapshot_ids = lambda refs: sorted(reference.snapshot_id for reference in refs)
    return {
        "ok": True,
        "dimension_id": index.dimension_id,
        "catalog_revision": index.catalog_revision,
        "repository_id": report.repository_id,
        "reference_index_fingerprint": index.fingerprint,
        "catalog_referenced": to_snapshot_ids(report.catalog_referenced),
        "descriptor_referenced": to_snapshot_ids(report.descriptor_referenced),
        "restic_only_orphans": to_snapshot_ids(report.restic_only_orphans),
        "missing_from_restic": to_snapshot_ids(report.missing_from_restic),
        "component_only": to_snapshot_ids(report.component_only),
        "legacy_objects": sorted(report.legacy_objects),
        "destructive_cleanup_performed": False,
    }


def verify_room_store(
    store,
    *,
    read_data: bool = False,
    read_data_subset: str | None = None,
) -> dict[str, Any]:
    if type(read_data) is not bool or (read_data and read_data_subset is not None):
        raise ValueError("Room Store verification scope is invalid")
    result = store.check(read_data=read_data, read_data_subset=read_data_subset)
    return {
        "ok": True,
        "status": "verified",
        "read_data": result.read_data,
        "read_data_subset": result.read_data_subset,
    }


def optimize_room_store(store, *, confirmed: bool = False) -> dict[str, Any]:
    if type(confirmed) is not bool:
        raise TypeError("Room Store optimize confirmation is invalid")
    result = store.prune(dry_run=not confirmed, confirmed=confirmed)
    return {
        "ok": True,
        "operation": result.operation,
        "status": "completed" if confirmed else "planned",
        "dry_run": result.dry_run,
    }


def remove_logical_catalog_records(
    context,
    identities,
    *,
    descriptors=None,
) -> LogicalCatalogRemoval:
    """CAS-remove logical/legacy catalog references before any Restic forget."""
    selected = frozenset(
        value if isinstance(value, LogicalJatIdentity) else LogicalJatIdentity(*value)
        for value in identities
    )
    if not selected:
        raise ValueError("at least one catalog record is required")
    if not context.catalog_etag:
        raise RoomStoreLifecycleError(
            "catalog revision is unavailable for safe deletion",
            code="catalog-revision-unavailable",
        )
    if descriptors is None:
        from .room_store_bridge import load_logical_descriptors

        descriptors = load_logical_descriptors(context)
    index = build_reference_index(context.catalog, descriptors)
    if any(identity not in index.records for identity in selected):
        raise ValueError("catalog record is not present in the selected Dimension")

    target_references = {
        reference
        for identity in selected
        for reference in index.records[identity].restic_snapshots
    }
    remaining_references = {
        reference
        for identity, record in index.records.items()
        if identity not in selected
        for reference in record.restic_snapshots
    }
    unreachable = target_references - remaining_references
    snapshot_trees = {
        reference: index.references[reference].tree_id
        for reference in unreachable
    }

    updated = context.catalog
    removable_objects: set[str] = set()
    for room_id in sorted({identity.room_id for identity in selected}):
        selected_room = {identity for identity in selected if identity.room_id == room_id}
        project = updated.body["projects"].get(room_id)
        if project is None:
            raise ValueError("Room is not present in the selected Dimension")
        all_room_ids = {
            LogicalJatIdentity(room_id, snapshot_id)
            for snapshot_id in project["snapshots"]
        }
        if selected_room == all_room_ids:
            updated, removable, _count = updated.remove_project(room_id)
            removable_objects.update(removable)
        else:
            for identity in sorted(selected_room):
                updated, removable, _room_removed = updated.remove_snapshot(
                    room_id,
                    identity.logical_jat_id,
                )
                removable_objects.update(removable)

    recipients = [
        context.material.recipient,
        *context.material.keyset.recovery_recipients,
    ]
    encrypted_catalog = _encrypt_catalog(updated, recipients, context.instance)
    operation_id = uuid.uuid4().hex
    try:
        context.backend.conditional_catalog_put(encrypted_catalog, context.catalog_etag)
    except Exception as error:  # noqa: BLE001 - provider publication errors carry commit certainty metadata.
        published = getattr(error, "published", None)
        state = "rejected" if published is False else "uncertain"
        _write_copy_receipt(
            context.instance,
            operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": operation_id,
                "status": "reconciliation-required",
                "publication_state": state,
                "room_ids": sorted({identity.room_id for identity in selected}),
                "catalog_revision": index.catalog_revision,
                "restic_snapshot_ids": sorted(reference.snapshot_id for reference in unreachable),
                "reconciliation_required": published is not False,
            },
        )
        raise RoomStoreLifecycleError(
            "catalog removal was not confirmed",
            code="catalog-removal-uncertain",
            result={
                "publication_state": state,
                "reconciliation_required": published is not False,
                "receipt_id": operation_id,
            },
        ) from None

    object_cleanup_pending: list[str] = []
    for object_key in sorted(removable_objects):
        try:
            context.backend.delete_object(object_key)
        except Exception:  # noqa: BLE001 - retain failed provider deletions for explicit reconciliation.
            object_cleanup_pending.append(object_key)
    if unreachable or object_cleanup_pending:
        _write_copy_receipt(
            context.instance,
            operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": operation_id,
                "status": "cleanup-pending",
                "publication_state": "committed",
                "catalog_revision": updated.body["revision"],
                "room_ids": sorted({identity.room_id for identity in selected}),
                "restic_snapshot_ids_to_recheck": sorted(
                    reference.snapshot_id for reference in unreachable
                ),
                "descriptor_object_keys_pending": object_cleanup_pending,
                "reconciliation_required": True,
            },
        )
    return LogicalCatalogRemoval(
        operation_id=operation_id,
        dimension_id=index.dimension_id,
        encryption_domain_id=index.encryption_domain_id,
        repository_id=index.repository_id or context.repository_info.repository_id,
        catalog_revision=updated.body["revision"],
        identities=selected,
        snapshot_trees=snapshot_trees,
        object_cleanup_pending=tuple(object_cleanup_pending),
    )


def complete_logical_catalog_removal(
    fresh_context,
    pending: LogicalCatalogRemoval,
    *,
    descriptors=None,
) -> dict[str, Any]:
    """Rebuild reachability after CAS, then forget only verified unreferenced IDs."""
    if not isinstance(pending, LogicalCatalogRemoval):
        raise TypeError("logical catalog removal receipt is invalid")
    if (
        fresh_context.dimension.dimension_id != pending.dimension_id
        or fresh_context.material.encryption_domain_id != pending.encryption_domain_id
        or fresh_context.repository_info.repository_id != pending.repository_id
        or fresh_context.catalog.body["revision"] < pending.catalog_revision
    ):
        raise RoomStoreLifecycleError(
            "fresh Room Store context does not match the delete receipt",
            code="delete-context-mismatch",
        )
    if pending.object_cleanup_pending:
        _write_copy_receipt(
            fresh_context.instance,
            pending.operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": pending.operation_id,
                "status": "cleanup-pending",
                "publication_state": "committed",
                "catalog_revision": fresh_context.catalog.body["revision"],
                "restic_snapshot_ids_to_recheck": sorted(
                    reference.snapshot_id for reference in pending.snapshot_trees
                ),
                "descriptor_object_keys_pending": list(pending.object_cleanup_pending),
                "reconciliation_required": True,
            },
        )
        return {
            "ok": True,
            "status": "cleanup-pending",
            "restic_snapshots_forgotten": [],
            "descriptor_object_keys_pending": len(pending.object_cleanup_pending),
            "reconciliation_required": True,
        }
    if any(
        identity.room_id in fresh_context.catalog.body["projects"]
        and identity.logical_jat_id
        in fresh_context.catalog.body["projects"][identity.room_id]["snapshots"]
        for identity in pending.identities
    ):
        return {
            "ok": True,
            "status": "catalog-reference-remains",
            "restic_snapshots_forgotten": [],
            "reconciliation_required": True,
        }
    try:
        if descriptors is None:
            from .room_store_bridge import load_logical_descriptors

            descriptors = load_logical_descriptors(fresh_context)
        index = build_reference_index(fresh_context.catalog, descriptors)
        inventory = tuple(fresh_context.store.snapshots())
        compare_restic_inventory(index, fresh_context.repository_info, inventory)
    except Exception as error:  # noqa: BLE001 - failed reachability proof must defer destructive cleanup.
        _write_copy_receipt(
            fresh_context.instance,
            pending.operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": pending.operation_id,
                "status": "restic-cleanup-pending",
                "publication_state": "committed",
                "catalog_revision": fresh_context.catalog.body["revision"],
                "restic_snapshot_ids_to_recheck": sorted(
                    reference.snapshot_id for reference in pending.snapshot_trees
                ),
                "reconciliation_required": True,
                "error_type": type(error).__name__,
            },
        )
        return {
            "ok": True,
            "status": "cleanup-pending",
            "restic_snapshots_forgotten": [],
            "receipt_id": pending.operation_id,
            "reconciliation_required": True,
        }
    candidates = frozenset(
        reference
        for reference in pending.snapshot_trees
        if reference not in index.references
    )
    still_referenced = frozenset(set(pending.snapshot_trees) - candidates)
    inventory_trees = {item.snapshot_id: item.tree_id for item in inventory}
    present: list[str] = []
    missing: list[str] = []
    for reference in candidates:
        observed_tree = inventory_trees.get(reference.snapshot_id)
        if observed_tree is None:
            missing.append(reference.snapshot_id)
        elif observed_tree != pending.snapshot_trees[reference]:
            raise RoomStoreLifecycleError(
                "Restic snapshot identity changed during delete",
                code="snapshot-tree-mismatch",
            )
        else:
            present.append(reference.snapshot_id)
    forgotten: list[str] = []
    if present:
        snapshot_ids = tuple(sorted(present))
        try:
            forget_plan = fresh_context.store.plan_forget(snapshot_ids)
            expected_trees = tuple(
                pending.snapshot_trees[ResticSnapshotRef(pending.repository_id, snapshot_id)]
                for snapshot_id in snapshot_ids
            )
            if (
                forget_plan.repository_id != pending.repository_id
                or forget_plan.snapshot_ids != snapshot_ids
                or forget_plan.tree_ids != expected_trees
            ):
                raise RoomStoreLifecycleError(
                    "Restic forget plan changed during delete",
                    code="forget-plan-mismatch",
                )
            fresh_context.store.forget(snapshot_ids, plan=forget_plan, confirmed=True)
            forgotten.extend(snapshot_ids)
        except RoomStoreLifecycleError:
            raise
        except Exception as error:  # noqa: BLE001 - preserve a cleanup receipt after a failed forget attempt.
            _write_copy_receipt(
                fresh_context.instance,
                pending.operation_id,
                {
                    "operation": "remove-logical-catalog-records",
                    "operation_id": pending.operation_id,
                    "status": "restic-cleanup-pending",
                    "publication_state": "committed",
                    "catalog_revision": fresh_context.catalog.body["revision"],
                    "restic_snapshot_ids_pending": present,
                    "reconciliation_required": True,
                    "error_type": type(error).__name__,
                },
            )
            return {
                "ok": True,
                "status": "cleanup-pending",
                "restic_snapshots_forgotten": [],
                "receipt_id": pending.operation_id,
                "reconciliation_required": True,
            }
    if missing or still_referenced:
        _write_copy_receipt(
            fresh_context.instance,
            pending.operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": pending.operation_id,
                "status": "restic-cleanup-pending",
                "publication_state": "committed",
                "catalog_revision": fresh_context.catalog.body["revision"],
                "still_referenced_snapshot_ids": sorted(
                    reference.snapshot_id for reference in still_referenced
                ),
                "missing_from_restic_snapshot_ids": sorted(missing),
                "reconciliation_required": True,
            },
        )
    else:
        _write_copy_receipt(
            fresh_context.instance,
            pending.operation_id,
            {
                "operation": "remove-logical-catalog-records",
                "operation_id": pending.operation_id,
                "status": "complete",
                "publication_state": "committed",
                "catalog_revision": fresh_context.catalog.body["revision"],
                "restic_snapshots_forgotten": sorted(forgotten),
                "reconciliation_required": False,
            },
        )
    return {
        "ok": True,
        "status": "removed",
        "restic_snapshots_forgotten": sorted(forgotten),
        "still_referenced_snapshot_ids": sorted(reference.snapshot_id for reference in still_referenced),
        "missing_from_restic_snapshot_ids": sorted(missing),
        "never_pruned": True,
        "never_unlocked": True,
    }
