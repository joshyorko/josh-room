"""Read committed Catalog parents from the selected bucket's encrypted namespaces."""

from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import BotoCoreError, ClientError

from .catalog import Catalog
from .composition_authority import CompositionAuthority
from .composition_retirement import active_inventory

_PREFIX = "composition/v1/"
_PAGE_SIZE = 16
_MAX_PAGES = 4
_MAX_POINTS = 64
_CHILD_LABEL_BUDGET = 12 * 1024
_CHILD_HISTORY_BUDGET = 12 * 1024


def _child_history(parent, operations, active_rooms, projects):
    paths = {}
    pinned = set()
    active_points = set(active_rooms[parent]["recovery_points"])
    for operation in operations:
        if operation["phase"] != "committed" or operation["parent_pin"]["room_id"] != parent:
            continue
        for path, pin in operation["child_pins"].items():
            room = pin["room_id"]
            if room not in active_rooms:
                continue
            paths[room] = path
            if operation["parent_pin"]["logical_jat_id"] in active_points:
                pinned.add((room, pin["logical_jat_id"]))
    return [{
        "room_id": room, "path": path, "recovery_points": [{
            "snapshot_id": snapshot,
            "created_at": projects[room]["snapshots"][snapshot]["created_at"],
            "pinned": (room, snapshot) in pinned,
        } for snapshot in active_rooms[room]["recovery_points"]],
    } for room, path in sorted(paths.items(), key=lambda item: item[1])]


def discover_catalogs(backend, dimension, material):
    """Treat listed prefixes as hints and expose only authenticated committed pins."""
    result = {"catalogs": [], "catalog_discovery_partial": False}
    catalogs = result["catalogs"]
    tokens = set()
    seen = set()
    cursor = None
    points = 0
    child_label_bytes = 0
    child_history_bytes = 0
    with TemporaryDirectory(prefix="josh-room-catalog-list-") as tmp:
        for _ in range(_MAX_PAGES):
            try:
                page = backend.client.list_objects_v2(
                    Bucket=dimension.bucket, Prefix=_PREFIX, Delimiter="/", MaxKeys=_PAGE_SIZE,
                    **({"ContinuationToken": cursor} if cursor else {}),
                )
            except (BotoCoreError, ClientError):
                result["catalog_discovery_partial"] = True
                break
            prefixes = page.get("CommonPrefixes") or []
            if len(prefixes) > _PAGE_SIZE:
                result["catalog_discovery_partial"] = True
            for entry in prefixes[:_PAGE_SIZE]:
                match = re.fullmatch(r"composition/v1/([a-f0-9]{32})/", entry.get("Prefix", ""))
                if not match or match[1] in seen:
                    continue
                namespace = match[1]
                seen.add(namespace)
                try:
                    authority = CompositionAuthority(backend, dimension, namespace, material, Path(tmp))
                    envelope, _etag = authority.read()
                    state = (envelope or {}).get("state")
                    if state is None:
                        continue
                    inventory = active_inventory(envelope)
                    if not inventory["catalog_active"]:
                        continue
                    active_rooms = inventory["rooms"]
                    catalog = Catalog.from_body(state["dimension_catalog"])
                    operations = state["operations"].values()
                    parents = {op["parent_pin"]["room_id"]: {} for op in operations
                               if op["phase"] == "committed" and op["parent_pin"]["room_id"] in active_rooms}
                    for operation in operations:
                        if operation["phase"] != "committed":
                            continue
                        pin = operation["parent_pin"]
                        parent = pin["room_id"]
                        snapshot = pin["logical_jat_id"]
                        if parent not in parents or snapshot not in active_rooms[parent]["recovery_points"]:
                            continue
                        record = catalog.resolve_snapshot(parent, snapshot)
                        if record["ciphertext_sha256"] != pin["descriptor_sha256"]:
                            raise ValueError("Catalog parent pin differs from its record")
                        paths = sorted(operation["child_pins"])
                        point = {
                            "snapshot_id": snapshot, "created_at": record["created_at"],
                            "child_count": len(paths),
                        }
                        label_bytes = len(json.dumps(paths))
                        if child_label_bytes + label_bytes <= _CHILD_LABEL_BUDGET:
                            point["children"] = paths
                            child_label_bytes += label_bytes
                        else:
                            result["catalog_discovery_partial"] = True
                        parents.setdefault(parent, {})[snapshot] = point
                    for parent, records in parents.items():
                        newest = sorted(records.values(), key=lambda row: (row["created_at"], row["snapshot_id"]), reverse=True)
                        available = _MAX_POINTS - points
                        if len(newest) > available:
                            result["catalog_discovery_partial"] = True
                        if not available and newest:
                            return result
                        projects = catalog.body["projects"]
                        entry = {"namespace": namespace, "parent_room_id": parent,
                                 "display_name": projects[parent]["display_name"], "recovery_points": newest[:available]}
                        history = _child_history(parent, operations, active_rooms, projects)
                        history_size = len(json.dumps(history))
                        if child_history_bytes + history_size <= _CHILD_HISTORY_BUDGET:
                            entry["child_rooms"] = history
                            child_history_bytes += history_size
                        else:
                            result["catalog_discovery_partial"] = True
                        catalogs.append(entry)
                        points += min(len(newest), available)
                except (BotoCoreError, ClientError, KeyError, OSError, RuntimeError, ValueError):
                    result["catalog_discovery_partial"] = True
            if not page.get("IsTruncated"):
                return result
            cursor = page.get("NextContinuationToken")
            if not isinstance(cursor, str) or not cursor or cursor in tokens:
                result["catalog_discovery_partial"] = True
                break
            tokens.add(cursor)
        else:
            result["catalog_discovery_partial"] = True
    return result
