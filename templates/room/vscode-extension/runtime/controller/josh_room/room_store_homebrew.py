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


def recipe_bytes(directory: Path) -> bytes:
    """Read a bounded Brewfile-only export; never traverse a Homebrew prefix."""
    directory = Path(directory).absolute()
    try:
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise RoomStoreHomebrewError("Homebrew recipe directory capture is not qualified on this platform")
        # Anchor every ancestor in a no-follow directory descriptor. The final
        # read never resolves a path through a replaceable ancestor again.
        descriptor = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in directory.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if set(os.listdir(descriptor)) != {"Brewfile"}:
                raise RoomStoreHomebrewError("Select a Brewfile-only recipe directory or a prepared recovery archive")
            target_info = os.stat("Brewfile", dir_fd=descriptor, follow_symlinks=False)
            target_descriptor = os.open("Brewfile", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        with os.fdopen(target_descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 1024 * 1024:
                raise RoomStoreHomebrewError("Brewfile must be a bounded regular file without hardlinks")
            data = handle.read(1024 * 1024 + 1)
            if len(data) != before.st_size or _signature(os.fstat(handle.fileno())) != _signature(before) or _signature(target_info) != _signature(before):
                raise RoomStoreHomebrewError("Homebrew recipe changed while reading")
        return data
    except OSError:
        raise RoomStoreHomebrewError("Homebrew recipe is unavailable") from None


def prepare_recipe_archive(directory: Path, output: Path) -> Path:
    """Package inert recipe bytes; JAT validates the resulting recovery archive."""
    import io
    import tarfile

    import zstandard

    data = recipe_bytes(directory)
    with Path(output).open("xb") as raw:
        protect_private_file(output)
        with zstandard.ZstdCompressor().stream_writer(raw, closefd=False) as compressed, tarfile.open(fileobj=compressed, mode="w|") as archive:
            member = tarfile.TarInfo("homebrew-recovery/Brewfile")
            member.mode = 0o600
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    if recipe_bytes(directory) != data:
        raise RoomStoreHomebrewError("Homebrew recipe changed during preparation")
    return Path(output)
