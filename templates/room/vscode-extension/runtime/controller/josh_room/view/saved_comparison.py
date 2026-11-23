"""Read-only comparison of one admitted live object with one authenticated saved point."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
from datetime import datetime
from pathlib import Path

from ..recovery_text import MAX_TEXT_BYTES, text_preview
from .contract import ViewValidationError
from .room_provider import _classify_file
from .sanitizer import DEFAULT_SANITIZATION_POLICY, _check_json_structure_safe, _check_safe_text

MAX_FILE = 1024 * 1024
MAX_WIRE = 65536
HEX = re.compile(r"[a-f0-9]{64}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def fail(code="comparison-unavailable"):
    raise ViewValidationError(code, "Saved file comparison unavailable")


def _text(name, data, policy, max_bytes):
    kind = _classify_file(Path(name).name)
    if len(data) > max_bytes or kind.is_secret or kind.node_kind == "artifact" or text_preview(name, data)["state"] != "available":
        return None
    body = data.decode("utf-8")
    try:
        _check_safe_text(body, "comparison", max_length=min(MAX_TEXT_BYTES, policy.max_preview_body_length),
                         max_digit_run=policy.max_digit_run, allow_newlines=True)
        if kind.media_type == "application/json":
            _check_json_structure_safe(json.loads(body), "comparison", policy=policy)
    except (ValueError, RecursionError):
        return None
    return body


def _file_version(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink, info.st_ctime_ns, info.st_mtime_ns)


def _local_file(provider, entry, directory, policy):
    name = entry.parts[-1]
    if _classify_file(name).is_secret or not stat.S_ISREG(entry.info.st_mode) or entry.info.st_nlink != 1:
        fail("comparison-unsupported")
    if entry.info.st_size > MAX_FILE:
        return {"size": entry.info.st_size, "sha256": None}, None
    data = provider._read_regular_file_bytes(directory, provider._root.joinpath(*entry.parts[:-1]),
                                             name, entry.info, MAX_FILE)
    if data is None:
        fail("filesystem-stale")
    allowed = provider._include_previews and len(data) <= provider._max_preview_bytes
    text = _text("/".join(entry.parts), data, policy, provider._max_preview_bytes) if allowed else None
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, text


def _point(value):
    snapshot, date = value["snapshot"], value["date"]
    if not LABEL.fullmatch(snapshot) or snapshot == "latest" or len(date) > 40:
        fail()
    datetime.fromisoformat(date)
    return {"snapshot": snapshot, "date": date}


def _historical_file(name, value, policy=DEFAULT_SANITIZATION_POLICY, max_bytes=MAX_TEXT_BYTES):
    if value is None:
        return None, ""
    size, digest = value["size"], value["sha256"]
    if (value["type"] != "file" or type(size) is not int or size < 0
            or not ((digest is None and size > MAX_FILE) or (isinstance(digest, str) and HEX.fullmatch(digest)))):
        fail()
    envelope = value.get("text_preview", {})
    summary = {"size": size, "sha256": digest}
    if envelope.get("state") != "available":
        return summary, None
    if (size > MAX_TEXT_BYTES or envelope.get("encoding") != "base64-utf8"
            or envelope.get("sha256") != digest or envelope.get("size") != size):
        fail()
    data = base64.b64decode(envelope["data"], validate=True)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        fail()
    return summary, _text(name, data, policy, max_bytes)


def _query(socket_path, request):
    payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
    if len(payload) > 8192:
        fail("comparison-unsupported")
    address = Path(socket_path)
    parent = address.parent.stat(follow_symlinks=False)
    if not address.is_absolute() or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        fail()
    deadline = time.monotonic() + 45
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(45)
        peer.connect(str(address))
        _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            fail()
        peer.sendall(payload)
        data = bytearray()
        while not data.endswith(b"\n"):
            peer.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = peer.recv(min(8192, MAX_WIRE + 1 - len(data)))
            if not chunk or len(data) + len(chunk) > MAX_WIRE or time.monotonic() > deadline:
                fail()
            data.extend(chunk)
    result = json.loads(data)
    if "error" in result:
        code = result["error"]
        fail("comparison-" + code if code in {"unsupported", "unavailable", "stale", "busy"} else "comparison-unavailable")
    return result


class SavedComparison:
    def __init__(self, socket_path, policy=DEFAULT_SANITIZATION_POLICY):
        self.socket_path = socket_path
        self.policy = policy

    def query(self, adapter, object_id, generation, point=None):
        try:
            return self._query(adapter, object_id, generation, point)
        except ViewValidationError:
            raise
        except (ValueError, TypeError, KeyError, OSError, IndexError):
            fail()

    def _query(self, adapter, object_id, generation, point):
        with adapter._lock, adapter._bound(object_id, generation) as (entry, _directory, _policy):
            if not entry.parts or not stat.S_ISREG(entry.info.st_mode) or _classify_file(entry.parts[-1]).is_secret:
                fail("comparison-unsupported")
            path = "/".join(entry.parts)
            version = _file_version(entry.info)
        request = {"op": "points"} if point is None else {"op": "inspect", "point": point, "path": path}
        result = _query(self.socket_path, request)
        with adapter._lock, adapter._bound(object_id, generation) as (entry, directory, _policy):
            if _file_version(entry.info) != version or "/".join(entry.parts) != path:
                fail("filesystem-stale")
            if point is None:
                points = result["points"]
                if not isinstance(points, list) or len(points) > 256 or type(result["portable"]) is not int or result["portable"] < 0:
                    fail()
                rows = []
                for row in points:
                    if not LABEL.fullmatch(row["id"]):
                        fail()
                    rows.append({**_point(row), "id": row["id"]})
                return {"points": rows, "portable": result["portable"]}
            if not HEX.fullmatch(result["tree_id"]) or not HEX.fullmatch(result["workspace_snapshot_id"]):
                fail()
            source = _point(result)
            saved, before = _historical_file(path, result["file"], self.policy, adapter.provider._max_preview_bytes)
            live, after = _local_file(adapter.provider, entry, directory, self.policy)
            if saved is None:
                change = "added"
            elif saved["size"] != live["size"]:
                change = "modified"
            elif not live["sha256"] or not saved["sha256"]:
                change = "unknown"
            else:
                change = "unchanged" if saved == live else "modified"
            diff = None
            if before is not None and after is not None:
                lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                             fromfile="saved", tofile="live")
                diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
            return {**source, "tree_id": result["tree_id"], "workspace_snapshot_id": result["workspace_snapshot_id"],
                    "saved": saved, "live": live, "change": change, "unified_diff": diff}
