"""No-follow admission of local Catalog child bindings before publication."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .composition import portable_path
from .workspace_onboarding import _directory
from .workspace_state import context_status


def admit_catalog_child(workspace, row, binding, namespace, allowed_metadata_split=None):
    """Return the verified marker or None for an absent directory entry."""
    workspace = Path(workspace)
    portable_path(row["path"])
    parts = row["path"].split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = _directory(workspace)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                return None
            os.close(current)
            current = child
        held = Path(f"/proc/{os.getpid()}/fd/{current}")
        marker_info = os.stat(".josh-room.json", dir_fd=current, follow_symlinks=False)
        if marker_info.st_nlink != 1:
            raise ValueError("child marker is not a private regular file")
        marker = context_status(held)
        expected = {
            "composition_namespace": namespace, "project_id": row["room_id"],
            "dimension_id": binding["dimension_id"], "encryption_domain_id": binding["encryption_domain_id"],
        }
        if not marker.get("path_matches") or any(marker.get(key) != value for key, value in expected.items()):
            raise ValueError("child marker differs from the Catalog binding")
        fd = os.open(".josh-room-split.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                raise ValueError("child namespace guard is invalid")
            guard = json.loads(stream.read(4097))
        snapshot = marker["snapshot_id"]
        if not isinstance(guard, dict) or set(guard) != {"namespace", "room_id", "snapshot_id"}:
            raise ValueError("child namespace guard is invalid")
        if guard["namespace"] != namespace or guard["room_id"] != row["room_id"]:
            raise ValueError("child namespace guard differs from the Catalog binding")
        if guard["snapshot_id"] != snapshot and (snapshot, guard["snapshot_id"]) != allowed_metadata_split:
            raise ValueError("child namespace guard and marker select different recovery points")
        actual = os.stat(workspace / row["path"], follow_symlinks=False)
        opened = os.fstat(current)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("child directory changed during admission")
        return marker
    except (OSError, ValueError) as error:
        raise ValueError("Catalog child path is occupied, unreadable or has an invalid Room binding") from error
    finally:
        os.close(current)
