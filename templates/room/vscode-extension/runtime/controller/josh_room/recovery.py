"""Pinned, read-only Restic tree traversal and absent-destination recovery.

No archive extraction, Room hydration, catalog writes or fallback to latest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .private_paths import protect_private_directory
from .room_store_lifecycle import RoomStoreLifecycleError, _selected_logical_record
from .room_store_operations import _portable_path_key, _relative_parts, _rename_directory_noreplace

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_FILES = 10000
MAX_BYTES = 64 * 1024 * 1024


def fail(code="recovery-invalid"):
    raise RoomStoreLifecycleError("Historical recovery failed closed", code=code)


class Recovery:
    def __init__(self, context, cancellation=None):
        record, descriptor = _selected_logical_record(context)
        self.context = context
        self.body = descriptor.to_dict()
        self.store = context.store
        self.cancel = cancellation
        self.bytes_read = 0
        self.calls = 0
        self.workspace = self.body["workspace"]
        snapshot = self.store.snapshot(self.workspace["snapshot_id"])
        if snapshot.tree_id != self.workspace["tree_id"]:
            fail("recovery-tree-mismatch")
        self.identity = {
            "dimension_id": self.body["dimension_id"],
            "room_id": self.body["room_id"],
            "snapshot_id": record["snapshot_id"],
            "logical_jat_id": self.body["logical_jat_id"],
            "workspace_snapshot_id": snapshot.snapshot_id,
            "tree_id": snapshot.tree_id,
        }

    def checkpoint(self):
        if self.cancel is not None and self.cancel.cancelled:
            fail("cancelled")

    def blob(self, digest):
        self.checkpoint()
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            fail()
        # Reuse the sanitized, deadline/size-bounded authenticated subprocess.
        _, raw = self.store._capture(["cat", "blob", digest])
        self.calls += 1
        self.bytes_read += len(raw)
        if hashlib.sha256(raw).hexdigest() != digest:
            fail("recovery-integrity")
        return raw

    def nodes(self, tree):
        try:
            nodes = json.loads(self.blob(tree))["nodes"]
        except (KeyError, TypeError, ValueError):
            fail()
        if not isinstance(nodes, list):
            fail()
        seen = set()
        for node in nodes:
            if not isinstance(node, dict):
                fail()
            name = node.get("name")
            if not isinstance(name, str) or len(_relative_parts(name)) != 1:
                fail()
            key = _portable_path_key(name)
            if key in seen:
                fail("recovery-name-collision")
            seen.add(key)
        return nodes

    def selected(self, path):
        parts = _relative_parts(path, allow_root=True)
        node = {"type": "dir", "subtree": self.workspace["tree_id"], "name": "."}
        for part in parts:
            if node.get("type") != "dir":
                fail("recovery-unsafe-type")
            candidates = [row for row in self.nodes(node.get("subtree")) if row["name"] == part]
            if not candidates:
                fail("recovery-path-absent")
            node = candidates[0]
        return node

    def receipt(self, **extra):
        return {"ok": True, **self.identity, "mode": "on-demand listing",
                "integrity": "sha256-verified-tree-and-content-blobs",
                "blob_bytes_read": self.bytes_read, "blob_calls": self.calls,
                "network_bytes_transferred": None, **extra}

    def tree(self, path=".", offset=0, limit=100):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            fail()
        node = self.selected(path)
        if node.get("type") != "dir":
            fail("recovery-not-directory")
        nodes = self.nodes(node.get("subtree"))
        rows = [{"name": row["name"], "type": row.get("type"), "size": row.get("size"),
                 "mtime": row.get("mtime"), "recoverable": row.get("type") in {"file", "dir"}}
                for row in nodes[offset:offset + limit]]
        return self.receipt(path=path, entries=rows,
                            next_offset=offset + limit if offset + limit < len(nodes) else None)

    def content(self, node, budget=MAX_BYTES):
        if node.get("type") != "file" or node.get("links", 1) > 1:
            fail("recovery-unsafe-type")
        size = node.get("size")
        chunks = node.get("content")
        if type(size) is not int or not 0 <= size <= budget or not isinstance(chunks, list):
            fail("recovery-budget")
        data = bytearray()
        for digest in chunks:
            data.extend(self.blob(digest))
            if len(data) > size:
                fail("recovery-integrity")
        if len(data) != size:
            fail("recovery-integrity")
        return bytes(data)

    def inspect(self, path):
        node = self.selected(path)
        data = self.content(node)
        # Content display is deliberately withheld until trust/sensitivity admission
        # is implemented. Digests and type/size are safe for binary and text alike.
        return self.receipt(path=path, type="file", size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), preview="withheld-policy")

    def extract(self, path, destination, dry_run=False):
        if path == ".":
            fail("recovery-selection-required")
        node = self.selected(path)
        plan = []
        total = 0

        def visit(row, relative):
            nonlocal total
            self.checkpoint()
            if len(plan) >= MAX_FILES or row.get("type") not in {"file", "dir"}:
                fail("recovery-unsafe-type-or-budget")
            if row["name"] == ".josh-room.json":
                fail("recovery-marker-forbidden")
            size = row.get("size", 0) if row["type"] == "file" else 0
            if type(size) is not int or size < 0 or total + size > MAX_BYTES:
                fail("recovery-budget")
            total += size
            plan.append((relative, row))
            if row["type"] == "dir":
                for child in self.nodes(row.get("subtree")):
                    visit(child, relative / child["name"])
        visit(node, Path("selection"))
        if dry_run:
            return self.receipt(status="planned", entries=len(plan), logical_bytes=total)
        target = Path(destination).absolute()
        # Reject parent aliases, symlinks and Windows reparse points, including
        # ancestors; never follow a destination derived from historical metadata.
        for parent in (target.parent, *target.parent.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                fail("recovery-unsafe-destination")
        if target.exists() or target.is_symlink():
            fail("recovery-destination-exists")
        digests = []
        with tempfile.TemporaryDirectory(prefix=".room-recovery-", dir=target.parent) as temporary:
            stage = Path(temporary)
            protect_private_directory(stage)
            for relative, row in plan:
                self.checkpoint()
                output = stage / relative
                if row["type"] == "dir":
                    output.mkdir(mode=0o700)
                else:
                    data = self.content(row)
                    with output.open("xb") as stream:
                        os.chmod(output, 0o600)
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    digest = hashlib.sha256(data).hexdigest()
                    if hashlib.sha256(output.read_bytes()).hexdigest() != digest:
                        fail("recovery-integrity")
                    digests.append({"path": relative.as_posix().removeprefix("selection/"), "sha256": digest})
            self.checkpoint()
            _rename_directory_noreplace(stage / "selection", target)
        return self.receipt(status="recovered", entries=len(plan), files=len(digests),
                            logical_bytes=total, verified_files=digests)


def run_recovery(context, args):
    if args.snapshot == "latest":
        fail("recovery-exact-snapshot-required")
    recovery = Recovery(context)
    if args.recovery_command == "tree":
        return recovery.tree(args.path, args.offset, args.limit)
    if args.recovery_command == "inspect":
        return recovery.inspect(args.path)
    return recovery.extract(args.path, args.destination, args.dry_run)
