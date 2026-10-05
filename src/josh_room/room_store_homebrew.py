"""Persist exact saved Homebrew recovery bytes after JAT-owned validation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .private_paths import protect_private_directory, protect_private_file

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class RoomStoreHomebrewError(RuntimeError):
    """Path-free Homebrew component failure."""


def _cancelled(cancellation) -> None:
    if cancellation is not None and cancellation.cancelled:
        raise RoomStoreHomebrewError("Homebrew component capture was cancelled")


def _signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def capture_homebrew_component(
    *, archive, prior_component, repository_id, repository_format, restic, validator, cancellation=None,
) -> dict:
    _cancelled(cancellation)
    if repository_format != 2 or not isinstance(repository_id, str) or _SHA256.fullmatch(repository_id) is None:
        raise RoomStoreHomebrewError("Homebrew repository identity is invalid")
    prior_snapshot = None
    if prior_component is not None:
        prior_snapshot = prior_component.get("snapshot")
        if (
            prior_component.get("kind") != "homebrew-recovery"
            or not isinstance(prior_snapshot, dict)
            or prior_snapshot.get("repository_id") != repository_id
            or prior_snapshot.get("repository_format") != repository_format
            or any(_SHA256.fullmatch(str(prior_snapshot.get(field, ""))) is None for field in ("snapshot_id", "tree_id"))
        ):
            raise RoomStoreHomebrewError("prior Homebrew component belongs to another Room Store")
    archive = Path(archive)
    try:
        if not stat.S_ISREG(archive.lstat().st_mode):
            raise RoomStoreHomebrewError("Homebrew archive must be a bounded regular file")
        descriptor = os.open(archive, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_ARCHIVE_BYTES:
                raise RoomStoreHomebrewError("Homebrew archive must be a bounded regular file")
            digest = hashlib.sha256()
            read = 0
            while block := source.read(1024 * 1024):
                _cancelled(cancellation)
                read += len(block)
                if read > MAX_ARCHIVE_BYTES:
                    raise RoomStoreHomebrewError("Homebrew archive exceeds its input limit")
                digest.update(block)
            sha256 = digest.hexdigest()
            if (_signature(os.fstat(source.fileno())) != _signature(before)
                    or _signature(archive.lstat()) != _signature(before) or read != before.st_size):
                raise RoomStoreHomebrewError("Homebrew archive changed during capture")
            if prior_component is not None and (
                prior_component.get("archive_sha256") == sha256
                and prior_component.get("archive_size") == before.st_size
                and prior_component.get("member_basename") == "homebrew-recovery.tar.zst"
            ):
                return dict(prior_component)
            with tempfile.TemporaryDirectory(prefix="josh-room-brew-") as temporary:
                stage = Path(temporary)
                protect_private_directory(stage)
                owned = stage / "homebrew-recovery.tar.zst"
                source.seek(0)
                copied = 0
                copied_digest = hashlib.sha256()
                with owned.open("xb") as output:
                    protect_private_file(owned)
                    while block := source.read(1024 * 1024):
                        _cancelled(cancellation)
                        copied += len(block)
                        if copied > MAX_ARCHIVE_BYTES:
                            raise RoomStoreHomebrewError("Homebrew archive exceeds its input limit")
                        output.write(block)
                        copied_digest.update(block)
                if copied != before.st_size or copied_digest.hexdigest() != sha256:
                    raise RoomStoreHomebrewError("Homebrew archive changed during capture")
                try:
                    validator().validate_brew_archive(owned)
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise RoomStoreHomebrewError("saved Homebrew archive failed JAT validation") from None
                _cancelled(cancellation)
                summary = restic.backup(stage, parent=prior_snapshot["snapshot_id"] if prior_snapshot else None, cancellation=cancellation)
                if _SHA256.fullmatch(str(summary.snapshot_id)) is None:
                    raise RoomStoreHomebrewError("Restic did not capture the Homebrew component")
                saved = restic.snapshot(summary.snapshot_id)
                if _signature(archive.lstat()) != _signature(before):
                    raise RoomStoreHomebrewError("Homebrew archive changed during capture")
                return {
                    "kind": "homebrew-recovery",
                    "snapshot": {"repository_id": repository_id, "repository_format": repository_format,
                                 "snapshot_id": summary.snapshot_id, "tree_id": saved.tree_id},
                    "archive_sha256": sha256, "archive_size": before.st_size,
                    "member_basename": "homebrew-recovery.tar.zst",
                }
    except RoomStoreHomebrewError:
        raise
    except OSError:
        raise RoomStoreHomebrewError("saved Homebrew archive is unavailable") from None
