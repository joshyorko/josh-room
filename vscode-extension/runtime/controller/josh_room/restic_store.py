"""Bounded subprocess adapter for a pinned restic Room Store repository."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from .adapter_contract import CancellationToken
from .cancellation import terminate_owned_process

RESTIC_VERSION = "0.19.1"
SUPPORTED_REPOSITORY_FORMAT = 2
MAX_JSON_EVENT_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1_000_000
MAX_INTEGER = (1 << 63) - 1
_REPOSITORY_ID = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ID = re.compile(r"^[0-9a-f]{64}$")
_ENTRY_TYPE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
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
    INITIALIZE_FAILED = "initialize-failed"
    COMMAND_FAILED = "command-failed"
    UNKNOWN_EXIT = "unknown-exit"
    INCOMPLETE_BACKUP = "incomplete-backup"
    BACKUP_ERRORS = "backup-errors"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed-out"
    PROGRESS_CALLBACK_FAILED = "progress-callback-failed"


_ERROR_MESSAGES = {
    ResticStoreErrorCode.NOT_OPEN: "restic store context is not open",
    ResticStoreErrorCode.INVALID_CONFIGURATION: "restic store configuration is invalid",
    ResticStoreErrorCode.INVALID_OUTPUT: "restic returned invalid or unsupported output",
    ResticStoreErrorCode.VERSION_MISMATCH: "restic version does not match the pinned version",
    ResticStoreErrorCode.REPOSITORY_FORMAT: "restic repository format is unsupported",
    ResticStoreErrorCode.REPOSITORY_ID: "restic repository identity is invalid",
    ResticStoreErrorCode.INITIALIZE_FAILED: "restic repository initialization failed",
    ResticStoreErrorCode.COMMAND_FAILED: "restic command failed",
    ResticStoreErrorCode.UNKNOWN_EXIT: "restic returned an unknown exit status",
    ResticStoreErrorCode.INCOMPLETE_BACKUP: "restic backup is incomplete and cannot be published",
    ResticStoreErrorCode.BACKUP_ERRORS: "restic backup reported source errors",
    ResticStoreErrorCode.CANCELLED: "restic operation was cancelled",
    ResticStoreErrorCode.TIMED_OUT: "restic operation timed out",
    ResticStoreErrorCode.PROGRESS_CALLBACK_FAILED: "restic progress handler failed",
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


@dataclass(frozen=True, slots=True)
class SnapshotInfo:
    snapshot_id: str
    tree_id: str
    parent_snapshot_id: str | None
    time: str
    paths: tuple[str, ...]


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


def parse_snapshot(raw: bytes | str, expected_snapshot_id: str) -> SnapshotInfo:
    expected = _validate_snapshot_id(expected_snapshot_id)
    value = _load_json(raw)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ResticStoreError(ResticStoreErrorCode.INVALID_OUTPUT)
    snapshot = value[0]
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
        snapshot_id != expected
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
        if not stat.S_ISREG(password_stat.st_mode) or stat.S_ISLNK(password_stat.st_mode):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if stat.S_IMODE(password_stat.st_mode) != 0o600:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        try:
            self._cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            cache_stat = self._cache_dir.lstat()
        except OSError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        if not stat.S_ISDIR(cache_stat.st_mode) or stat.S_ISLNK(cache_stat.st_mode):
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        if cache_stat.st_mode & 0o077:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
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

    def _require_initialized(self) -> None:
        self._ensure_open()
        if self._repository_info is None:
            raise ResticStoreError(ResticStoreErrorCode.NOT_OPEN)

    def _validate_workspace(self, workspace: Path, exclude_file: Path | None) -> Path:
        try:
            root = Path(workspace).resolve(strict=True)
        except OSError:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION) from None
        if not root.is_dir():
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        protected = [self._password_file, self._cache_dir]
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
        args = ["backup", "--json", "--skip-if-unchanged"]
        if parent is not None:
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
            return summary

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

    def entries(self, snapshot_id: str) -> Iterator[SnapshotEntry]:
        self._require_initialized()
        snapshot_id = _validate_snapshot_id(snapshot_id)
        snapshot = self.snapshot(snapshot_id)
        with self._process(["ls", "--json", snapshot_id]) as process:
            try:
                yield from parse_snapshot_entries(
                    self._read_lines(process),
                    snapshot,
                    max_event_bytes=self._max_json_event_bytes,
                    max_entries=self._max_entries,
                )
            except ResticStoreError:
                code = process.poll()
                if code is not None and code != 0:
                    self._raise_exit(code)
                self._terminate(process)
                raise
            self._raise_exit(self._wait(process))

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

    def check(self, read_data: bool = False) -> CheckResult:
        self._require_initialized()
        if type(read_data) is not bool:
            raise ResticStoreError(ResticStoreErrorCode.INVALID_CONFIGURATION)
        args = ["check"]
        if read_data:
            args.append("--read-data")
        self._capture(args)
        return CheckResult(read_data)
