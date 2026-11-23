"""Session-scoped workspace discovery. Only issued handles can reach a descriptor."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ..composition import portable_path
from ..directory_inventory import stamp
from .contract import Attachment, AttachmentRef, NodeState, ViewValidationError
from .room_provider import _alias_key, _classify_file, _safe_display_label, _safe_mount_id


_SCAN_BATCH = 128
_PAGE = 48
_MAX_CURSORS = 8
_MAX_HANDLES = 8192
_BLOCKED = {"reserved-boundary", "git-pointer-unsupported", "inaccessible-boundary"}


def _stale():
    return ViewValidationError("filesystem-stale", "workspace observation changed; reopen directory")


def _unknown():
    return ViewValidationError("filesystem-unknown", "filesystem handle is unavailable")


@dataclass(frozen=True)
class Entry:
    id: str
    parent: Entry | None
    parts: tuple[str, ...]
    info: os.stat_result

    def ancestors(self):
        result = []
        parent = self.parent
        while parent:
            result.append(parent)
            parent = parent.parent
        return list(reversed(result))

    def node(self):
        when = datetime.fromtimestamp(self.info.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        name = self.parts[-1] if self.parts else "/"
        label = _safe_display_label(name, self.id) if self.parts else "/"
        parent = self.parent.id if self.parent else None
        if stat.S_ISDIR(self.info.st_mode):
            return NodeState(self.id, parent, "directory", label, when, fields={"coverage": "not-examined"})
        kind = _classify_file(name)
        fields = {"file_bytes": self.info.st_size, "entry_kind": kind.node_kind, "preview_status": "not-examined"}
        if kind.language:
            fields["language"] = kind.language
        return NodeState(self.id, parent, kind.node_kind, label, when, kind=kind.legend_kind,
                         metric=self.info.st_size, fields=fields)


class SiblingScan:
    """Disk-backed alias admission and ordered paging. Never publish a partial sibling scan."""

    def __init__(self, entry, fd):
        self.entry = entry
        # open a fresh directory stream; duplicated file descriptions share offsets.
        self.fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        self.iterator = None
        self.db = None
        try:
            self.iterator = os.scandir(self.fd)
            self.db = sqlite3.connect("", check_same_thread=False)
            self.db.execute("pragma max_page_count=2048")  # 8 MiB per active cursor
            self.db.execute("pragma cache_size=-128")
            self.db.execute("create table siblings(alias text primary key, name text unique, sortkey text unique, ambiguous integer default 0)")
        except BaseException:
            self.close()
            raise
        self.after = ""
        self.examined = self.omitted = 0
        self.inaccessible = False

    def close(self):
        if self.iterator is not None:
            self.iterator.close()
            self.iterator = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.db is not None:
            self.db.close()
            self.db = None

    def advance(self):
        deadline = time.monotonic() + 0.05
        for _ in range(_SCAN_BATCH):
            item = next(self.iterator, None)
            if item is None:
                self.iterator.close()
                self.iterator = None
                return
            self.examined += 1
            try:
                item.name.encode("utf-8")
            except UnicodeEncodeError:
                self.omitted += 1
                continue
            key = ("0" if item.is_dir(follow_symlinks=False) else "1") + item.name
            self.db.execute(
                "insert into siblings(alias,name,sortkey) values(?,?,?) "
                "on conflict(alias) do update set ambiguous=ambiguous+1",
                (_alias_key(item.name), item.name, key),
            )
            if time.monotonic() >= deadline:
                break


class WorkspaceFilesystem:
    def __init__(self, provider):
        self.provider = provider
        self._lock = threading.RLock()
        self._key = secrets.token_bytes(32)
        self._generation = None
        self._policy_hash = None
        self._handles = OrderedDict()
        self._cursors = OrderedDict()

    def _clear(self):
        for scan in self._cursors.values():
            scan.close()
        self._cursors.clear()
        self._handles.clear()
        self._generation = None

    def close(self):
        with self._lock:
            self._clear()

    def _remember(self, entry):
        self._handles[entry.id] = entry
        self._handles.move_to_end(entry.id)
        if len(self._handles) > _MAX_HANDLES:
            self._handles.move_to_end("root")
            self._handles.popitem(last=False)

    def _bootstrap(self):
        fd, _ = self.provider._open_bound_root()
        try:
            info = os.fstat(fd)
            policy = self.provider._capture_policy(fd)
            root = self._handles.get("root")
            if (root is None or stamp(root.info) != stamp(info) or policy.sha256 != self._policy_hash):
                self._clear()
                self._generation = secrets.token_urlsafe(18)
                self._policy_hash = policy.sha256
                self._remember(Entry("root", None, (), info))
        finally:
            os.close(fd)

    def _boundary(self, entry, fd):
        return self.provider._dir_boundary_kind(fd, self.provider._root.joinpath(*entry.parts), is_root=not entry.parts)

    def _open_entry(self, entry):
        fd, _ = self.provider._open_bound_root()
        try:
            policy = self.provider._capture_policy(fd)
            if policy.sha256 != self._policy_hash:
                raise _stale()
            mount = _safe_mount_id(fd)
            if mount is None:
                raise ViewValidationError("workspace-platform-unsupported", "mount-aware filesystem browsing unavailable")
            chain = [*entry.ancestors(), entry]
            for index, component in enumerate(chain):
                if index:
                    name = component.parts[-1]
                    if policy.is_excluded("/".join(component.parts)):
                        raise _stale()
                    if stat.S_ISREG(component.info.st_mode):
                        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stamp(current) != stamp(component.info) or current.st_nlink != 1:
                            raise _stale()
                        break
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                if stamp(os.fstat(fd)) != stamp(component.info) or _safe_mount_id(fd) != mount:
                    raise _stale()
                if index < len(chain) - 1 and self._boundary(component, fd) in _BLOCKED:
                    raise _stale()
            return fd, policy
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _bound(self, object_id, generation):
        if generation != self._generation or generation is None:
            raise _stale()
        entry = self._handles.get(object_id)
        if entry is None:
            raise _unknown()
        self._handles.move_to_end(entry.id)
        try:
            fd, policy = self._open_entry(entry)
            try:
                yield entry, fd, policy
                fresh, _ = self._open_entry(entry)
                os.close(fresh)
            finally:
                os.close(fd)
        except ViewValidationError as error:
            if error.code in {"filesystem-stale", "workspace-binding-changed"}:
                self._clear()
            raise
        except (OSError, ValueError):
            self._clear()
            raise _stale() from None

    def _child(self, parent, fd, policy, name):
        parts = (*parent.parts, name)
        if policy.is_excluded("/".join(parts)):
            return None
        try:
            portable_path(name)
        except ValueError:
            return None
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if info.st_dev != parent.info.st_dev or not (stat.S_ISDIR(info.st_mode) or
                (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
            return None
        # O_PATH never reads a device or FIFO, even if the candidate was replaced.
        probe = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if stamp(os.fstat(probe)) != stamp(info) or _safe_mount_id(probe) != _safe_mount_id(fd):
                return None
        finally:
            os.close(probe)
        identity = f"{info.st_dev}:{info.st_ino}:" + "/".join(parts)
        token = hashlib.blake2s(identity.encode(), key=self._key, digest_size=16).hexdigest()
        child = Entry(f"n-{token}", parent, parts, info)
        self._remember(child)
        return child

    def _page(self, scan, entry, fd, policy, rows):
        candidates = scan.db.execute(
            "select name,sortkey,ambiguous from siblings where sortkey>? order by sortkey limit ?",
            (scan.after, _PAGE + 1),
        ).fetchall()
        for name, key, aliases in candidates[:_PAGE]:
            scan.after = key
            if aliases:
                scan.omitted += 1 + aliases
                continue
            try:
                child = self._child(entry, fd, policy, name)
            except OSError:
                scan.inaccessible = True
                child = None
            if child:
                rows.append(child.node().to_dict())
            else:
                scan.omitted += 1
        return len(candidates) > _PAGE

    def listing(self, object_id, generation=None, cursor=None):
        with self._lock:
            if object_id == "root" and generation is None and cursor is None:
                self._bootstrap()
                generation = self._generation
            with self._bound(object_id, generation) as (entry, fd, policy):
                if not stat.S_ISDIR(entry.info.st_mode):
                    raise _unknown()
                boundary = self._boundary(entry, fd)
                rows = []
                result = {"generation": generation, "directory": entry.node().to_dict(),
                          "ancestors": [a.node().to_dict() for a in entry.ancestors()],
                          "entries": rows, "cursor": None, "examined": 0, "omitted": 0}
                if boundary in _BLOCKED:
                    result["coverage"] = boundary
                    return result
                if cursor is not None:
                    scan = self._cursors.get(cursor)
                    if scan is None or scan.entry.id != object_id:
                        raise _unknown()
                    del self._cursors[cursor]
                else:
                    scan = SiblingScan(entry, fd)
                retained = False
                try:
                    if scan.iterator is not None:
                        scan.advance()
                    more = scan.iterator is not None or self._page(scan, entry, fd, policy, rows)
                    if more:
                        if len(self._cursors) >= _MAX_CURSORS:
                            _, evicted = self._cursors.popitem(last=False)
                            evicted.close()
                        result["cursor"] = secrets.token_urlsafe(18)
                        self._cursors[result["cursor"]] = scan
                        retained = True
                    result["coverage"] = "partial" if more else (
                        "inaccessible-boundary" if scan.inaccessible else "complete")
                except sqlite3.DatabaseError:
                    rows.clear()
                    result["coverage"] = "resource-limit"
                except OSError:
                    rows.clear()
                    result["coverage"] = "inaccessible-boundary"
                finally:
                    if not retained:
                        scan.close()
                result.update(examined=scan.examined, omitted=scan.omitted)
                return result

    def inspect(self, object_id, generation):
        with self._lock, self._bound(object_id, generation) as (entry, _, _policy):
            return {"generation": generation, "node": entry.node().to_dict(),
                    "ancestors": [a.node().to_dict() for a in entry.ancestors()]}

    def _content(self, entry, fd):
        node = entry.node()
        if node.node_kind == "directory":
            return node, None
        name = entry.parts[-1]
        directory = self.provider._root.joinpath(*entry.parts[:-1])
        kind = _classify_file(name)
        preview, _, _ = self.provider._build_file_preview(
            fd, directory, name, entry.info, kind, node.label, self.provider._max_preview_bytes)
        attachment = None
        refs = ()
        media = kind.embed_media_type
        if media and self.provider._include_attachments:
            limit = min(self.provider._max_attachment_bytes, 1024 * 1024)
            data = self.provider._read_regular_file_bytes(fd, directory, name, entry.info, limit)
            signatures = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff",
                          "application/pdf": b"%PDF-", "image/webp": b"RIFF"}
            if data and data.startswith(signatures[media]) and (media != "image/webp" or data[8:12] == b"WEBP"):
                attachment = Attachment("evidence" + Path(name).suffix.lower(), media, data)
                refs = (AttachmentRef(attachment.name, media, len(data), attachment.sha256),)
        fields = {**node.fields, "preview_status": preview.status}
        return replace(node, previews=(preview,), attachments=refs, fields=fields), attachment

    def preview(self, object_id, generation):
        with self._lock, self._bound(object_id, generation) as (entry, fd, _):
            node, _ = self._content(entry, fd)
            return {"generation": generation, "node": node.to_dict()}

    def attachment(self, object_id, generation, name, digest):
        with self._lock, self._bound(object_id, generation) as (entry, fd, _):
            _, attachment = self._content(entry, fd)
            if attachment and attachment.name == name and attachment.sha256 == digest:
                return attachment
            return None
