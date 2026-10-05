"""Bounded subprocess adapter for a pinned restic Room Store repository."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from .adapter_contract import CancellationToken
from .cancellation import terminate_owned_process
from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    verify_private_path,
)

RESTIC_VERSION = "0.19.1"
SUPPORTED_REPOSITORY_FORMAT = 2
MAX_JSON_EVENT_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1_000_000
MAX_SNAPSHOT_INVENTORY = 10_000
MAX_FORGET_SNAPSHOTS = 256
MAX_CA_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_PASSWORD_FILE_BYTES = 64 * 1024
MAX_INTEGER = (1 << 63) - 1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_REPOSITORY_ID = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_TYPE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SUBSET_FRACTION = re.compile(r"^([1-9][0-9]{0,8})/([1-9][0-9]{0,8})$")
_BACKUP_MESSAGE_TYPES = frozenset({"status", "verbose_status", "error", "summary"})
_BASE_ENVIRONMENT = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    }
)
_PROVIDER_ENVIRONMENT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_REGION",
    }
)


class ResticStoreErrorCode(StrEnum):
    NOT_OPEN = "not-open"
    INVALID_CONFIGURATION = "invalid-configuration"
    INVALID_OUTPUT = "invalid-output"
    VERSION_MISMATCH = "version-mismatch"
    REPOSITORY_FORMAT = "repository-format-mismatch"
    REPOSITORY_ID = "repository-id-invalid"
    REPOSITORY_MISSING = "repository-missing"
    INITIALIZE_FAILED = "initialize-failed"
    COMMAND_FAILED = "command-failed"
    UNKNOWN_EXIT = "unknown-exit"
    INCOMPLETE_BACKUP = "incomplete-backup"
    BACKUP_ERRORS = "backup-errors"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"
    PROGRESS_CALLBACK_FAILED = "progress-callback-failed"
    CONFIRMATION_REQUIRED = "confirmation-required"


_ERROR_MESSAGES = {
    ResticStoreErrorCode.NOT_OPEN: "restic store context is not open",
    ResticStoreErrorCode.INVALID_CONFIGURATION: "restic store configuration is invalid",
    ResticStoreErrorCode.INVALID_OUTPUT: "restic returned invalid or unsupported output",
    ResticStoreErrorCode.VERSION_MISMATCH: "restic version does not match the pinned version",
    ResticStoreErrorCode.REPOSITORY_FORMAT: "restic repository format is unsupported",
    ResticStoreErrorCode.REPOSITORY_ID: "restic repository identity is invalid",
    ResticStoreErrorCode.REPOSITORY_MISSING: "restic repository does not exist",
    ResticStoreErrorCode.INITIALIZE_FAILED: "restic repository initialization failed",
    ResticStoreErrorCode.COMMAND_FAILED: "restic command failed",
    ResticStoreErrorCode.UNKNOWN_EXIT: "restic returned an unknown exit status",
    ResticStoreErrorCode.INCOMPLETE_BACKUP: "restic backup is incomplete and cannot be published",
    ResticStoreErrorCode.BACKUP_ERRORS: "restic backup reported source errors",
    ResticStoreErrorCode.CANCELLED: "restic operation was cancelled",
    ResticStoreErrorCode.TIMED_OUT: "restic operation timed out",
    ResticStoreErrorCode.PROGRESS_CALLBACK_FAILED: "restic progress handler failed",
    ResticStoreErrorCode.CONFIRMATION_REQUIRED: "explicit maintenance confirmation is required",
}


class ResticStoreError(RuntimeError):
    """Stable, path-free adapter error with bounded recovery metadata."""

    def __init__(
        self,
        code: ResticStoreErrorCode,
        *,
        exit_code: int | None = None,
        orphan_snapshot_id: str | None = None,
    ) -> None:
        self.code = code
        self.exit_code = exit_code
        self.orphan_snapshot_id = (
            orphan_snapshot_id if _valid_snapshot_id(orphan_snapshot_id) else None
        )
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    repository_id: str
    repository_format: int


@dataclass(frozen=True, slots=True)
class BackupProgress:
    files_done: int | None
    total_files: int | None
    bytes_done: int | None
    total_bytes: int | None


@dataclass(frozen=True, slots=True)
class BackupSummary:
    snapshot_id: str | None
    files_new: int
    files_changed: int
    files_unmodified: int
    data_added: int
    data_added_packed: int
    total_bytes_processed: int
    errors: int
    force_scan: bool = False
    effective_parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    snapshot_id: str
    tree_id: str
    parent_snapshot_id: str | None
    time: str
    paths: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SnapshotInventoryItem:
    snapshot_id: str
    tree_id: str
    time: str
    parent_snapshot_id: str | None


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    path: str
    entry_type: str
    size: int | None
    mode: int | None
    link_target: str | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    read_data: bool
    read_data_subset: str | None = None


@dataclass(frozen=True, slots=True)
class ForgetPlan:
    repository_id: str
    snapshot_ids: tuple[str, ...]
    tree_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    operation: str
    dry_run: bool
    snapshot_ids: tuple[str, ...] = ()


def _valid_snapshot_id(value: object) -> bool:
    return isinstance(value, str) and _SNAPSHOT_ID.fullmatch(value) is not None


def _nonnegative_integer(value: object, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or value < 0 or value > MAX_INTEGER:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return value


def _load_json(raw: bytes | str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None


def _nonblocking_descriptor(stream: Any) -> int | None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    try:
        os.set_blocking(descriptor, False)
    except (OSError, ValueError):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
    return descriptor


def parse_repository_config(raw: bytes | str) -> RepositoryInfo:
    value = _load_json(raw)
    if not isinstance(value, dict):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    repository_id = value.get("id")
    if not isinstance(repository_id, str) or _REPOSITORY_ID.fullmatch(repository_id) is None:
        raise ResticStoreError(ResticStoreErrorCode.REPOSITORY_ID)
    repository_format = value.get("version")
    if type(repository_format) is not int or repository_format <= 0:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return RepositoryInfo(repository_id, repository_format)


def _event(raw: bytes | str, *, max_event_bytes: int) -> dict[str, Any]:
    value = _json_object(raw, max_event_bytes=max_event_bytes)
    if not isinstance(value.get("message_type"), str):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return value


def _json_object(raw: bytes | str, *, max_event_bytes: int) -> dict[str, Any]:
    try:
        encoded = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(encoded) > max_event_bytes:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        value = json.loads(encoded)
    except ResticStoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
    if not isinstance(value, dict):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return value


def _snapshot_identifier(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not _valid_snapshot_id(value):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return str(value)


def _progress(value: dict[str, Any]) -> BackupProgress:
    return BackupProgress(
        files_done=_nonnegative_integer(value.get("files_done"), optional=True),
        total_files=_nonnegative_integer(value.get("total_files"), optional=True),
        bytes_done=_nonnegative_integer(value.get("bytes_done"), optional=True),
        total_bytes=_nonnegative_integer(value.get("total_bytes"), optional=True),
    )


def parse_backup_events(
    events: Iterable[bytes | str],
    *,
    on_progress: Callable[[BackupProgress], None] | None = None,
    max_event_bytes: int = MAX_JSON_EVENT_BYTES,
) -> BackupSummary:
    summary: BackupSummary | None = None
    error_events = 0
    for raw in events:
        event = _event(raw, max_event_bytes=max_event_bytes)
        message_type = event["message_type"]
        if message_type not in _BACKUP_MESSAGE_TYPES:
            raise ResticStoreError(
                ResticStoreErrorCode.INVALID_OUTPUT,
                orphan_snapshot_id=summary.snapshot_id if summary else None,
            )
        if summary is not None:
            raise ResticStoreError(
                ResticStoreErrorCode.INVALID_OUTPUT,
                orphan_snapshot_id=summary.snapshot_id,
            )
        if message_type in {"status", "verbose_status"}:
            if on_progress is not None:
                try:
                    on_progress(_progress(event))
                except Exception:  # noqa: BLE001 - callback diagnostics may contain paths/secrets.
                    raise ResticStoreError(ResticStoreErrorCode.PROGRESS_CALLBACK_FAILED) from None
        elif message_type == "error":
            error_events += 1
        elif message_type == "summary":
            snapshot_id = _snapshot_identifier(event.get("snapshot_id"), required=False)
            required_counts = {
                name: _nonnegative_integer(event.get(name))
                for name in (
                    "files_new",
                    "files_changed",
                    "files_unmodified",
                    "data_added",
                    "data_added_packed",
                    "total_bytes_processed",
                )
            }
            summary_errors = _nonnegative_integer(event.get("errors", error_events))
            if summary_errors != error_events:
                raise ResticStoreError(
                    ResticStoreErrorCode.INVALID_OUTPUT,
                    orphan_snapshot_id=snapshot_id,
                )
            summary = BackupSummary(
                snapshot_id=snapshot_id,
                files_new=required_counts["files_new"],
                files_changed=required_counts["files_changed"],
                files_unmodified=required_counts["files_unmodified"],
                data_added=required_counts["data_added"],
                data_added_packed=required_counts["data_added_packed"],
                total_bytes_processed=required_counts["total_bytes_processed"],
                errors=summary_errors,
            )
    if summary is None:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    if summary.errors:
        raise ResticStoreError(
            ResticStoreErrorCode.BACKUP_ERRORS,
            orphan_snapshot_id=summary.snapshot_id,
        )
    return summary


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not _valid_snapshot_id(snapshot_id):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    return snapshot_id


def _explicit_snapshot_ids(snapshot_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(snapshot_ids, (str, bytes)) or not isinstance(snapshot_ids, Iterable):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    values: list[str] = []
    for snapshot_id in snapshot_ids:
        if len(values) >= MAX_FORGET_SNAPSHOTS:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        values.append(_validate_snapshot_id(snapshot_id))
    if not values or len(set(values)) != len(values):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    return tuple(sorted(values))


def _parse_snapshot_record(
    snapshot: dict[str, Any], expected_snapshot_id: str | None = None
) -> SnapshotInfo:
    snapshot_id = _snapshot_identifier(snapshot.get("id"), required=True)
    tree_id = snapshot.get("tree")
    parent = snapshot.get("parent")
    time_value = snapshot.get("time")
    paths_value = snapshot.get("paths")
    try:
        timestamp = datetime.fromisoformat(time_value)
    except (AttributeError, TypeError, ValueError):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
    if (
        (expected_snapshot_id is not None and snapshot_id != expected_snapshot_id)
        or not isinstance(tree_id, str)
        or _REPOSITORY_ID.fullmatch(tree_id) is None
        or (parent is not None and not _valid_snapshot_id(parent))
        or not isinstance(time_value, str)
        or not time_value
        or len(time_value) > 128
        or timestamp.tzinfo is None
        or not isinstance(paths_value, list)
        or not paths_value
        or len(paths_value) > 256
    ):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    source_paths: list[str] = []
    for path in paths_value:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        try:
            if len(path.encode("utf-8")) > 4096:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        except UnicodeEncodeError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
        source_paths.append(path)
    return SnapshotInfo(snapshot_id, tree_id, parent, time_value, tuple(source_paths))


def parse_snapshot(raw: bytes | str, expected_snapshot_id: str) -> SnapshotInfo:
    expected = _validate_snapshot_id(expected_snapshot_id)
    value = _load_json(raw)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return _parse_snapshot_record(value[0], expected)


def parse_snapshot_inventory(
    raw: bytes | str,
    *,
    max_snapshots: int = MAX_SNAPSHOT_INVENTORY,
) -> tuple[SnapshotInventoryItem, ...]:
    if type(max_snapshots) is not int or not 1 <= max_snapshots <= MAX_SNAPSHOT_INVENTORY:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    if not isinstance(raw, (bytes, str)):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    try:
        encoded_length = len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
    if encoded_length > MAX_CAPTURE_BYTES:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    value = _load_json(raw)
    if not isinstance(value, list) or len(value) > max_snapshots:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    snapshots: list[SnapshotInventoryItem] = []
    seen_ids: set[str] = set()
    for record in value:
        if not isinstance(record, dict):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        private_snapshot = _parse_snapshot_record(record)
        if private_snapshot.snapshot_id in seen_ids:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        seen_ids.add(private_snapshot.snapshot_id)
        snapshots.append(
            SnapshotInventoryItem(
                private_snapshot.snapshot_id,
                private_snapshot.tree_id,
                private_snapshot.time,
                private_snapshot.parent_snapshot_id,
            )
        )
    return tuple(snapshots)


def _normalize_virtual_path(path: object, max_event_bytes: int) -> str:
    if not isinstance(path, str) or not path:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    try:
        if len(path.encode("utf-8")) > max_event_bytes:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    except UnicodeEncodeError:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
    if path == "/":
        return "."
    if not path.startswith("/") or path.startswith("//") or "\\" in path or "\x00" in path:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    components = path[1:].split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    return "/".join(components)


def parse_snapshot_entries(
    events: Iterable[bytes | str],
    expected_snapshot: SnapshotInfo,
    *,
    max_event_bytes: int = MAX_JSON_EVENT_BYTES,
    max_entries: int = MAX_ENTRIES,
) -> Iterator[SnapshotEntry]:
    if not isinstance(expected_snapshot, SnapshotInfo):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    expected = _validate_snapshot_id(expected_snapshot.snapshot_id)
    if (
        not isinstance(expected_snapshot.tree_id, str)
        or _REPOSITORY_ID.fullmatch(expected_snapshot.tree_id) is None
        or not expected_snapshot.paths
    ):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
    header_seen = False
    entry_count = 0
    seen_paths: set[str] = set()
    for raw in events:
        event = _json_object(raw, max_event_bytes=max_event_bytes)
        struct_type = event.get("struct_type")
        if struct_type == "snapshot":
            paths = event.get("paths")
            if (
                header_seen
                or event.get("message_type") != "snapshot"
                or event.get("id") != expected
                or event.get("tree") != expected_snapshot.tree_id
                or not isinstance(paths, list)
                or tuple(paths) != expected_snapshot.paths
            ):
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
            header_seen = True
            continue
        if (
            struct_type != "node"
            or event.get("message_type") != "node"
            or not header_seen
        ):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        path = _normalize_virtual_path(event.get("path"), max_event_bytes)
        entry_type = event.get("type")
        if (
            not isinstance(entry_type, str)
            or _ENTRY_TYPE.fullmatch(entry_type) is None
        ):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        if path in seen_paths:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        seen_paths.add(path)
        size = _nonnegative_integer(event.get("size"), optional=True)
        mode = _nonnegative_integer(event.get("mode"), optional=True)
        link_target = event.get("linktarget")
        if link_target is not None:
            if not isinstance(link_target, str):
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
            try:
                if len(link_target.encode("utf-8")) > max_event_bytes:
                    raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
            except UnicodeEncodeError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
        entry_count += 1
        if entry_count > max_entries:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        yield SnapshotEntry(path, entry_type, size, mode, link_target)
    if not header_seen:
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)


class ResticStore:
    """Context-scoped access to one explicitly versioned restic repository."""

    def __init__(
        self,
        *,
        repository: str | Path,
        cache_dir: Path,
        password_file: Path,
        ca_bundle: Path | None = None,
        provider_env: Mapping[str, str] | None = None,
        executable: str = "restic",
        process_factory: Callable[..., Any] = subprocess.Popen,
        terminate_process: Callable[[Any], None] = terminate_owned_process,
        command_timeout: float = 300.0,
        max_json_event_bytes: int = MAX_JSON_EVENT_BYTES,
        max_capture_bytes: int = MAX_CAPTURE_BYTES,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        repository_value = str(repository)
        if repository_value.startswith("s3:"):
            try:
                remote = urlsplit(repository_value[3:])
                host = remote.hostname
                port = remote.port
            except ValueError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
            if (
                remote.scheme != "https"
                or not host
                or (port is not None and not 1 <= port <= 65535)
                or remote.username
                or remote.password
                or remote.query
                or remote.fragment
                or not any(segment for segment in remote.path.split("/") if segment)
            ):
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        else:
            try:
                repository_value = str(Path(repository_value).expanduser().resolve())
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        self._repository = repository_value
        self._cache_dir = Path(cache_dir)
        self._password_file = Path(password_file)
        self._ca_bundle = Path(ca_bundle) if ca_bundle is not None else None
        self._provider_env = self._validate_provider_env(provider_env or {})
        self._executable = executable
        self._process_factory = process_factory
        self._terminate_process = terminate_process
        self._command_timeout = command_timeout
        self._max_json_event_bytes = max_json_event_bytes
        self._max_capture_bytes = max_capture_bytes
        self._max_entries = max_entries
        self._entered = False
        self._active_process: Any | None = None
        self._repository_info: RepositoryInfo | None = None
        self.data_added_bytes = 0
        self._snapshot_info: dict[str, SnapshotInfo] = {}
        if command_timeout <= 0 or max_json_event_bytes <= 0 or max_capture_bytes <= 0 or max_entries <= 0:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)

    @staticmethod
    def _validate_provider_env(values: Mapping[str, str]) -> dict[str, str]:
        if any(key not in _PROVIDER_ENVIRONMENT for key in values):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if ("AWS_ACCESS_KEY_ID" in values) != ("AWS_SECRET_ACCESS_KEY" in values):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        return dict(values)

    def __enter__(self) -> Self:
        if self._entered:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        try:
            password_stat = self._password_file.lstat()
        except OSError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        if (
            not stat.S_ISREG(password_stat.st_mode)
            or password_stat.st_size <= 0
            or password_stat.st_size > MAX_PASSWORD_FILE_BYTES
        ):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        try:
            verify_private_path(self._password_file, directory=False)
        except PrivatePathError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        if self._ca_bundle is not None:
            try:
                ca_stat = self._ca_bundle.lstat()
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
            if (
                not stat.S_ISREG(ca_stat.st_mode)
                or stat.S_ISLNK(ca_stat.st_mode)
                or getattr(ca_stat, "st_file_attributes", 0)
                & _FILE_ATTRIBUTE_REPARSE_POINT
                or ca_stat.st_size <= 0
                or ca_stat.st_size > MAX_CA_BUNDLE_BYTES
            ):
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
            try:
                self._ca_bundle = self._ca_bundle.resolve(strict=True)
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        try:
            try:
                cache_stat = self._cache_dir.lstat()
            except FileNotFoundError:
                self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                protect_private_directory(self._cache_dir)
            else:
                if not stat.S_ISDIR(cache_stat.st_mode):
                    raise PrivatePathError("private cache directory is invalid")
                verify_private_path(self._cache_dir, directory=True)
        except (OSError, PrivatePathError):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        self._entered = True
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        process = self._active_process
        if process is not None and process.poll() is None:
            self._terminate_process(process)
        self._active_process = None
        self._entered = False
        self._snapshot_info.clear()

    def _ensure_open(self) -> None:
        if not self._entered:
            raise ResticStoreError(ResticStoreErrorCode.NOT_OPEN)

    def _environment(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _BASE_ENVIRONMENT if key in os.environ}
        env.update(
            {
                "LC_ALL": "C",
                "RESTIC_REPOSITORY": self._repository,
                "RESTIC_PASSWORD_FILE": str(self._password_file),
                "RESTIC_CACHE_DIR": str(self._cache_dir),
                "RESTIC_PROGRESS_FPS": "2",
            }
        )
        if self._ca_bundle is not None:
            env["RESTIC_CACERT"] = str(self._ca_bundle)
        env.update(self._provider_env)
        return env

    def _spawn(self, args: list[str], *, cwd: Path | None = None) -> Any:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": self._environment(),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = self._process_factory([self._executable, *args], **kwargs)
        except OSError:
            raise ResticStoreError(ResticStoreErrorCode.COMMAND_FAILED) from None
        self._active_process = process
        return process

    def _terminate(self, process: Any) -> None:
        if process.poll() is None:
            self._terminate_process(process)

    @contextmanager
    def _process(self, args: list[str], *, cwd: Path | None = None) -> Iterator[Any]:
        process = self._spawn(args, cwd=cwd)
        try:
            yield process
        except BaseException:
            self._terminate(process)
            raise
        finally:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                stdout.close()
            if self._active_process is process:
                self._active_process = None

    def _wait(self, process: Any) -> int:
        try:
            return process.wait(timeout=self._command_timeout)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            raise ResticStoreError(ResticStoreErrorCode.TIMED_OUT) from None

    def _read_lines(
        self,
        process: Any,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[bytes]:
        stdout = process.stdout
        if stdout is None:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        deadline = time.monotonic() + self._command_timeout
        descriptor = _nonblocking_descriptor(stdout)
        if descriptor is None:
            while True:
                if cancellation is not None and cancellation.cancelled:
                    raise ResticStoreError(ResticStoreErrorCode.CANCELLED)
                if time.monotonic() >= deadline:
                    raise ResticStoreError(ResticStoreErrorCode.TIMED_OUT)
                line = stdout.readline(self._max_json_event_bytes + 1)
                if not line:
                    return
                if len(line) > self._max_json_event_bytes:
                    raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
                yield line
        buffered = bytearray()
        while True:
            if cancellation is not None and cancellation.cancelled:
                raise ResticStoreError(ResticStoreErrorCode.CANCELLED)
            if time.monotonic() >= deadline:
                raise ResticStoreError(ResticStoreErrorCode.TIMED_OUT)
            try:
                chunk = os.read(descriptor, 64 * 1024)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
            if not chunk:
                if buffered:
                    yield bytes(buffered)
                return
            buffered.extend(chunk)
            while True:
                newline = buffered.find(b"\n")
                if newline < 0:
                    break
                line = bytes(buffered[: newline + 1])
                del buffered[: newline + 1]
                if len(line) > self._max_json_event_bytes:
                    raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
                yield line
            if len(buffered) > self._max_json_event_bytes:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)

    def _read_capture(self, process: Any) -> bytes:
        stdout = process.stdout
        if stdout is None:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        deadline = time.monotonic() + self._command_timeout
        descriptor = _nonblocking_descriptor(stdout)
        if descriptor is None:
            output = stdout.read(self._max_capture_bytes + 1)
            if len(output) > self._max_capture_bytes:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
            return output
        output = bytearray()
        while True:
            if time.monotonic() >= deadline:
                self._terminate(process)
                raise ResticStoreError(ResticStoreErrorCode.TIMED_OUT)
            try:
                chunk = os.read(descriptor, min(64 * 1024, self._max_capture_bytes + 1 - len(output)))
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT) from None
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > self._max_capture_bytes:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)

    def _capture(self, args: list[str], *, allowed_exit_codes: frozenset[int] = frozenset({0})) -> tuple[int, bytes]:
        with self._process(args) as process:
            output = self._read_capture(process)
            code = self._wait(process)
            if code not in allowed_exit_codes:
                self._raise_exit(code)
            return code, output

    def _ensure_version(self) -> None:
        _code, output = self._capture(["version"])
        fields = output.decode("utf-8", errors="replace").split()
        if len(fields) < 2 or fields[0] != "restic" or fields[1] != RESTIC_VERSION:
            raise ResticStoreError(ResticStoreErrorCode.VERSION_MISMATCH)

    def _read_repository_info(self, *, allow_missing: bool = False) -> RepositoryInfo | None:
        allowed = frozenset({0, 10}) if allow_missing else frozenset({0})
        code, output = self._capture(["cat", "config"], allowed_exit_codes=allowed)
        if code == 10:
            return None
        info = parse_repository_config(output)
        if info.repository_format != SUPPORTED_REPOSITORY_FORMAT:
            raise ResticStoreError(ResticStoreErrorCode.REPOSITORY_FORMAT)
        return info

    def initialize(self) -> RepositoryInfo:
        self._ensure_open()
        self._snapshot_info.clear()
        self._ensure_version()
        info = self._read_repository_info(allow_missing=True)
        if info is None:
            code, _ = self._capture(
                ["init", "--repository-version", str(SUPPORTED_REPOSITORY_FORMAT)],
                allowed_exit_codes=frozenset({0, 1}),
            )
            if code != 0:
                # A competing creator may win initialization. Reopen only an
                # already-existing, explicitly compatible repository.
                info = self._read_repository_info(allow_missing=True)
                if info is None:
                    raise ResticStoreError(
                        ResticStoreErrorCode.INITIALIZE_FAILED, exit_code=code
                    )
            else:
                info = self._read_repository_info()
        if info is None:
            raise ResticStoreError(ResticStoreErrorCode.INITIALIZE_FAILED)
        self._repository_info = info
        return info

    def open_existing(self) -> RepositoryInfo:
        """Validate an existing repository without creating or migrating it."""
        self._ensure_open()
        self._snapshot_info.clear()
        self._ensure_version()
        info = self._read_repository_info(allow_missing=True)
        if info is None:
            self._repository_info = None
            raise ResticStoreError(ResticStoreErrorCode.REPOSITORY_MISSING)
        self._repository_info = info
        return info

    def validate_existing(self) -> RepositoryInfo:
        """Read-only alias for :meth:`open_existing`."""
        return self.open_existing()

    def _require_initialized(self) -> None:
        self._ensure_open()
        if self._repository_info is None:
            raise ResticStoreError(ResticStoreErrorCode.NOT_OPEN)

    @property
    def repository_info(self) -> RepositoryInfo:
        self._require_initialized()
        assert self._repository_info is not None
        return self._repository_info

    def _validate_workspace(self, workspace: Path, exclude_file: Path | None) -> Path:
        try:
            root = Path(workspace).resolve(strict=True)
        except OSError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        if not root.is_dir():
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        protected = [self._password_file, self._cache_dir]
        if self._ca_bundle is not None:
            protected.append(self._ca_bundle)
        if not self._repository.startswith("s3:"):
            protected.append(Path(self._repository))
        if exclude_file is not None:
            try:
                policy = Path(exclude_file).resolve(strict=True)
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
            if not policy.is_file():
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
            protected.append(policy)
        for item in protected:
            try:
                Path(item).resolve().relative_to(root)
            except ValueError:
                continue
            except OSError:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        return root

    def backup(
        self,
        workspace: Path,
        parent: str | None = None,
        excludes: Path | None = None,
        on_progress: Callable[[BackupProgress], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> BackupSummary:
        self._require_initialized()
        root = self._validate_workspace(workspace, excludes)
        if parent is not None:
            _validate_snapshot_id(parent)
        if on_progress is not None and not callable(on_progress):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if cancellation is not None and cancellation.cancelled:
            raise ResticStoreError(ResticStoreErrorCode.CANCELLED)
        force_scan = sys.platform.startswith("win")
        args = ["backup", "--json", "--skip-if-unchanged", "--no-scan"]
        if force_scan:
            args.append("--force")
        elif parent is not None:
            args.extend(["--parent", parent])
        if excludes is not None:
            args.extend(["--exclude-file", str(Path(excludes).resolve())])
        args.extend(["--", "."])
        with self._process(args, cwd=root) as process:
            def events() -> Iterator[bytes]:
                yield from self._read_lines(process, cancellation)

            try:
                summary = parse_backup_events(
                    events(),
                    on_progress=on_progress,
                    max_event_bytes=self._max_json_event_bytes,
                )
            except ResticStoreError as error:
                code = process.poll()
                if code is not None and code != 0:
                    self._raise_exit(code, orphan_snapshot_id=error.orphan_snapshot_id)
                self._terminate(process)
                raise
            code = self._wait(process)
            if cancellation is not None and cancellation.cancelled:
                raise ResticStoreError(
                    ResticStoreErrorCode.CANCELLED,
                    exit_code=code,
                    orphan_snapshot_id=summary.snapshot_id,
                )
            self._raise_exit(code, orphan_snapshot_id=summary.snapshot_id)
            self.data_added_bytes += summary.data_added
            return replace(
                summary,
                force_scan=force_scan,
                effective_parent_id=None if force_scan else parent,
            )

    @staticmethod
    def _raise_exit(code: int, *, orphan_snapshot_id: str | None = None) -> None:
        if code == 0:
            return
        if code == 3:
            raise ResticStoreError(
                ResticStoreErrorCode.INCOMPLETE_BACKUP,
                exit_code=code,
                orphan_snapshot_id=orphan_snapshot_id,
            )
        if code in {130, -2, -15}:
            raise ResticStoreError(
                ResticStoreErrorCode.CANCELLED,
                exit_code=code,
                orphan_snapshot_id=orphan_snapshot_id,
            )
        if code not in {1, 2, 10, 11, 12, 14, 15, 16}:
            raise ResticStoreError(
                ResticStoreErrorCode.UNKNOWN_EXIT,
                exit_code=code,
                orphan_snapshot_id=orphan_snapshot_id,
            )
        raise ResticStoreError(
            ResticStoreErrorCode.COMMAND_FAILED,
            exit_code=code,
            orphan_snapshot_id=orphan_snapshot_id,
        )

    def snapshot(self, snapshot_id: str) -> SnapshotInfo:
        self._require_initialized()
        snapshot_id = _validate_snapshot_id(snapshot_id)
        cached = self._snapshot_info.get(snapshot_id)
        if cached is not None:
            return cached
        _code, output = self._capture(["snapshots", "--json", snapshot_id])
        snapshot = parse_snapshot(output, snapshot_id)
        self._snapshot_info[snapshot_id] = snapshot
        return snapshot

    def snapshots(self) -> tuple[SnapshotInventoryItem, ...]:
        """Return a bounded, read-only inventory with private fields omitted."""
        self._require_initialized()
        _code, output = self._capture(["snapshots", "--json"])
        return parse_snapshot_inventory(output)

    def entries(self, snapshot_id: str, *, expected_tree_id: str | None = None) -> Iterator[SnapshotEntry]:
        self._require_initialized()
        snapshot_id = _validate_snapshot_id(snapshot_id)
        from .parent_inventory import read_inventory, write_inventory

        if expected_tree_id is not None:
            if _REPOSITORY_ID.fullmatch(expected_tree_id) is None:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
            cached = read_inventory(self._cache_dir, self._repository, self._repository_info.repository_id, snapshot_id, expected_tree_id)
            if cached is not None:
                yield from cached
                return
        snapshot = self.snapshot(snapshot_id)
        if expected_tree_id is not None and snapshot.tree_id != expected_tree_id:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
        rows = []
        with self._process(["ls", "--json", snapshot_id]) as process:
            try:
                for row in parse_snapshot_entries(
                    self._read_lines(process),
                    snapshot,
                    max_event_bytes=self._max_json_event_bytes,
                    max_entries=self._max_entries,
                ):
                    rows.append(row)
                    yield row
            except ResticStoreError:
                code = process.poll()
                if code is not None and code != 0:
                    self._raise_exit(code)
                self._terminate(process)
                raise
            self._raise_exit(self._wait(process))
        write_inventory(self._cache_dir, self._repository, self._repository_info.repository_id, snapshot_id, snapshot.tree_id, rows)

    def restore(self, snapshot_id: str, destination: Path) -> None:
        self._require_initialized()
        snapshot_id = _validate_snapshot_id(snapshot_id)
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        parent = target.parent.resolve(strict=True)
        if not parent.is_dir():
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        self._capture(["restore", snapshot_id, "--target", str(target)])

    def check(
        self,
        read_data: bool = False,
        *,
        read_data_subset: str | None = None,
    ) -> CheckResult:
        self._require_initialized()
        if type(read_data) is not bool:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if read_data_subset is not None:
            if read_data or not isinstance(read_data_subset, str):
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
            fraction = _SUBSET_FRACTION.fullmatch(read_data_subset)
            if fraction is None:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
            numerator, denominator = (int(part) for part in fraction.groups())
            if numerator > denominator:
                raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        args = ["check"]
        if read_data:
            args.append("--read-data")
        elif read_data_subset is not None:
            args.extend(["--read-data-subset", read_data_subset])
        self._capture(args)
        return CheckResult(read_data, read_data_subset)

    def plan_forget(self, snapshot_ids: Iterable[str]) -> ForgetPlan:
        """Run restic's native dry-run for an explicit set of full snapshot IDs."""
        self._require_initialized()
        selected = _explicit_snapshot_ids(snapshot_ids)
        inventory = {snapshot.snapshot_id: snapshot for snapshot in self.snapshots()}
        if any(snapshot_id not in inventory for snapshot_id in selected):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        self._capture(["forget", "--dry-run", *selected])
        return ForgetPlan(
            repository_id=self.repository_info.repository_id,
            snapshot_ids=selected,
            tree_ids=tuple(inventory[snapshot_id].tree_id for snapshot_id in selected),
        )

    def forget(
        self,
        snapshot_ids: Iterable[str],
        *,
        plan: ForgetPlan,
        confirmed: bool = False,
    ) -> MaintenanceResult:
        """Forget only the explicit snapshots in a matching dry-run plan."""
        self._require_initialized()
        selected = _explicit_snapshot_ids(snapshot_ids)
        if type(confirmed) is not bool or not confirmed:
            raise ResticStoreError(ResticStoreErrorCode.CONFIRMATION_REQUIRED)
        if (
            not isinstance(plan, ForgetPlan)
            or plan.repository_id != self.repository_info.repository_id
            or plan.snapshot_ids != selected
            or len(plan.tree_ids) != len(selected)
        ):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        inventory = {snapshot.snapshot_id: snapshot for snapshot in self.snapshots()}
        if any(
            snapshot_id not in inventory
            or inventory[snapshot_id].tree_id != tree_id
            for snapshot_id, tree_id in zip(selected, plan.tree_ids, strict=True)
        ):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        self._capture(["forget", *selected])
        for snapshot_id in selected:
            self._snapshot_info.pop(snapshot_id, None)
        return MaintenanceResult("forget", False, selected)

    def prune(self, dry_run: bool = True, *, confirmed: bool = False) -> MaintenanceResult:
        """Run an explicit prune plan; destructive prune requires confirmation."""
        self._require_initialized()
        if type(dry_run) is not bool or type(confirmed) is not bool:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if not dry_run and not confirmed:
            raise ResticStoreError(ResticStoreErrorCode.CONFIRMATION_REQUIRED)
        args = ["prune"]
        if dry_run:
            args.append("--dry-run")
        self._capture(args)
        return MaintenanceResult("prune", dry_run)
