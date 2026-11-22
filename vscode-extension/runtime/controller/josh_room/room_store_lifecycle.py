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

from . import native_agent_reference
from .cancellation import CLICancelled
from .catalog import corroborate_logical_snapshot
from .jat import require_serve_startup, run_extract, run_serve
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
    agent_reference_receipts: tuple[dict, ...] = ()


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
    result = {"ok": True, **inspect_logical_jat(record, descriptor)}
    agent_recovery = native_agent_reference.inspect_reference(context)
    if agent_recovery is not None:
        result["agent_recovery"] = agent_recovery
    return result


def export_logical_jat(
    context,
    *,
    jat_root,
    output,
    cancellation=None,
) -> dict[str, Any]:
    _record, descriptor = _selected_logical_record(context)
    native_agent_reference.preflight_export(context, output)
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
        raise RoomStoreLifecycleError(
            str(error), code="portable-export-failed", result={"diagnostic": str(error)}
        ) from None
    response = {
