"""Save and staged-restore orchestration for logical Room Store JATs.

Provider catalog encryption/publication remains an injected boundary until
the Room Store catalog format is admitted. The callback must encrypt the
canonical descriptor with the selected Dimension material and conditionally
publish the catalog using its observed revision/ETag.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import errno
import hashlib
import json
import os
import posixpath
import re
import stat
import tempfile
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth import bind_room_store_repository, ensure_room_store_keyset
from .cancellation import CLICancelled, defer_sigterm_cancellation
from .catalog import CatalogConflict
from .logical_jat import LogicalJat
from .restic_store import (
    BackupProgress,
    ResticStore,
    ResticStoreError,
    ResticStoreErrorCode,
    SnapshotEntry,
)
from .windows_file_metadata import WindowsFileMetadataError
from .windows_file_metadata import change_time_ns as _native_windows_change_time_ns
from .workspace_policy import CapturePolicy, load_capture_policy

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ENTRY_TYPES = {"dir", "file", "symlink"}
_WINDOWS_HOST = os.name == "nt"


def _windows_change_time_ns(path: Path, expected_stat: os.stat_result) -> int:
    try:
        return _native_windows_change_time_ns(path, expected_stat)
    except WindowsFileMetadataError as error:
        raise RoomStoreOperationsError(
            "workspace changed during safety scan"
        ) from error


class RoomStoreOperationsError(RuntimeError):
    """Stable, path-free failure at the Room Store operation boundary."""

    def __init__(
        self, message: str, *, deletion_confirmation_token: str | None = None
    ) -> None:
        self.deletion_confirmation_token = deletion_confirmation_token
        super().__init__(message)


class RoomStorePublicationError(RoomStoreOperationsError):
    def __init__(self, error: BaseException, publication_state: str) -> None:
        self.publication_state = publication_state
        self.reconciliation_required = publication_state == "uncertain"
        self.marker_state = (
            "stale"
            if publication_state == "committed-marker-stale"
            else "updated"
            if publication_state in {"committed", "committed-verification-unknown"}
            else "unchanged"
        )
        self.result = {
            "ok": False,
            "publication_state": publication_state,
            "marker_state": self.marker_state,
            "error_type": type(error).__name__,
        }
        if self.reconciliation_required:
            self.result["reconciliation_required"] = True
        super().__init__(f"Room Store catalog publication {publication_state}")


@dataclass(frozen=True, slots=True)
class WorkspaceScan:
    signature: str
    signature_algorithm: str
    capture_policy_sha256: str
    paths: frozenset[str]
    logical_bytes: int


@dataclass(frozen=True, slots=True)
class SavePreview:
    scanned_bytes: int
    workspace_signature: str
    signature_algorithm: str
    capture_policy_sha256: str
    previous_entry_count: int
    current_entry_count: int
    deleted_paths: tuple[str, ...]
    deletion_confirmation_token: str | None
    restic_added_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SaveResult:
    status: str
    descriptor: LogicalJat | None
    scanned_bytes: int
    data_added_bytes: int
    snapshot_id: str | None
    publication_state: str
    workspace_signature: str
    signature_algorithm: str
    capture_policy_sha256: str


@dataclass(frozen=True, slots=True)
class RestoreResult:
    descriptor: LogicalJat
    destination: Path


def _validate_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or any(character in name for character in '<>:"|?*\\\x00')
        or name.endswith((" ", "."))
        or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise RoomStoreOperationsError("workspace path cannot be restored safely")


def _relative_parts(value: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if allow_root and value == ".":
        return ()
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(("/", "\\"))
        or "\\" in value
    ):
        raise RoomStoreOperationsError("snapshot contains an unsafe path")
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RoomStoreOperationsError("snapshot contains an unsafe path")
    for part in parts:
        _validate_name(part)
    return parts


def _portable_path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _require_same_device(root_device: int, device: int) -> None:
    if device != root_device:
        raise RoomStoreOperationsError("workspace crosses a filesystem boundary")


def _validate_link_graph(entries: Mapping[str, SnapshotEntry]) -> None:
    links = {
        path: row.link_target
        for path, row in entries.items()
        if row.entry_type == "symlink"
    }
    for original_path, original_target in links.items():
        if (
            not isinstance(original_target, str)
            or not original_target
            or original_target.startswith(("/", "\\"))
            or "\\" in original_target
            or re.match(r"^[A-Za-z]:", original_target)
        ):
            raise RoomStoreOperationsError("snapshot contains an unsafe symlink")
        parent = posixpath.dirname(original_path)
        stack = [] if not parent else parent.split("/")
        pending = original_target.split("/")
        seen: set[str] = set()
        while pending:
            if "\\" in "/".join(pending) or (
                pending and re.match(r"^[A-Za-z]:", pending[0])
            ):
                raise RoomStoreOperationsError("snapshot contains an unsafe symlink")
            part = pending.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                if not stack:
                    raise RoomStoreOperationsError(
                        "snapshot contains an external symlink"
                    )
                stack.pop()
                continue
            _validate_name(part)
            candidate = "/".join((*stack, part)) or "."
            row = entries.get(candidate)
            if row is None:
                if pending:
                    raise RoomStoreOperationsError(
                        "snapshot contains a dangling symlink parent"
                    )
                stack.append(part)
                continue
            if row.entry_type == "symlink":
                if candidate in seen:
                    raise RoomStoreOperationsError("snapshot contains a cyclic symlink")
                seen.add(candidate)
                target = links[candidate]
                if (
                    not isinstance(target, str)
                    or not target
                    or target.startswith(("/", "\\"))
                    or "\\" in target
                    or re.match(r"^[A-Za-z]:", target)
                ):
                    raise RoomStoreOperationsError(
                        "snapshot contains an unsafe symlink"
                    )
                pending = target.split("/") + pending
                continue
            if pending and row.entry_type != "dir":
                raise RoomStoreOperationsError(
                    "snapshot symlink traverses a non-directory"
                )
            stack.append(part)
        resolved = "/".join(stack) or "."
        if resolved not in entries:
            raise RoomStoreOperationsError("snapshot contains a dangling symlink")


def _validate_snapshot_entries(
    rows: Iterable[SnapshotEntry],
) -> dict[str, SnapshotEntry]:
    entries: dict[str, SnapshotEntry] = {}
    folded: set[str] = set()
    for row in rows:
        parts = _relative_parts(row.path, allow_root=True)
        if row.path == ".":
            if row.entry_type != "dir":
                raise RoomStoreOperationsError("snapshot root is not a directory")
            if "." in entries:
                raise RoomStoreOperationsError("snapshot contains duplicate paths")
            entries["."] = row
            continue
        path = "/".join(parts)
        if path in entries or _portable_path_key(path) in folded:
            raise RoomStoreOperationsError("snapshot contains duplicate paths")
        folded.add(_portable_path_key(path))
        if row.entry_type not in _ENTRY_TYPES:
            raise RoomStoreOperationsError("snapshot contains a special file")
        if row.entry_type == "symlink" and row.size not in {None, 0}:
            # Restic reports the target length for links; size is not a data file.
            pass
        entries[path] = row
    for path in entries:
        parent = posixpath.dirname(path)
        while parent:
            parent_row = entries.get(parent)
            if parent_row is None or parent_row.entry_type != "dir":
                raise RoomStoreOperationsError("snapshot path has no directory parent")
            parent = posixpath.dirname(parent)
    _validate_link_graph(entries)
    return entries


def _scan_workspace(root: Path, policy: CapturePolicy | None) -> WorkspaceScan:
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise RoomStoreOperationsError("workspace is not a directory")
        root_device = resolved_root.stat().st_dev
    except OSError as error:
        raise RoomStoreOperationsError("workspace is unavailable") from error

    records: list[bytes] = []
    paths: set[str] = set()
    folded_paths: set[str] = set()
    logical_bytes = 0

    def visit(directory: Path) -> None:
        nonlocal logical_bytes
        try:
            _require_same_device(
                root_device, directory.stat(follow_symlinks=False).st_dev
            )
        except OSError as error:
            raise RoomStoreOperationsError(
                "workspace cannot be scanned safely"
            ) from error
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise RoomStoreOperationsError(
                "workspace cannot be scanned safely"
            ) from error
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(resolved_root).as_posix()
            if policy is not None and policy.is_excluded(relative):
                continue
            _relative_parts(relative)
            if _portable_path_key(relative) in folded_paths:
                raise RoomStoreOperationsError(
                    "workspace has paths that conflict on Windows"
                )
            folded_paths.add(_portable_path_key(relative))
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise RoomStoreOperationsError(
                    "workspace changed during safety scan"
                ) from error
            mode = stat.S_IMODE(metadata.st_mode)
            _require_same_device(root_device, metadata.st_dev)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if (
                    not target
                    or os.path.isabs(target)
                    or "\\" in target
                    or re.match(r"^[A-Za-z]:", target)
                ):
                    raise RoomStoreOperationsError(
                        "workspace contains an unsafe symlink"
                    )
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(resolved_root)
                    _require_same_device(root_device, resolved.stat().st_dev)
                except (OSError, RuntimeError, ValueError) as error:
                    raise RoomStoreOperationsError(
                        "workspace contains a dangling, cyclic, or external symlink"
                    ) from error
                paths.add(relative)
                records.append(f"{relative}\0link\0{target}\0{mode}".encode())
            elif stat.S_ISDIR(metadata.st_mode):
                paths.add(relative)
                records.append(f"{relative}\0dir\0{mode}".encode())
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                paths.add(relative)
                logical_bytes += metadata.st_size
                change_time = (
                    _windows_change_time_ns(path, metadata)
                    if _WINDOWS_HOST
                    else metadata.st_ctime_ns
                )
                records.append(
                    f"{relative}\0file\0{metadata.st_size}\0{metadata.st_mtime_ns}\0{change_time}\0{mode}".encode()
                )
            else:
                raise RoomStoreOperationsError("workspace contains a special file")

    visit(resolved_root)
    digest = hashlib.sha256(b"\n".join(records)).hexdigest()
    return WorkspaceScan(
        digest,
        "josh-room-stat-v1",
        policy.sha256 if policy is not None else "",
        frozenset(paths),
        logical_bytes,
    )


def scan_workspace_for_status(root: Path) -> WorkspaceScan:
    """Run the same metadata-only, policy-aware scanner used by Room Store Save."""
    root = Path(root)
    runtime_value = os.environ.get("ROBOCORP_HOME")
    runtime = Path(runtime_value) if runtime_value else None
    policy = load_capture_policy(
        root, active_runtime_root=runtime if runtime and runtime.is_dir() else None
    )
    return _scan_workspace(root, policy)


def _deletion_token(
    parent_id: str, deleted: tuple[str, ...], scan: WorkspaceScan
) -> str:
    value = json.dumps(
        [parent_id, scan.signature, deleted], separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    """Promote a staged tree without replacing a destination created in a race."""
    if os.name == "nt":
        os.rename(source, destination)
        return
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise RoomStoreOperationsError(
            "atomic no-replace restore promotion is unavailable"
        )
    at_fdcwd = -100
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(destination),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise RoomStoreOperationsError("restore destination appeared during promotion")
    raise RoomStoreOperationsError("staged restore could not be promoted")


class RoomStoreOperations:
    """Run complete-snapshot save/restore with catalog work injected at the edge.

    ``read_latest`` returns ``(LogicalJat | None, etag)``. ``publish_descriptor``
    receives the validated descriptor, stat signature metadata, and observed
    ETag; it must age-encrypt
    the canonical bytes using the selected Dimension material, publish referenced
    immutable data first, and conditionally replace the catalog last. A callback
    error must expose ``published`` as True/False only when that outcome is known.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        repository: str,
        cache_dir: Path,
        password_dir: Path,
        store_factory: Callable[..., Any] | None = None,
        dimension: object,
        backend: object,
        ensure_keyset: Callable[..., Any] = ensure_room_store_keyset,
        bind_repository: Callable[..., Any] = bind_room_store_repository,
        read_latest: Callable[[], tuple[LogicalJat | None, str | None]],
        read_catalog_signature: Callable[[], tuple[str, str, str] | None] | None = None,
        read_snapshot_entries: Callable[[str], Iterable[SnapshotEntry]] | None = None,
        publish_descriptor: Callable[..., None],
        write_marker: Callable[..., None],
        descriptor_metadata: Mapping[str, Any],
        resolve_components: Callable[[Any, LogicalJat | None], Mapping[str, Any]]
        | None = None,
        active_runtime_root: Path | None = None,
        secure_private_file: Callable[[int, Path, int], None] | None = None,
        validate_private_directory: Callable[[Path], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.repository = repository
        self.cache_dir = Path(cache_dir)
        self.password_dir = Path(password_dir)
        self.store_factory = store_factory or self._restic_store_factory
        self.dimension = dimension
        self.backend = backend
        self.ensure_keyset = ensure_keyset
        self.bind_repository = bind_repository
        self.read_latest = read_latest
        self.read_catalog_signature = read_catalog_signature
        self.read_snapshot_entries = read_snapshot_entries
        self.publish_descriptor = publish_descriptor
        self.write_marker = write_marker
        self.descriptor_metadata = copy.deepcopy(dict(descriptor_metadata))
        self.resolve_components = resolve_components
        self.active_runtime_root = active_runtime_root
        self.secure_private_file = secure_private_file
        self.validate_private_directory = validate_private_directory

    def _restic_store_factory(
        self, *, repository: str, cache_dir: Path, password_file: Path
    ):
        return ResticStore(
            repository=repository, cache_dir=cache_dir, password_file=password_file
        )

    def _policy(self) -> CapturePolicy:
        return load_capture_policy(
            self.workspace, active_runtime_root=self.active_runtime_root
        )

    def _exclude_file(self, policy: CapturePolicy) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix="room-store-excludes-", dir=self.password_dir
        )
        try:
            self._protect_private_file(descriptor, Path(name))
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(policy.restic_excludes()))
                stream.write("\n")
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)

    def _password_file(self, secret: str) -> Path:
        descriptor: int | None = None
        name: str | None = None
        try:
            if not isinstance(secret, str) or len(secret) != 43:
                raise ValueError
            password = base64.urlsafe_b64decode(secret + "=")
            if (
                len(password) != 32
                or base64.urlsafe_b64encode(password).decode().rstrip("=") != secret
            ):
                raise ValueError
            descriptor, name = tempfile.mkstemp(
                prefix="room-store-password-", dir=self.password_dir
            )
            self._protect_private_file(descriptor, Path(name))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secret.encode("ascii") + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            return Path(name)
        except BaseException as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if name is not None:
                Path(name).unlink(missing_ok=True)
            if isinstance(error, RoomStoreOperationsError):
                raise
            raise RoomStoreOperationsError(
                "Room Store password could not be prepared safely"
            ) from error

    def _protect_private_file(self, descriptor: int, path: Path) -> None:
        if self.secure_private_file is not None:
            self.secure_private_file(descriptor, path, 0o600)
            return
        if os.name == "nt":
            raise RoomStoreOperationsError(
                "Windows private-file ACL handoff is unavailable"
            )
        os.fchmod(descriptor, 0o600)

    def _validate_private_roots(self) -> None:
        try:
            root = self.workspace.resolve(strict=True)
            cache = self.cache_dir.resolve()
            private = self.password_dir.resolve()
            cache.relative_to(root)
            raise RoomStoreOperationsError(
                "Room Store cache must be outside the workspace"
            )
        except ValueError:
            pass
        try:
            private.relative_to(root)
            raise RoomStoreOperationsError(
                "Room Store private files must be outside the workspace"
            )
        except ValueError:
            pass
        cache = self.cache_dir.resolve()
        private = self.password_dir.resolve()
        if (
            cache == private
            or cache.is_relative_to(private)
            or private.is_relative_to(cache)
        ):
            raise RoomStoreOperationsError(
                "Room Store cache and credential directory must be separate"
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.password_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "nt":
            if self.validate_private_directory is None:
                raise RoomStoreOperationsError(
                    "Windows private-directory ACL validation is unavailable"
                )
            self.validate_private_directory(self.cache_dir)
            self.validate_private_directory(self.password_dir)
            return
        for directory in (self.cache_dir, self.password_dir):
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_mode & 0o077
            ):
                raise RoomStoreOperationsError(
                    "Room Store private directory permissions are unsafe"
                )

    def _open_store(self):
        self._validate_private_roots()
        keyset = self.ensure_keyset(self.dimension, self.backend)
        metadata = getattr(keyset, "room_store", None)
        secret = getattr(metadata, "secret", None)
        generation = getattr(metadata, "generation", None)
        if metadata is None or type(generation) is not int:
            raise RoomStoreOperationsError("Room Store keyset is unavailable")
        password_file = self._password_file(secret)
        try:
            store = self.store_factory(
                repository=self.repository,
                cache_dir=self.cache_dir,
                password_file=password_file,
            )
        except BaseException:
            password_file.unlink(missing_ok=True)
            raise
        return keyset, password_file, store

    def _bind_repository(self, keyset: object, repository_id: str) -> object:
        metadata = getattr(keyset, "room_store", None)
        if metadata is None:
            raise RoomStoreOperationsError("Room Store keyset is unavailable")
        bound_id = getattr(metadata, "repository_id", None)
        if bound_id is None:
            keyset = self.bind_repository(
                self.dimension,
                self.backend,
                repository_id,
                expected_generation=metadata.generation,
            )
            metadata = getattr(keyset, "room_store", None)
            bound_id = getattr(metadata, "repository_id", None)
        if bound_id != repository_id:
            raise RoomStoreOperationsError(
                "Room Store repository binding does not match"
            )
        return keyset

    def _deletion_preview(
        self,
        parent: LogicalJat | None,
        scan: WorkspaceScan,
        rows: Iterable[SnapshotEntry] | None,
    ) -> SavePreview:
        if parent is None:
            return SavePreview(
                scan.logical_bytes,
                scan.signature,
                scan.signature_algorithm,
                scan.capture_policy_sha256,
                0,
                len(scan.paths),
                (),
                None,
            )
        if rows is None:
            raise RoomStoreOperationsError(
                "read-only snapshot entry reader is required for deletion preview"
            )
        previous_rows = _validate_snapshot_entries(rows)
        prior_paths = frozenset(path for path in previous_rows if path != ".")
        current_paths = scan.paths
        deleted = tuple(sorted(prior_paths - current_paths))
        threshold = max(25, (len(prior_paths) + 3) // 4)
        token = (
            _deletion_token(parent.to_dict()["logical_jat_id"], deleted, scan)
            if deleted and len(deleted) >= threshold
            else None
        )
        return SavePreview(
            scan.logical_bytes,
            scan.signature,
            scan.signature_algorithm,
            scan.capture_policy_sha256,
            len(prior_paths),
            len(current_paths),
            deleted,
            token,
        )

    def preview(self, parent: LogicalJat | None = None) -> SavePreview:
        policy = self._policy()
        scan = _scan_workspace(self.workspace, policy)
        selected_parent = parent
        latest, _etag = self.read_latest()
        if selected_parent is None:
            selected_parent = latest
        if selected_parent is None:
            return self._deletion_preview(None, scan, None)
        parent_body = selected_parent.to_dict()
        if (
            parent_body["dimension_id"] != self.descriptor_metadata["dimension_id"]
            or parent_body["encryption_domain_id"]
            != self.descriptor_metadata["encryption_domain_id"]
            or parent_body["room_id"] != self.descriptor_metadata["room_id"]
        ):
            raise RoomStoreOperationsError(
                "selected parent belongs to another Room Store scope"
            )
        if self.read_snapshot_entries is None:
            raise RoomStoreOperationsError(
                "read-only snapshot entry reader is required for deletion preview"
            )
        entries = self.read_snapshot_entries(parent_body["workspace"]["snapshot_id"])
        return self._deletion_preview(selected_parent, scan, entries)

    def _descriptor(
        self,
        parent: LogicalJat | None,
        repository_id: str,
        snapshot,
        summary,
        policy: CapturePolicy,
        logical_bytes: int,
        components: Mapping[str, Any],
    ) -> LogicalJat:
        parent_body = parent.to_dict() if parent is not None else None
        body = {
            "format_version": 1,
            "payload_kind": "room-store-v1",
            "logical_jat_id": uuid.uuid4().hex,
            "dimension_id": self.descriptor_metadata["dimension_id"],
            "encryption_domain_id": self.descriptor_metadata["encryption_domain_id"],
            "room_id": self.descriptor_metadata["room_id"],
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "capture_policy_sha256": policy.sha256,
            "workspace": {
                "engine": "restic",
                "repository_id": repository_id,
                "repository_format": 2,
                "snapshot_id": snapshot.snapshot_id,
                "tree_id": snapshot.tree_id,
                "source_path": ".",
                "logical_bytes": logical_bytes,
                "data_added": summary.data_added if summary else 0,
                "data_added_packed": summary.data_added_packed if summary else 0,
            },
            "components": components,
            "source": self.descriptor_metadata["source"],
            "producer": self.descriptor_metadata["producer"],
        }
        if parent_body is not None:
            body["parent_logical_jat_id"] = parent_body["logical_jat_id"]
            body["workspace"]["parent_snapshot_id"] = parent_body["workspace"][
                "snapshot_id"
            ]
        for key in ("origin_room_id",):
            if key in self.descriptor_metadata:
                body[key] = self.descriptor_metadata[key]
        return LogicalJat.from_dict(body)

    def _is_unchanged(
        self,
        latest: LogicalJat | None,
        parent: LogicalJat | None,
        policy: CapturePolicy,
        components: Mapping[str, Any],
        workspace_signature: str,
        catalog_signature: tuple[str, str, str] | None,
        force_scan: bool,
    ) -> bool:
        expected_catalog_evidence = (
            workspace_signature,
            "josh-room-stat-v1",
            policy.sha256,
        )
        if (
            latest is None
            or parent is None
            or force_scan
            or catalog_signature != expected_catalog_evidence
        ):
            return False
        latest_body = latest.to_dict()
        parent_body = parent.to_dict()
        return (
            latest_body["logical_jat_id"] == parent_body["logical_jat_id"]
            and latest_body["capture_policy_sha256"] == policy.sha256
            and latest_body["components"] == components
            and latest_body["source"] == self.descriptor_metadata["source"]
            and latest_body["producer"] == self.descriptor_metadata["producer"]
            and latest_body["dimension_id"] == self.descriptor_metadata["dimension_id"]
            and latest_body["encryption_domain_id"]
            == self.descriptor_metadata["encryption_domain_id"]
            and latest_body["room_id"] == self.descriptor_metadata["room_id"]
            and latest_body.get("origin_room_id")
            == self.descriptor_metadata.get("origin_room_id")
        )

    @staticmethod
    def _publication_state(error: BaseException) -> str:
        if (
            isinstance(error, CatalogConflict)
            or getattr(error, "published", None) is False
        ):
            return "rejected"
        if getattr(error, "published", None) is True:
            return "committed-verification-unknown"
        return "uncertain"

    def save(
        self,
        parent: LogicalJat | None = None,
        *,
        deletion_confirmation_token: str | None = None,
        on_progress: Callable[[BackupProgress], None] | None = None,
        cancellation: Any = None,
    ) -> SaveResult:
        latest, etag = self.read_latest()
        selected_parent = parent if parent is not None else latest
        policy = self._policy()
        before = _scan_workspace(self.workspace, policy)
        keyset, password_file, store = self._open_store()
        try:
            with store as opened:
                repository_info = opened.initialize()
                initial_data_added = getattr(opened, "data_added_bytes", None)
                self._bind_repository(keyset, repository_info.repository_id)
                if selected_parent is not None:
                    parent_body = selected_parent.to_dict()
                    if (
                        parent_body["workspace"]["repository_id"]
                        != repository_info.repository_id
                        or parent_body["dimension_id"]
                        != self.descriptor_metadata["dimension_id"]
                        or parent_body["encryption_domain_id"]
                        != self.descriptor_metadata["encryption_domain_id"]
                        or parent_body["room_id"] != self.descriptor_metadata["room_id"]
                    ):
                        raise RoomStoreOperationsError(
                            "selected parent belongs to another Room Store scope"
                        )
                if (
                    latest is not None
                    and repository_info.repository_id
                    != latest.to_dict()["workspace"]["repository_id"]
                ):
                    raise RoomStoreOperationsError(
                        "Room Store catalog points to another repository"
                    )
                preview = self._deletion_preview(
                    selected_parent,
                    before,
                    opened.entries(
                        selected_parent.to_dict()["workspace"]["snapshot_id"]
                    )
                    if selected_parent is not None
                    else None,
                )
                if (
                    preview.deletion_confirmation_token
                    and deletion_confirmation_token
                    != preview.deletion_confirmation_token
                ):
                    raise RoomStoreOperationsError(
                        "suspicious deletions require the current preview confirmation token",
                        deletion_confirmation_token=preview.deletion_confirmation_token,
                    )
                components = copy.deepcopy(self.descriptor_metadata["components"])
                if self.resolve_components is not None:
                    resolved_components = self.resolve_components(opened, latest)
                    if not isinstance(resolved_components, Mapping) or set(
                        resolved_components
                    ) != set(components):
                        raise RoomStoreOperationsError(
                            "native component resolver returned an invalid component set"
                        )
                    components = copy.deepcopy(dict(resolved_components))
                catalog_signature = (
                    self.read_catalog_signature()
                    if self.read_catalog_signature is not None
                    else None
                )
                if self._is_unchanged(
                    latest,
                    selected_parent,
                    policy,
                    components,
                    before.signature,
                    catalog_signature,
                    False,
                ):
                    if cancellation is not None and cancellation.cancelled:
                        raise ResticStoreError(ResticStoreErrorCode.CANCELLED)
                    return SaveResult(
                        "already-saved",
                        latest,
                        before.logical_bytes,
                        0,
                        latest.to_dict()["workspace"]["snapshot_id"],
                        "not-published",
                        before.signature,
                        before.signature_algorithm,
                        policy.sha256,
                    )
                exclude_file = self._exclude_file(policy)
                try:
                    summary = opened.backup(
                        self.workspace,
                        parent=selected_parent.to_dict()["workspace"]["snapshot_id"]
                        if selected_parent
                        else None,
                        excludes=exclude_file,
                        on_progress=on_progress,
                        cancellation=cancellation,
                    )
                finally:
                    exclude_file.unlink(missing_ok=True)
                if summary.snapshot_id is None and summary.force_scan:
                    raise RoomStoreOperationsError(
                        "Restic reported a no-op after a forced content scan"
                    )
                if summary.snapshot_id is None and self._is_unchanged(
                    latest,
                    selected_parent,
                    policy,
                    components,
                    before.signature,
                    catalog_signature,
                    summary.force_scan,
                ):
                    return SaveResult(
                        "already-saved",
                        latest,
                        before.logical_bytes,
                        0,
                        latest.to_dict()["workspace"]["snapshot_id"],
                        "not-published",
                        before.signature,
                        before.signature_algorithm,
                        policy.sha256,
                    )
                if summary.snapshot_id is None:
                    if selected_parent is None:
                        raise RoomStoreOperationsError(
                            "restic reported a no-op without a parent snapshot"
                        )
                    if (
                        summary.effective_parent_id
                        != selected_parent.to_dict()["workspace"]["snapshot_id"]
                    ):
                        raise RoomStoreOperationsError(
                            "Restic no-op parent does not match the selected logical JAT"
                        )
                    snapshot = opened.snapshot(
                        selected_parent.to_dict()["workspace"]["snapshot_id"]
                    )
                    if (
                        snapshot.tree_id
                        != selected_parent.to_dict()["workspace"]["tree_id"]
                    ):
                        raise RoomStoreOperationsError(
                            "selected parent tree identity does not match the repository"
                        )
                else:
                    snapshot = opened.snapshot(summary.snapshot_id)
                    expected_parent_snapshot = (
                        None
                        if summary.force_scan
                        else (
                            selected_parent.to_dict()["workspace"]["snapshot_id"]
                            if selected_parent is not None
                            else None
                        )
                    )
                    if summary.effective_parent_id != expected_parent_snapshot:
                        raise RoomStoreOperationsError(
                            "Restic backup parent does not match its effective parent"
                        )
                    if snapshot.parent_snapshot_id != expected_parent_snapshot:
                        raise RoomStoreOperationsError(
                            "Restic snapshot parent does not match its effective parent"
                        )
                entries = _validate_snapshot_entries(
                    opened.entries(snapshot.snapshot_id)
                )
                logical_bytes = sum(
                    row.size or 0
                    for row in entries.values()
                    if row.entry_type == "file"
                )
                if (
                    set(entries) - {"."}
                ) != before.paths or logical_bytes != before.logical_bytes:
                    raise RoomStoreOperationsError(
                        "Restic snapshot inventory does not match the validated workspace"
                    )
                after_policy = self._policy()
                after = _scan_workspace(self.workspace, after_policy)
                if (
                    before.signature != after.signature
                    or policy.sha256 != after_policy.sha256
                ):
                    raise RoomStoreOperationsError(
                        "workspace or capture policy changed during save"
                    )
                descriptor = self._descriptor(
                    selected_parent
                    if snapshot.parent_snapshot_id is not None
                    else None,
                    repository_info.repository_id,
                    snapshot,
                    summary,
                    policy,
                    logical_bytes,
                    components,
                )
                commit_state = "committed"
                commit_error: BaseException | None = None
                deferred_cancel: CLICancelled | None = None
                try:
                    with defer_sigterm_cancellation():
                        try:
                            self.publish_descriptor(
                                descriptor,
                                expected_etag=etag,
                                workspace_signature=before.signature,
                                signature_algorithm=before.signature_algorithm,
                            )
                        except BaseException as error:  # noqa: BLE001 - assign catalog outcome before deferred SIGTERM.
                            commit_error = error
                            commit_state = self._publication_state(error)
                except CLICancelled as error:
                    deferred_cancel = error
                if commit_state in {"rejected", "uncertain"}:
                    if deferred_cancel is not None:
                        deferred_cancel.result.update(
                            {
                                "publication_state": commit_state,
                                "marker_state": "unchanged",
                                "workspace_signature": before.signature,
                                "signature_algorithm": before.signature_algorithm,
                                "capture_policy_sha256": policy.sha256,
                            }
                        )
                        raise deferred_cancel
                    assert commit_error is not None
                    raise RoomStorePublicationError(
                        commit_error, commit_state
                    ) from commit_error
                dirty = False
                try:
                    current = _scan_workspace(self.workspace, self._policy())
                    dirty = current.signature != before.signature
                except CLICancelled as error:
                    deferred_cancel = error
                    dirty = True
                except (OSError, RuntimeError, ValueError):
                    dirty = True
                try:
                    self.write_marker(
                        descriptor,
                        clean=not dirty,
                        workspace_signature=before.signature,
                        signature_algorithm=before.signature_algorithm,
                        capture_policy_sha256=policy.sha256,
                    )
                except BaseException as error:
                    if deferred_cancel is not None:
                        deferred_cancel.result.update(
                            {
                                "publication_state": "committed",
                                "marker_state": "stale",
                                "marker_error_type": type(error).__name__,
                                "logical_jat_id": descriptor.to_dict()[
                                    "logical_jat_id"
                                ],
                                "snapshot_id": snapshot.snapshot_id,
                                "workspace_signature": before.signature,
                                "signature_algorithm": before.signature_algorithm,
                                "capture_policy_sha256": policy.sha256,
                            }
                        )
                        raise deferred_cancel
                    raise RoomStorePublicationError(
                        error, "committed-marker-stale"
                    ) from error
                if commit_state == "committed-verification-unknown":
                    if deferred_cancel is not None:
                        deferred_cancel.result.update(
                            {
                                "publication_state": commit_state,
                                "marker_state": "updated",
                                "saved_but_dirty": dirty,
                                "logical_jat_id": descriptor.to_dict()[
                                    "logical_jat_id"
                                ],
                                "snapshot_id": snapshot.snapshot_id,
                                "workspace_signature": before.signature,
                                "signature_algorithm": before.signature_algorithm,
                                "capture_policy_sha256": policy.sha256,
                            }
                        )
                        raise deferred_cancel
                    assert commit_error is not None
                    raise RoomStorePublicationError(
                        commit_error, commit_state
                    ) from commit_error
                if deferred_cancel is not None:
                    deferred_cancel.result.update(
                        {
                            "publication_state": "committed",
                            "marker_state": "updated",
                            "saved_but_dirty": dirty,
                            "logical_jat_id": descriptor.to_dict()["logical_jat_id"],
                            "snapshot_id": snapshot.snapshot_id,
                            "workspace_signature": before.signature,
                            "signature_algorithm": before.signature_algorithm,
                            "capture_policy_sha256": policy.sha256,
                        }
                    )
                    raise deferred_cancel
                return SaveResult(
                    "saved-but-dirty" if dirty else "saved",
                    descriptor,
                    before.logical_bytes,
                    (opened.data_added_bytes - initial_data_added
                     if initial_data_added is not None else summary.data_added),
                    snapshot.snapshot_id,
                    "committed",
                    before.signature,
                    before.signature_algorithm,
                    policy.sha256,
                )
        finally:
            password_file.unlink(missing_ok=True)

    def restore(
        self,
        descriptor: LogicalJat,
        destination: Path,
        *,
        prepare_restored_workspace: Callable[[Path, LogicalJat, Any], None]
        | None = None,
        write_restore_marker: Callable[[Path, Path, LogicalJat], None] | None = None,
        restic_store: Any | None = None,
        repository_info: Any | None = None,
    ) -> RestoreResult:
        body = descriptor.to_dict()
        if write_restore_marker is None:
            raise RoomStoreOperationsError("staged restore requires a marker writer")
        has_components = any(
            component is not None for component in body["components"].values()
        )
        if has_components and prepare_restored_workspace is None:
            raise RoomStoreOperationsError(
                "logical JAT components require a restore preparation callback"
            )
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise RoomStoreOperationsError("restore destination must be absent")
        parent_dir = target.parent.resolve(strict=True)
        if not parent_dir.is_dir():
            raise RoomStoreOperationsError(
                "restore destination parent must be a directory"
            )
        keyset = None
        password_file = None
        if restic_store is None:
            keyset, password_file, store = self._open_store()
        else:
            store = restic_store
        try:
            if keyset is not None:
                keyset_metadata = getattr(keyset, "room_store", None)
                bound_repository_id = getattr(keyset_metadata, "repository_id", None)
                if bound_repository_id is None:
                    raise RoomStoreOperationsError(
                        "Room Store repository binding does not match"
                    )
            store_context = store if restic_store is None else nullcontext(store)
            with store_context as opened:
                if repository_info is None:
                    repository_info = opened.open_existing()
                if (
                    keyset is not None
                    and bound_repository_id != repository_info.repository_id
                ):
                    raise RoomStoreOperationsError(
                        "Room Store repository binding does not match"
                    )
                workspace = body["workspace"]
                if (
                    workspace["repository_id"] != repository_info.repository_id
                    or workspace["repository_format"]
                    != repository_info.repository_format
                    or body["dimension_id"] != self.descriptor_metadata["dimension_id"]
                    or body["encryption_domain_id"]
                    != self.descriptor_metadata["encryption_domain_id"]
                    or body["room_id"] != self.descriptor_metadata["room_id"]
                ):
                    raise RoomStoreOperationsError(
                        "logical JAT belongs to another Room Store repository"
                    )
                snapshot = opened.snapshot(workspace["snapshot_id"])
                if snapshot.tree_id != workspace["tree_id"]:
                    raise RoomStoreOperationsError(
                        "logical JAT tree identity does not match the repository"
                    )
                _validate_snapshot_entries(opened.entries(snapshot.snapshot_id))
                with tempfile.TemporaryDirectory(
                    prefix=f".{target.name}.josh-room-",
                    dir=parent_dir,
                ) as staging_name:
                    staging_root = Path(staging_name)
                    if self.validate_private_directory is not None:
                        self.validate_private_directory(staging_root)
                    else:
                        staging_stat = staging_root.lstat()
                        if (
                            os.name == "nt"
                            or not stat.S_ISDIR(staging_stat.st_mode)
                            or staging_stat.st_mode & 0o077
                        ):
                            raise RoomStoreOperationsError(
                                "private restore staging permissions are unsafe"
                            )
                    stage = staging_root / "workspace"
                    opened.restore(snapshot.snapshot_id, stage)
                    _scan_workspace(stage, None)
                    if prepare_restored_workspace is not None:
                        prepare_restored_workspace(stage, descriptor, opened)
                    _scan_workspace(stage, None)
                    write_restore_marker(stage, target, descriptor)
                    _rename_directory_noreplace(stage, target)
                    return RestoreResult(descriptor, target)
        finally:
            if password_file is not None:
                password_file.unlink(missing_ok=True)
