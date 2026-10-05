"""Private cache of verified immutable Restic parent inventories."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from .private_paths import (
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)

_LIMIT = 128 * 1024 * 1024


def _identity(
    repository: str, repository_id: str, snapshot_id: str, tree_id: str
) -> dict:
    return {
        "repository_locator_sha256": hashlib.sha256(repository.encode()).hexdigest(),
        "repository_id": repository_id,
        "snapshot_id": snapshot_id,
        "tree_id": tree_id,
    }


def _path(cache_dir: Path, identity: dict) -> Path:
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return Path(cache_dir) / "parent-inventories" / (key + ".json")


def read_inventory(
    cache_dir: Path, repository: str, repository_id: str, snapshot_id: str, tree_id: str
):
    """Return only exact authenticated-parent evidence; uncertainty is a miss."""
    try:
        identity = _identity(repository, repository_id, snapshot_id, tree_id)
        path = _path(cache_dir, identity)
        verify_private_path(path.parent, directory=True)
        verify_private_path(path, directory=False)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > _LIMIT
        ):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return None
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return None
        value = json.loads(raw)
        if value["format_version"] != 1 or value["identity"] != identity:
            return None
        rows = value["rows"]
        from .restic_store import MAX_ENTRIES, SnapshotEntry

        if not isinstance(rows, list) or len(rows) > MAX_ENTRIES:
            return None
        if (
            hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
            != value["rows_sha256"]
        ):
            return None
        result = []
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 5
                or not isinstance(row[0], str)
                or not isinstance(row[1], str)
                or any(
                    item is not None
                    and (
                        not isinstance(item, int) or isinstance(item, bool) or item < 0
                    )
                    for item in row[2:4]
                )
                or (row[4] is not None and not isinstance(row[4], str))
            ):
                return None
            result.append(SnapshotEntry(*row))
        return result
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def write_inventory(
    cache_dir: Path,
    repository: str,
    repository_id: str,
    snapshot_id: str,
    tree_id: str,
    entries,
) -> None:
    temporary = None
    try:
        identity = _identity(repository, repository_id, snapshot_id, tree_id)
        path = _path(cache_dir, identity)
        rows = [
            [row.path, row.entry_type, row.size, row.mode, row.link_target]
            for row in entries
        ]
        value = {
            "format_version": 1,
            "identity": identity,
            "rows": rows,
            "rows_sha256": hashlib.sha256(
                json.dumps(rows, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        raw = json.dumps(value, separators=(",", ":")).encode()
        if len(raw) > _LIMIT:
            return
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        protect_private_directory(path.parent)
        fd, name = tempfile.mkstemp(prefix=".inventory.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        protect_private_file(temporary)
        os.replace(temporary, path)
    except (OSError, RuntimeError, ValueError, TypeError):
        return
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
