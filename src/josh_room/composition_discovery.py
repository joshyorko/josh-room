"""Read committed Catalog parents from the selected bucket's encrypted namespaces."""

from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from botocore.exceptions import BotoCoreError, ClientError

from .catalog import Catalog
from .composition_authority import CompositionAuthority

_PREFIX = "composition/v1/"
_PAGE_SIZE = 16
_MAX_PAGES = 4
_MAX_POINTS = 64


def discover_catalogs(backend, dimension, material):
    """Treat listed prefixes as hints and expose only authenticated committed pins."""
    result = {"catalogs": [], "catalog_discovery_partial": False}
    catalogs = result["catalogs"]
    tokens = set()
    seen = set()
    cursor = None
    points = 0
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
                    catalog = Catalog.from_body(state["dimension_catalog"])
                    parents = {}
                    for operation in state["operations"].values():
                        if operation["phase"] != "committed":
                            continue
                        pin = operation["parent_pin"]
                        parent = pin["room_id"]
                        snapshot = pin["logical_jat_id"]
                        record = catalog.resolve_snapshot(parent, snapshot)
                        if record["ciphertext_sha256"] != pin["descriptor_sha256"]:
                            raise ValueError("Catalog parent pin differs from its record")
                        parents.setdefault(parent, {})[snapshot] = {
                            "snapshot_id": snapshot, "created_at": record["created_at"],
                            "child_count": len(operation["child_pins"]),
                        }
                    for parent, records in parents.items():
                        newest = sorted(records.values(), key=lambda row: (row["created_at"], row["snapshot_id"]), reverse=True)
                        available = _MAX_POINTS - points
                        if len(newest) > available:
                            result["catalog_discovery_partial"] = True
                        if not available:
                            return result
                        catalogs.append({"namespace": namespace, "parent_room_id": parent,
                                         "display_name": "Catalog Room", "recovery_points": newest[:available]})
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
