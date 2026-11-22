"""Descriptor-bound semantic workspace fingerprints for native references."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from . import workspace_policy

FORMAT = "josh-room.workspace-content.v1"
MAX_ENTRIES = 100_000
_CHUNK = 1024 * 1024


class NativeWorkspaceContentError(ValueError):
    """A bounded reason code without workspace paths or content."""


def _fail(code):
    raise NativeWorkspaceContentError(code)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_root(path):
    if not path.is_absolute() or str(path).startswith("//") or ".." in path.parts:
        _fail("native-workspace-content-path-invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _policy_digest(defaults, ignore, runtime_relative):
    digest = hashlib.sha256(defaults)
    digest.update(b"\0ignore-present\0" if ignore is not None else b"\0ignore-absent\0")
    if ignore is not None:
        digest.update(ignore)
    digest.update(b"\0active-runtime\0")
    digest.update((runtime_relative or "").encode("utf-8"))
    return digest.hexdigest()


def _runtime(root):
    value = os.environ.get("ROBOCORP_HOME")
    if not value:
        return None, None
    try:
        path = Path(value).resolve(strict=True)
        info = path.stat(follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    if not stat.S_ISDIR(info.st_mode):
        return None, None
    relative = None
    if path.is_relative_to(root):
        relative = path.relative_to(root).as_posix()
        if relative == "." or any(char in relative for char in "*?[]"):
            _fail("native-workspace-content-runtime-invalid")
    return relative, (str(path), info.st_dev, info.st_ino)


def _policy(root, descriptor):
    policy = workspace_policy.load_capture_policy(root, directory_fd=descriptor)
    defaults, _ = workspace_policy._read_defaults()
    ignore = workspace_policy._read_ignore(root, directory_fd=descriptor)
    if _policy_digest(defaults, ignore, None) != policy.sha256:
        _fail("native-workspace-content-changed")
    relative, runtime = _runtime(root)
    return dataclasses.replace(policy, sha256=_policy_digest(defaults, ignore, relative),
                               active_runtime_relative=relative), runtime


def _tree(descriptor, policy, device, *, contents):
    records = {}
    signatures = {}
    count = 0

    def visit(directory, prefix):
        nonlocal count
        before = os.fstat(directory)
        if not stat.S_ISDIR(before.st_mode) or before.st_dev != device:
            _fail("native-workspace-content-filesystem-boundary")
        signatures[prefix] = _signature(before)
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_ENTRIES:
                    _fail("native-workspace-content-limit")
                names.append(entry.name)
        for name in sorted(names):
            relative = prefix + "/" + name if prefix else name
            if policy.is_excluded(relative):
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if info.st_dev != device:
                _fail("native-workspace-content-filesystem-boundary")
            signatures[relative] = _signature(info)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                records[relative] = ("symlink", os.readlink(name, dir_fd=directory), mode)
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    if _signature(os.fstat(child)) != _signature(info):
                        _fail("native-workspace-content-changed")
                    records[relative] = ("dir", mode)
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if contents:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        if _signature(os.fstat(child)) != _signature(info):
                            _fail("native-workspace-content-changed")
                        digest = hashlib.sha256()
                        remaining = info.st_size
                        while remaining:
                            chunk = os.read(child, min(_CHUNK, remaining))
                            if not chunk:
                                _fail("native-workspace-content-changed")
                            digest.update(chunk)
                            remaining -= len(chunk)
                        if os.read(child, 1) or _signature(os.fstat(child)) != _signature(info):
                            _fail("native-workspace-content-changed")
                        records[relative] = ("file", info.st_size, digest.hexdigest(), mode)
                    finally:
                        os.close(child)
            else:
                _fail("native-workspace-content-entry-unsupported")
            if _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _signature(info):
                _fail("native-workspace-content-changed")
        if _signature(os.fstat(directory)) != _signature(before):
            _fail("native-workspace-content-changed")

    visit(descriptor, "")
    return records, signatures


def fingerprint(workspace, *, expected_policy_sha256):
    """Hash included bytes/modes/paths without following workspace symlinks."""
    if not sys.platform.startswith("linux") or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY")):
        _fail("native-workspace-content-unavailable")
    if not isinstance(expected_policy_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_policy_sha256):
        _fail("native-workspace-content-policy-invalid")
    descriptor = None
    try:
        root = Path(workspace)
        descriptor = _open_root(root)
        info = os.fstat(descriptor)
        policy, runtime = _policy(root, descriptor)
        if policy.sha256 != expected_policy_sha256:
            _fail("native-workspace-content-policy-mismatch")
        records, signatures = _tree(descriptor, policy, info.st_dev, contents=True)
        from .restic_store import SnapshotEntry
        from .room_store_operations import (
            RoomStoreOperationsError,
            _validate_snapshot_entries,
        )

        try:
            _validate_snapshot_entries(
                SnapshotEntry(path, value[0], value[1] if value[0] == "file" else 0, value[-1],
                              value[1] if value[0] == "symlink" else None)
                for path, value in records.items()
            )
        except RoomStoreOperationsError:
            _fail("native-workspace-content-entry-unsupported")
        _, verified = _tree(descriptor, policy, info.st_dev, contents=False)
        if verified != signatures or _policy(root, descriptor) != (policy, runtime):
            _fail("native-workspace-content-changed")
        attached = _open_root(root)
        try:
            current = os.fstat(attached)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                _fail("native-workspace-content-changed")
        finally:
            os.close(attached)
        body = {"format": FORMAT, "capture_policy_sha256": policy.sha256,
                "capture_mode": policy.capture_mode, "entries": records}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    except NativeWorkspaceContentError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        raise NativeWorkspaceContentError("native-workspace-content-unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
