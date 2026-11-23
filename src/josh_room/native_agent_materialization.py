"""Atomic single-file native history publication without named plaintext staging."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from .cancellation import CLICancelled

MAX_BYTES = 16 * 1024 * 1024
_AT_EMPTY_PATH = 0x1000


class MaterializationError(RuntimeError):
    """A content-free failure; committed means a complete final file may exist."""

    def __init__(self, code: str, *, committed: bool = False):
        self.code = code
        self.committed = committed
        super().__init__(code)


def _identity(info):
    return info.st_dev, info.st_ino


def _private_directory(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise MaterializationError("materialization-unsafe-parent")
    return info


def _open_parent(parent):
    descriptor = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parent.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        _private_directory(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_parent(parent, descriptor):
    current = _open_parent(parent)
    try:
        if _identity(_private_directory(current)) != _identity(_private_directory(descriptor)):
            raise MaterializationError("materialization-parent-changed")
    finally:
        os.close(current)


def _link_anonymous(source, parent, name):
    try:
        link = ctypes.CDLL(None, use_errno=True).linkat
    except AttributeError:
        raise MaterializationError("anonymous-materialization-unavailable") from None
    link.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    link.restype = ctypes.c_int
    if link(source, b"", parent, os.fsencode(name), _AT_EMPTY_PATH):
        reason = "materialization-target-collision" if ctypes.get_errno() == errno.EEXIST else "anonymous-materialization-unavailable"
        raise MaterializationError(reason)


def anonymous_file(target: Path, payload: bytes, *, before_commit: Callable[[], None]) -> dict:
    """Publish one complete native file or leave no named plaintext temporary file.

    Before publication, closing the descriptor discards the anonymous inode.
    After publication, an interrupted operation can leave the complete final file.
    This does not promise secure block erasure or a multi-file transaction.
    """
    if not sys.platform.startswith("linux") or not hasattr(os, "O_TMPFILE"):
        raise MaterializationError("anonymous-materialization-unavailable")
    target = Path(target)
    if not target.is_absolute() or target.anchor != "/" or ".." in target.parts or "\x00" in str(target) or not target.name or not callable(before_commit):
        raise MaterializationError("materialization-invalid-input")
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_BYTES:
        raise MaterializationError("materialization-invalid-input")
    parent_fd = None
    temporary_fd = None
    committed = False
    try:
        parent_fd = _open_parent(target.parent)
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise MaterializationError("materialization-target-collision")
        temporary_fd = os.open(".", os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        info = os.fstat(temporary_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise MaterializationError("anonymous-materialization-unavailable")
        offset = 0
        while offset < len(payload):
            written = os.write(temporary_fd, payload[offset:])
            if written <= 0:
                raise MaterializationError("materialization-write-failed")
            offset += written
        os.fsync(temporary_fd)
        try:
            before_commit()
        except (OSError, ValueError, RuntimeError):
            raise MaterializationError("materialization-admission-denied") from None
        _check_parent(target.parent, parent_fd)
        _link_anonymous(temporary_fd, parent_fd, target.name)
        committed = True
        os.fsync(parent_fd)
        try:
            _check_parent(target.parent, parent_fd)
        except MaterializationError:
            raise MaterializationError("materialization-commit-uncertain", committed=True) from None
        published = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(published) != _identity(os.fstat(temporary_fd)):
            raise MaterializationError("materialization-commit-uncertain", committed=True)
    except (KeyboardInterrupt, CLICancelled):
        if temporary_fd is not None and os.fstat(temporary_fd).st_nlink:
            raise MaterializationError("materialization-commit-uncertain", committed=True) from None
        raise
    except MaterializationError:
        raise
    except OSError:
        code = "materialization-commit-uncertain" if committed else "anonymous-materialization-unavailable"
        raise MaterializationError(code, committed=committed) from None
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    return {"ok": True, "state": "history-materialized", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "native_resume_verified": False}
