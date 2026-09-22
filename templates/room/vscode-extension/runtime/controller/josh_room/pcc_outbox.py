"""Crash-safe local trigger queue and prepared delivery outbox.

This module owns only local PCC lifecycle state.  Enqueue accepts already
bounded logical metadata; it never discovers sources, reads transcripts,
loads policy or key material, encrypts, uploads, or contacts a provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised by the platform contract test
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised by the platform contract test
    _msvcrt = None


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_KEY = re.compile(r"^objects/sha256/([0-9a-f]{64})$")
_MIME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*/[A-Za-z0-9][A-Za-z0-9.+-]*$")
_MAX_METADATA_BYTES = 16 * 1024
_MAX_CIPHERTEXT_BYTES = 8 * 1024 * 1024 * 1024
_MAX_EVENT_IDS = 64


class QueueState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SOURCE_SNAPSHOTTED = "source-snapshotted"
    PREPARED_ENCRYPTED = "prepared-encrypted"
    OBJECT_UPLOADED = "object-uploaded"
    INDEX_PUBLISHED = "index-published"
    COMMITTED = "committed"
    RETRYABLE_FAILURE = "retryable-failure"
    QUARANTINED = "quarantined"
    POLICY_DENIED = "policy-denied"
    CAPTURE_GAP = "capture-gap"


OutboxState = QueueState


class OutboxError(RuntimeError):
    """Base error with a public-safe, stable message."""


class LeaseConflict(OutboxError):
    def __init__(self) -> None:
        super().__init__("outbox lease conflict")


class InvalidTransition(OutboxError):
    def __init__(self) -> None:
        super().__init__("outbox transition is invalid")


class OutboxStorageError(OutboxError):
    def __init__(self, code: str = "storage-unavailable", *, pending_preserved: bool | None = None) -> None:
        self.code = code
        self.pending_preserved = pending_preserved
        super().__init__(f"outbox storage error: {code}")


def _finite_number(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _ensure_directory(path: Path) -> bool:
    try:
        if path.is_symlink():
            raise OutboxStorageError("storage-unavailable")
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode):
            raise OutboxStorageError("storage-unavailable")
        return not existed
    except OutboxStorageError:
        raise
    except OSError as error:
        raise OutboxStorageError("storage-unavailable") from error


def _validate_root_path(path: Path) -> None:
    try:
        if path.is_symlink() or (path.exists() and not stat.S_ISDIR(path.lstat().st_mode)):
            raise ValueError("outbox root is not a directory")
    except OSError as error:
        raise ValueError("outbox root is unavailable") from error


@dataclass(frozen=True)
class CaptureGap:
    reason_code: str
    pending_preserved: bool
    retryable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "pending_preserved": self.pending_preserved,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class QueueReceipt:
    event_id: str
    state: QueueState
    coalesced: bool = False
    sequence: int | None = None
    is_final: bool = False
    diagnostic: CaptureGap | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "event_id": self.event_id,
            "state": self.state.value,
            "coalesced": self.coalesced,
            "is_final": self.is_final,
        }
        if self.sequence is not None:
            body["sequence"] = self.sequence
        if self.diagnostic is not None:
            body["diagnostic"] = self.diagnostic.to_dict()
        return body


@dataclass(frozen=True)
class QueueRecord:
    event_id: str
    session_id: str
    checkpoint: dict[str, object]
    metadata: dict[str, object]
    state: QueueState
    sequence: int
    event_ids: tuple[str, ...]
    is_final: bool = False
    final_event_id: str | None = None
    owner: str | None = None
    lease_until: float | None = None
    lease_seconds: float = 60.0
    resume_state: QueueState = QueueState.QUEUED
    failure_code: str | None = None
    object_key: str | None = None
    ciphertext_sha256: str | None = None
    ciphertext_size: int | None = None
    index_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "checkpoint": self.checkpoint,
            "metadata": self.metadata,
            "state": self.state.value,
            "sequence": self.sequence,
            "event_ids": list(self.event_ids),
            "is_final": self.is_final,
            "final_event_id": self.final_event_id,
            "owner": self.owner,
            "lease_until": self.lease_until,
            "lease_seconds": self.lease_seconds,
            "resume_state": self.resume_state.value,
            "failure_code": self.failure_code,
            "object_key": self.object_key,
            "ciphertext_sha256": self.ciphertext_sha256,
            "ciphertext_size": self.ciphertext_size,
            "index_id": self.index_id,
        }
        return body

    @classmethod
    def from_dict(cls, body: object) -> QueueRecord:
        if not isinstance(body, dict):
            raise TypeError("record shape")
        required = {
            "event_id",
            "session_id",
            "checkpoint",
            "metadata",
            "state",
            "sequence",
            "event_ids",
            "is_final",
            "final_event_id",
            "owner",
            "lease_until",
            "lease_seconds",
            "resume_state",
            "failure_code",
            "object_key",
            "ciphertext_sha256",
            "ciphertext_size",
            "index_id",
        }
        if set(body) != required:
            raise ValueError("record fields")
        event_id = _identifier(body["event_id"])
        session_id = _identifier(body["session_id"])
        checkpoint = _validate_checkpoint(body["checkpoint"])
        metadata = _validate_metadata(body["metadata"])
        try:
            state = QueueState(body["state"])
            resume_state = QueueState(body["resume_state"])
        except (TypeError, ValueError) as error:
            raise ValueError("record state") from error
        if type(body["sequence"]) is not int or body["sequence"] < 1:
            raise ValueError("record sequence")
        event_ids = body["event_ids"]
        if (
            not isinstance(event_ids, list)
            or not 1 <= len(event_ids) <= _MAX_EVENT_IDS
            or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in event_ids)
            or len(set(event_ids)) != len(event_ids)
        ):
            raise ValueError("record event ids")
        if type(body["is_final"]) is not bool:
            raise ValueError("record final")
        final_event_id = body["final_event_id"]
        if final_event_id is not None and (not isinstance(final_event_id, str) or not _IDENTIFIER.fullmatch(final_event_id)):
            raise ValueError("record final event")
        owner = body["owner"]
        if owner is not None:
            _identifier(owner)
        lease_until = body["lease_until"]
        if lease_until is not None and (not _finite_number(lease_until) or lease_until < 0):
            raise ValueError("record lease")
        lease_seconds = body["lease_seconds"]
        if not _finite_number(lease_seconds) or not 0 < lease_seconds <= 86400:
            raise ValueError("record lease duration")
        failure_code = body["failure_code"]
        if failure_code is not None:
            _identifier(failure_code)
        object_key = body["object_key"]
        if object_key is not None and not _OBJECT_KEY.fullmatch(object_key):
            raise ValueError("record object key")
        digest = body["ciphertext_sha256"]
        if digest is not None and not _DIGEST.fullmatch(digest):
            raise ValueError("record ciphertext digest")
        size = body["ciphertext_size"]
        if size is not None and (type(size) is not int or not 0 <= size <= _MAX_CIPHERTEXT_BYTES):
            raise ValueError("record ciphertext size")
        index_id = body["index_id"]
        if index_id is not None:
            _identifier(index_id)
        progress_states = {
            QueueState.SOURCE_SNAPSHOTTED,
            QueueState.PREPARED_ENCRYPTED,
            QueueState.OBJECT_UPLOADED,
            QueueState.INDEX_PUBLISHED,
        }
        if event_id not in event_ids:
            raise ValueError("record primary event")
        if body["is_final"] and final_event_id is None:
            raise ValueError("record final event")
        if final_event_id is not None and (not body["is_final"] or final_event_id not in event_ids):
            raise ValueError("record final event")
        if state is QueueState.QUEUED and resume_state is not QueueState.QUEUED:
            raise ValueError("queued record progress")
        if state is QueueState.CLAIMED and resume_state not in {QueueState.QUEUED, *progress_states}:
            raise ValueError("claimed record progress")
        if state in progress_states and resume_state is not state:
            raise ValueError("progress record stage")
        if state in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP} and resume_state not in {QueueState.QUEUED, *progress_states}:
            raise ValueError("failure record progress")
        if state is QueueState.COMMITTED and (resume_state is not QueueState.INDEX_PUBLISHED or index_id is None):
            raise ValueError("committed record index")
        if state is QueueState.INDEX_PUBLISHED and index_id is None:
            raise ValueError("indexed record index")
        if state in {QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED, QueueState.COMMITTED} and object_key is None:
            raise ValueError("published record object")
        if state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED, QueueState.COMMITTED} and (
            digest is None or size is None
        ):
            raise ValueError("prepared record ciphertext")
        if owner is None and lease_until is not None:
            raise ValueError("unowned record lease")
        if owner is not None and lease_until is None:
            raise ValueError("owned record lease")
        if state in {QueueState.QUEUED, QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP, QueueState.QUARANTINED, QueueState.POLICY_DENIED, QueueState.COMMITTED} and owner is not None:
            raise ValueError("unowned state owner")
        if state is QueueState.CLAIMED and owner is None:
            raise ValueError("claimed record owner")
        return cls(
            event_id=event_id,
            session_id=session_id,
            checkpoint=checkpoint,
            metadata=metadata,
            state=state,
            sequence=body["sequence"],
            event_ids=tuple(event_ids),
            is_final=body["is_final"],
            final_event_id=final_event_id,
            owner=owner,
            lease_until=float(lease_until) if lease_until is not None else None,
            lease_seconds=float(lease_seconds),
            resume_state=resume_state,
            failure_code=failure_code,
            object_key=object_key,
            ciphertext_sha256=digest,
            ciphertext_size=size,
            index_id=index_id,
        )


@dataclass(frozen=True)
class PreparedRecord:
    event_id: str
    ciphertext: bytes
    metadata: dict[str, object]
    ciphertext_sha256: str
    ciphertext_size: int

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "ciphertext_b64": base64.b64encode(self.ciphertext).decode("ascii"),
            "ciphertext_sha256": self.ciphertext_sha256,
            "ciphertext_size": self.ciphertext_size,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, body: object) -> PreparedRecord:
        if not isinstance(body, dict) or set(body) != {
            "event_id",
            "ciphertext_b64",
            "ciphertext_sha256",
            "ciphertext_size",
            "metadata",
        }:
            raise ValueError("prepared fields")
        event_id = _identifier(body["event_id"])
        encoded = body["ciphertext_b64"]
        if not isinstance(encoded, str) or len(encoded) > ((_MAX_CIPHERTEXT_BYTES + 2) // 3) * 4:
            raise ValueError("prepared ciphertext")
        try:
            ciphertext = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as error:
            raise ValueError("prepared ciphertext") from error
        digest = body["ciphertext_sha256"]
        size = body["ciphertext_size"]
        if (
            not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or type(size) is not int
            or size != len(ciphertext)
            or hashlib.sha256(ciphertext).hexdigest() != digest
        ):
            raise ValueError("prepared ciphertext metadata")
        metadata = _validate_metadata(body["metadata"])
        return cls(event_id, ciphertext, metadata, digest, size)


@dataclass(frozen=True)
class Diagnostic:
    code: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code}


@dataclass(frozen=True)
class Inspection:
    records: list[QueueRecord]
    diagnostics: list[Diagnostic]
    quarantined_count: int = 0
    partial_count: int = 0
    orphan_prepared: list[str] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "records": [record.to_dict() for record in self.records],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "quarantined_count": self.quarantined_count,
            "partial_count": self.partial_count,
            "orphan_prepared": list(self.orphan_prepared or []),
        }


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier is invalid")
    return value


def _validate_checkpoint(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"source", "representation", "start", "end", "prefix_sha256"}:
        raise ValueError("checkpoint is invalid")
    source = _identifier(value["source"])
    representation = _identifier(value["representation"])
    start = value["start"]
    end = value["end"]
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ValueError("checkpoint boundary is invalid")
    prefix = value["prefix_sha256"]
    if not isinstance(prefix, str) or not _DIGEST.fullmatch(prefix):
        raise ValueError("checkpoint digest is invalid")
    return {"source": source, "representation": representation, "start": start, "end": end, "prefix_sha256": prefix}


def _validate_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("public metadata is invalid")
    allowed = {
        "workspace_id",
        "source_surface",
        "source_adapter",
        "source_adapter_version",
        "content_type",
        "content_sha256",
        "content_size",
        "policy_decision",
        "capture_status",
        "sensitivity",
        "ciphertext_sha256",
        "ciphertext_size",
        "object_key",
        "index_id",
    }
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise ValueError("public metadata contains an unsupported field")
    result: dict[str, object] = {}
    for key, item in value.items():
        if key in {"workspace_id", "source_surface", "source_adapter", "source_adapter_version", "index_id"}:
            result[key] = _identifier(item)
        elif key == "content_type":
            if not isinstance(item, str) or len(item) > 128 or not _MIME.fullmatch(item):
                raise ValueError("public metadata content type is invalid")
            result[key] = item
        elif key in {"content_sha256", "ciphertext_sha256"}:
            if not isinstance(item, str) or not _DIGEST.fullmatch(item):
                raise ValueError("public metadata digest is invalid")
            result[key] = item
        elif key in {"content_size", "ciphertext_size"}:
            if type(item) is not int or not 0 <= item <= _MAX_CIPHERTEXT_BYTES:
                raise ValueError("public metadata size is invalid")
            result[key] = item
        elif key == "object_key":
            if not isinstance(item, str) or not _OBJECT_KEY.fullmatch(item):
                raise ValueError("public metadata object key is invalid")
            result[key] = item
        elif key == "policy_decision":
            if item not in {"allow", "deny", "local-only", "quarantine"}:
                raise ValueError("public metadata policy decision is invalid")
            result[key] = item
        elif key == "capture_status":
            if item not in {"partial", "complete", "recovered", "quarantined"}:
                raise ValueError("public metadata capture status is invalid")
            result[key] = item
        elif key == "sensitivity":
            if item not in {"normal", "sensitive", "unknown"}:
                raise ValueError("public metadata sensitivity is invalid")
            result[key] = item
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_METADATA_BYTES:
        raise ValueError("public metadata exceeds bound")
    return result


def _checkpoint_key(session_id: str, checkpoint: Mapping[str, object]) -> tuple[object, ...]:
    return (
        session_id,
        checkpoint["source"],
        checkpoint["representation"],
        checkpoint["start"],
        checkpoint["end"],
        checkpoint["prefix_sha256"],
    )


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    try:
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.lstat().st_mode)):
            raise OutboxStorageError("storage-unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OutboxStorageError:
        raise
    except OSError as error:
        raise OutboxStorageError("storage-unavailable") from error
    with handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            return
        if _msvcrt is None:
            raise OutboxStorageError("unsupported-lock-platform")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


def _sync_directory(directory: Path, *, platform_name: str | None = None) -> None:
    if (platform_name or os.name) != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _DurablePublisher:
    def publish(self, path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(temporary, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _sync_directory(path.parent)
        except OSError as error:
            raise OutboxStorageError(
                "publication-failed",
                pending_preserved=path.is_file() and not path.is_symlink(),
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class PreparedOutbox:
    """The ciphertext-bearing local outbox, separate from trigger state."""

    def __init__(self, root: Path, *, publisher: _DurablePublisher | None = None):
        self.root = Path(root)
        _validate_root_path(self.root)
        self.directory = self.root / "prepared"
        self._publisher = publisher or _DurablePublisher()
        self._lock_path = self.root / "state.lock"

    def _path(self, event_id: str) -> Path:
        return self.directory / f"{_identifier(event_id)}.json"

    def _publish_unlocked(self, record: PreparedRecord) -> None:
        body = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._publisher.publish(self._path(record.event_id), body)

    def _quarantine_unlocked(self, path: Path) -> None:
        target_directory = self.root / "quarantine"
        _ensure_directory(target_directory)
        target = target_directory / f"prepared-corrupt-{uuid.uuid4().hex}.json"
        try:
            os.replace(path, target)
        except OSError as error:
            raise OutboxStorageError("prepared-record-unmovable") from error

    def publish(self, record: PreparedRecord) -> None:
        with _exclusive_file_lock(self._lock_path):
            root_created = _ensure_directory(self.root)
            directory_created = _ensure_directory(self.directory)
            if root_created or directory_created:
                _sync_directory(self.root)
            self._publish_unlocked(record)

    def inspect_record(self, event_id: str) -> PreparedRecord | None:
        with _exclusive_file_lock(self._lock_path):
            _ensure_directory(self.root)
            _ensure_directory(self.directory)
            path = self._path(event_id)
            try:
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("prepared record is not regular")
                body = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                self._quarantine_unlocked(path)
                raise OutboxStorageError("prepared-record-corrupt") from error
            try:
                return PreparedRecord.from_dict(body)
            except (TypeError, ValueError) as error:
                self._quarantine_unlocked(path)
                raise OutboxStorageError("prepared-record-corrupt") from error

    def _records_unlocked(self) -> dict[str, PreparedRecord]:
        result: dict[str, PreparedRecord] = {}
        if not self.directory.is_dir():
            return result
        for path in self.directory.glob("*.json"):
            try:
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("prepared record is not regular")
                body = json.loads(path.read_text(encoding="utf-8"))
                record = PreparedRecord.from_dict(body)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise OutboxStorageError("prepared-record-corrupt") from error
            result[record.event_id] = record
        return result


class PccOutbox:
    """A local-only trigger queue plus its distinct prepared ciphertext outbox."""

    def __init__(
        self,
        root: Path,
        *,
        max_events: int = 10_000,
        max_bytes: int = 256 * 1024 * 1024,
        lease_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
        publisher: _DurablePublisher | None = None,
    ):
        if type(max_events) is not int or max_events < 1:
            raise ValueError("outbox event quota is invalid")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("outbox byte quota is invalid")
        if not _finite_number(lease_seconds) or not 0 < lease_seconds <= 86400:
            raise ValueError("outbox lease duration is invalid")
        self.root = Path(root)
        _validate_root_path(self.root)
        self.queue_directory = self.root / "queue"
        self.quarantine_directory = self.root / "quarantine"
        self._lock_path = self.root / "state.lock"
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.lease_seconds = float(lease_seconds)
        self.clock = clock or time.time
        self._publisher = publisher or _DurablePublisher()
        self.prepared = PreparedOutbox(self.root, publisher=self._publisher)

    def _path(self, event_id: str) -> Path:
        return self.queue_directory / f"{_identifier(event_id)}.json"

    def _ensure_layout(self) -> None:
        root_created = _ensure_directory(self.root)
        queue_created = _ensure_directory(self.queue_directory)
        prepared_created = _ensure_directory(self.prepared.directory)
        quarantine_created = _ensure_directory(self.quarantine_directory)
        if root_created or queue_created or prepared_created or quarantine_created:
            _sync_directory(self.root)

    def _publish_record_unlocked(self, record: QueueRecord) -> None:
        body = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._publisher.publish(self._path(record.event_id), body)

    def _prepared_bytes_unlocked(self) -> int:
        total = 0
        if not self.prepared.directory.is_dir():
            return total
        for path in self.prepared.directory.iterdir():
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                raise OutboxStorageError("prepared-record-unavailable") from error
            if path.is_symlink() or not stat.S_ISREG(mode):
                raise OutboxStorageError("prepared-record-corrupt")
            total += path.lstat().st_size
        return total

    def _read_record(self, path: Path) -> QueueRecord:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("queue record is not regular")
        body = json.loads(path.read_text(encoding="utf-8"))
        return QueueRecord.from_dict(body)

    def _records_unlocked(self) -> list[QueueRecord]:
        result = []
        if not self.queue_directory.is_dir():
            return result
        for path in sorted(self.queue_directory.glob("*.json")):
            result.append(self._read_record(path))
        return result

    def _quarantine_corrupt_unlocked(self, path: Path) -> None:
        _ensure_directory(self.quarantine_directory)
        target = self.quarantine_directory / f"corrupt-{uuid.uuid4().hex}.json"
        try:
            os.replace(path, target)
        except OSError as error:
            raise OutboxStorageError("corrupt-record-unmovable") from error

    def _safe_records_unlocked(self) -> tuple[list[QueueRecord], list[Diagnostic], int]:
        records: list[QueueRecord] = []
        diagnostics: list[Diagnostic] = []
        quarantined = 0
        if not self.queue_directory.is_dir():
            return records, diagnostics, quarantined
        for path in sorted(self.queue_directory.glob("*.json")):
            try:
                records.append(self._read_record(path))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                self._quarantine_corrupt_unlocked(path)
                quarantined += 1
                diagnostics.append(Diagnostic("corrupt-record"))
        records.sort(key=lambda item: item.sequence)
        return records, diagnostics, quarantined

    def enqueue(
        self,
        *,
        event_id: str,
        session_id: str,
        checkpoint: Mapping[str, object],
        is_final: bool = False,
        metadata: Mapping[str, object] | None = None,
        policy_decision: str = "allow",
        diagnostic_detail: object | None = None,
    ) -> QueueReceipt:
        del diagnostic_detail  # Deliberately inert: enqueue never captures caller data.
        event_id = _identifier(event_id)
        session_id = _identifier(session_id)
        checkpoint = _validate_checkpoint(checkpoint)
        metadata = _validate_metadata(metadata or {})
        if type(is_final) is not bool:
            raise ValueError("final marker is invalid")
        if policy_decision not in {"allow", "deny", "local-only", "quarantine"}:
            raise ValueError("policy decision is invalid")
        if policy_decision == "deny":
            initial_state = QueueState.POLICY_DENIED
        elif policy_decision == "quarantine":
            initial_state = QueueState.QUARANTINED
        else:
            initial_state = QueueState.QUEUED
        try:
            with _exclusive_file_lock(self._lock_path):
                self._ensure_layout()
                records, _diagnostics, _quarantined = self._safe_records_unlocked()
                key = _checkpoint_key(session_id, checkpoint)
                existing = next((item for item in records if _checkpoint_key(item.session_id, item.checkpoint) == key), None)
                event_owner = next((item for item in records if event_id in item.event_ids), None)
                if event_owner is not None and event_owner is not existing:
                    return QueueReceipt(
                        event_id,
                        QueueState.CAPTURE_GAP,
                        diagnostic=CaptureGap("event-id-conflict", True),
                    )
                if existing is not None:
                    event_ids = list(existing.event_ids)
                    quota_hit = False
                    if event_id not in event_ids:
                        if len(event_ids) >= _MAX_EVENT_IDS:
                            if not is_final:
                                return QueueReceipt(
                                    existing.event_id,
                                    QueueState.CAPTURE_GAP,
                                    coalesced=True,
                                    sequence=existing.sequence,
                                    is_final=existing.is_final,
                                    diagnostic=CaptureGap("event-id-quota", True),
                                )
                            # Preserve the primary identity and the final marker
                            # within the fixed event-id bound.  Older duplicate
                            # ids are only coalescing evidence and may be evicted.
                            event_ids = [existing.event_id, *event_ids[1 : _MAX_EVENT_IDS - 1], event_id]
                            quota_hit = True
                        else:
                            event_ids.append(event_id)
                    final_event_id = existing.final_event_id
                    if is_final:
                        final_event_id = event_id
                    updated = QueueRecord(
                        **{
                            **existing.__dict__,
                            "event_ids": tuple(event_ids),
                            "is_final": existing.is_final or is_final,
                            "final_event_id": final_event_id,
                        }
                    )
                    try:
                        self._publish_record_unlocked(updated)
                    except OutboxStorageError as error:
                        return QueueReceipt(
                            existing.event_id,
                            QueueState.CAPTURE_GAP,
                            coalesced=True,
                            sequence=existing.sequence,
                            is_final=existing.is_final,
                            diagnostic=CaptureGap(
                                "publication-failed",
                                error.pending_preserved if error.pending_preserved is not None else True,
                            ),
                        )
                    if quota_hit:
                        return QueueReceipt(
                            updated.event_id,
                            QueueState.CAPTURE_GAP,
                            coalesced=True,
                            sequence=updated.sequence,
                            is_final=updated.is_final,
                            diagnostic=CaptureGap("event-id-quota", True),
                        )
                    return QueueReceipt(updated.event_id, updated.state, coalesced=True, sequence=updated.sequence, is_final=updated.is_final)
                if len(records) >= self.max_events:
                    return QueueReceipt(event_id, QueueState.CAPTURE_GAP, diagnostic=CaptureGap("queue-full", False))
                current_bytes = sum(self._path(item.event_id).stat().st_size for item in records)
                sequence = max((item.sequence for item in records), default=0) + 1
                record = QueueRecord(
                    event_id=event_id,
                    session_id=session_id,
                    checkpoint=checkpoint,
                    metadata=metadata,
                    state=initial_state,
                    sequence=sequence,
                    event_ids=(event_id,),
                    is_final=is_final,
                    final_event_id=event_id if is_final else None,
                    resume_state=initial_state if initial_state is QueueState.QUEUED else QueueState.QUEUED,
                )
                encoded = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode()
                if current_bytes + len(encoded) > self.max_bytes:
                    return QueueReceipt(event_id, QueueState.CAPTURE_GAP, diagnostic=CaptureGap("queue-full", False))
                try:
                    self._publisher.publish(self._path(event_id), encoded)
                except OutboxStorageError as error:
                    return QueueReceipt(
                        event_id,
                        QueueState.CAPTURE_GAP,
                        diagnostic=CaptureGap(
                            "publication-failed",
                            error.pending_preserved if error.pending_preserved is not None else False,
                        ),
                    )
                return QueueReceipt(event_id, initial_state, is_final=is_final, sequence=sequence)
        except (OutboxStorageError, OSError):
            return QueueReceipt(event_id, QueueState.CAPTURE_GAP, diagnostic=CaptureGap("storage-unavailable", False))

    def inspect_record(self, event_id: str) -> QueueRecord | None:
        event_id = _identifier(event_id)
        try:
            with _exclusive_file_lock(self._lock_path):
                self._ensure_layout()
                path = self._path(event_id)
                if not path.exists() and not path.is_symlink():
                    return None
                try:
                    return self._read_record(path)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                    self._quarantine_corrupt_unlocked(path)
                    return None
        except (OutboxStorageError, OSError) as error:
            raise OutboxStorageError("storage-unavailable") from error

    def inspect(self, event_id: str | None = None) -> Inspection:
        if event_id is not None:
            record = self.inspect_record(event_id)
            return Inspection(records=[] if record is None else [record], diagnostics=[])
        try:
            with _exclusive_file_lock(self._lock_path):
                self._ensure_layout()
                records, diagnostics, quarantined = self._safe_records_unlocked()
                partial_count = sum(
                    1
                    for directory in (self.queue_directory, self.prepared.directory)
                    for temporary in directory.glob(".*")
                    if temporary.name not in {".", ".."}
                )
                prepared_ids: set[str] = set()
                for prepared_path in self.prepared.directory.glob("*.json"):
                    try:
                        if prepared_path.is_symlink() or not stat.S_ISREG(prepared_path.lstat().st_mode):
                            raise ValueError("prepared record is not regular")
                        prepared_body = json.loads(prepared_path.read_text(encoding="utf-8"))
                        prepared_record = PreparedRecord.from_dict(prepared_body)
                    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                        self.prepared._quarantine_unlocked(prepared_path)
                        quarantined += 1
                        diagnostics.append(Diagnostic("prepared-record-corrupt"))
                    else:
                        prepared_ids.add(prepared_record.event_id)
                queue_ids = {record.event_id for record in records}
                return Inspection(records, diagnostics, quarantined, partial_count, sorted(prepared_ids - queue_ids))
        except (OutboxStorageError, OSError):
            return Inspection([], [Diagnostic("storage-unavailable")])

    def _now(self) -> float:
        value = self.clock()
        if not _finite_number(value) or value < 0:
            raise OutboxStorageError("clock-invalid")
        return float(value)

    def _claimable(self, record: QueueRecord) -> bool:
        return record.state in {
            QueueState.QUEUED,
            QueueState.RETRYABLE_FAILURE,
            QueueState.CAPTURE_GAP,
            QueueState.SOURCE_SNAPSHOTTED,
            QueueState.PREPARED_ENCRYPTED,
            QueueState.OBJECT_UPLOADED,
            QueueState.INDEX_PUBLISHED,
        } and record.owner is None

    def claim(self, owner: str) -> QueueRecord | None:
        owner = _identifier(owner)
        with _exclusive_file_lock(self._lock_path):
            self._ensure_layout()
            records, _diagnostics, _quarantined = self._safe_records_unlocked()
            candidate = next((record for record in records if self._claimable(record)), None)
            if candidate is None:
                return None
            resume = candidate.resume_state
            if candidate.state not in {QueueState.RETRYABLE_FAILURE, QueueState.CAPTURE_GAP}:
                resume = candidate.state
            claimed = QueueRecord(**{**candidate.__dict__, "state": QueueState.CLAIMED, "owner": owner, "lease_until": self._now() + self.lease_seconds, "lease_seconds": self.lease_seconds, "resume_state": resume, "failure_code": None})
            self._publish_record_unlocked(claimed)
            return claimed

    def _require_claim(self, record: QueueRecord, owner: str) -> None:
        owned_states = {
            QueueState.CLAIMED,
            QueueState.SOURCE_SNAPSHOTTED,
            QueueState.PREPARED_ENCRYPTED,
            QueueState.OBJECT_UPLOADED,
            QueueState.INDEX_PUBLISHED,
        }
        if record.state not in owned_states or record.owner != owner or record.lease_until is None or record.lease_until <= self._now():
            raise LeaseConflict()

    def renew(self, event_id: str, owner: str) -> QueueRecord:
        owner = _identifier(owner)
        with _exclusive_file_lock(self._lock_path):
            record = self._must_read_unlocked(event_id)
            self._require_claim(record, owner)
            renewed = QueueRecord(**{**record.__dict__, "lease_until": self._now() + record.lease_seconds})
            self._publish_record_unlocked(renewed)
            return renewed

    def takeover(self, event_id: str, owner: str) -> QueueRecord:
        owner = _identifier(owner)
        with _exclusive_file_lock(self._lock_path):
            record = self._must_read_unlocked(event_id)
            owned_states = {
                QueueState.CLAIMED,
                QueueState.SOURCE_SNAPSHOTTED,
                QueueState.PREPARED_ENCRYPTED,
                QueueState.OBJECT_UPLOADED,
                QueueState.INDEX_PUBLISHED,
            }
            if record.state not in owned_states or record.owner is None or record.lease_until is None or record.lease_until > self._now():
                raise LeaseConflict()
            taken = QueueRecord(**{**record.__dict__, "owner": owner, "lease_until": self._now() + record.lease_seconds})
            self._publish_record_unlocked(taken)
            return taken

    def _must_read_unlocked(self, event_id: str) -> QueueRecord:
        path = self._path(event_id)
        try:
            return self._read_record(path)
        except FileNotFoundError as error:
            raise OutboxError("outbox record not found") from error
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise OutboxError("outbox record is quarantined") from error

    def _next_state_valid(self, record: QueueRecord, target: QueueState) -> bool:
        if target is QueueState.SOURCE_SNAPSHOTTED:
            return record.resume_state is QueueState.QUEUED
        if target is QueueState.PREPARED_ENCRYPTED:
            return record.resume_state is QueueState.SOURCE_SNAPSHOTTED
        if target is QueueState.OBJECT_UPLOADED:
            return record.resume_state is QueueState.PREPARED_ENCRYPTED
        if target is QueueState.INDEX_PUBLISHED:
            return record.resume_state is QueueState.OBJECT_UPLOADED
        if target is QueueState.COMMITTED:
            return record.resume_state is QueueState.INDEX_PUBLISHED and record.index_id is not None
        return target in {QueueState.RETRYABLE_FAILURE, QueueState.QUARANTINED, QueueState.POLICY_DENIED, QueueState.CAPTURE_GAP}

    def transition(self, event_id: str, owner: str, state: QueueState, **details: object) -> QueueRecord:
        owner = _identifier(owner)
        try:
            state = QueueState(state)
        except (TypeError, ValueError) as error:
            raise InvalidTransition() from error
        with _exclusive_file_lock(self._lock_path):
            record = self._must_read_unlocked(event_id)
            if state in {QueueState.RETRYABLE_FAILURE, QueueState.QUARANTINED} and record.state is state:
                requested_reason = _identifier(details.get("reason_code", state.value))
                if record.failure_code == requested_reason:
                    return record
            if record.state is QueueState.COMMITTED and state is QueueState.COMMITTED:
                return record
            self._require_claim(record, owner)
            if record.state is state:
                if state is QueueState.OBJECT_UPLOADED and details.get("object_key") not in {None, record.object_key}:
                    raise InvalidTransition()
                if state is QueueState.OBJECT_UPLOADED and details.get("ciphertext_size") not in {None, record.ciphertext_size}:
                    raise InvalidTransition()
                if state is QueueState.INDEX_PUBLISHED and details.get("index_id") not in {None, record.index_id}:
                    raise InvalidTransition()
                return record
            if not self._next_state_valid(record, state):
                raise InvalidTransition()
            if state is QueueState.RETRYABLE_FAILURE:
                reason_code = details.get("reason_code", "retryable-failure")
                failure_code = _identifier(reason_code)
                updated = QueueRecord(**{**record.__dict__, "state": state, "failure_code": failure_code, "owner": None, "lease_until": None})
            elif state in {QueueState.QUARANTINED, QueueState.CAPTURE_GAP, QueueState.POLICY_DENIED}:
                updated = QueueRecord(**{**record.__dict__, "state": state, "owner": None, "lease_until": None, "failure_code": _identifier(details.get("reason_code", state.value))})
            else:
                updates: dict[str, object] = {
                    "state": state,
                    "resume_state": record.resume_state if state is QueueState.COMMITTED else state,
                    "failure_code": None,
                }
                if "object_key" in details:
                    object_key = details["object_key"]
                    if not isinstance(object_key, str) or not _OBJECT_KEY.fullmatch(object_key):
                        raise ValueError("object key is invalid")
                    object_digest = _OBJECT_KEY.fullmatch(object_key).group(1)
                    if state is QueueState.OBJECT_UPLOADED and object_digest != record.ciphertext_sha256:
                        raise InvalidTransition()
                    updates["object_key"] = object_key
                    updates["ciphertext_sha256"] = object_digest
                if "ciphertext_size" in details:
                    size = details["ciphertext_size"]
                    if type(size) is not int or not 0 <= size <= _MAX_CIPHERTEXT_BYTES:
                        raise ValueError("ciphertext size is invalid")
                    if state is QueueState.OBJECT_UPLOADED and size != record.ciphertext_size:
                        raise InvalidTransition()
                    updates["ciphertext_size"] = size
                if "index_id" in details:
                    updates["index_id"] = _identifier(details["index_id"])
                if state is QueueState.COMMITTED:
                    updates["owner"] = None
                    updates["lease_until"] = None
                updated = QueueRecord(**{**record.__dict__, **updates})
            self._publish_record_unlocked(updated)
            return updated

    def prepare_encrypted(self, event_id: str, owner: str, ciphertext: bytes, *, metadata: Mapping[str, object] | None = None) -> QueueRecord:
        if not isinstance(ciphertext, bytes) or len(ciphertext) > _MAX_CIPHERTEXT_BYTES:
            raise ValueError("ciphertext is invalid")
        event_id = _identifier(event_id)
        metadata = _validate_metadata(metadata or {})
        with _exclusive_file_lock(self._lock_path):
            record = self._must_read_unlocked(event_id)
            self._require_claim(record, _identifier(owner))
            if record.resume_state is not QueueState.SOURCE_SNAPSHOTTED:
                if record.resume_state in {QueueState.PREPARED_ENCRYPTED, QueueState.OBJECT_UPLOADED, QueueState.INDEX_PUBLISHED}:
                    return record
                raise InvalidTransition()
            prepared = PreparedRecord(event_id, ciphertext, metadata, hashlib.sha256(ciphertext).hexdigest(), len(ciphertext))
            prepared_body = json.dumps(prepared.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            current_queue_bytes = sum(
                self._path(item.event_id).stat().st_size
                for item in self._safe_records_unlocked()[0]
            )
            current_path = self._path(record.event_id)
            current_record_body = current_path.stat().st_size
            prepared_path = self.prepared._path(event_id)
            prepared_bytes = self._prepared_bytes_unlocked()
            existing_prepared_bytes = 0
            if prepared_path.exists() or prepared_path.is_symlink():
                try:
                    prepared_mode = prepared_path.lstat().st_mode
                except OSError as error:
                    raise OutboxStorageError("prepared-record-unavailable") from error
                if prepared_path.is_symlink() or not stat.S_ISREG(prepared_mode):
                    raise OutboxStorageError("prepared-record-corrupt")
                existing_prepared_bytes = prepared_path.lstat().st_size
            updated = QueueRecord(
                **{
                    **record.__dict__,
                    "state": QueueState.PREPARED_ENCRYPTED,
                    "resume_state": QueueState.PREPARED_ENCRYPTED,
                    "failure_code": None,
                    "ciphertext_sha256": prepared.ciphertext_sha256,
                    "ciphertext_size": prepared.ciphertext_size,
                }
            )
            updated_body = json.dumps(updated.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            if (
                current_queue_bytes
                - current_record_body
                + len(updated_body)
                + prepared_bytes
                - existing_prepared_bytes
                + len(prepared_body)
                > self.max_bytes
            ):
                gap = QueueRecord(
                    **{
                        **record.__dict__,
                        "state": QueueState.CAPTURE_GAP,
                        "owner": None,
                        "lease_until": None,
                        "resume_state": QueueState.SOURCE_SNAPSHOTTED,
                        "failure_code": "prepared-byte-quota",
                    }
                )
                self._publish_record_unlocked(gap)
                return gap
            self.prepared._publish_unlocked(prepared)
            self._publish_record_unlocked(updated)
            return updated

    def reconcile_prepared(
        self,
        event_id: str,
        *,
        session_id: str,
        checkpoint: Mapping[str, object],
        metadata: Mapping[str, object] | None = None,
        is_final: bool = False,
    ) -> QueueRecord:
        """Reattach a durable prepared record after a crash between publications."""

        event_id = _identifier(event_id)
        session_id = _identifier(session_id)
        checkpoint = _validate_checkpoint(checkpoint)
        requested_metadata = _validate_metadata(metadata or {})
        if type(is_final) is not bool:
            raise ValueError("final marker is invalid")
        with _exclusive_file_lock(self._lock_path):
            self._ensure_layout()
            prepared_path = self.prepared._path(event_id)
            try:
                if prepared_path.is_symlink() or not stat.S_ISREG(prepared_path.lstat().st_mode):
                    raise ValueError("prepared record is not regular")
                prepared = PreparedRecord.from_dict(json.loads(prepared_path.read_text(encoding="utf-8")))
            except FileNotFoundError as error:
                raise OutboxError("prepared record not found") from error
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                self.prepared._quarantine_unlocked(prepared_path)
                raise OutboxStorageError("prepared-record-corrupt") from error
            if prepared.event_id != event_id:
                self.prepared._quarantine_unlocked(prepared_path)
                raise OutboxStorageError("prepared-record-corrupt")
            records, _diagnostics, _quarantined = self._safe_records_unlocked()
            existing = next((item for item in records if item.event_id == event_id), None)
            delivery_metadata = _validate_metadata({**prepared.metadata, **requested_metadata})
            if existing is not None:
                stale_owner = False
                if existing.owner is not None:
                    if existing.lease_until is not None and existing.lease_until > self._now():
                        raise LeaseConflict()
                    stale_owner = True
                    existing = QueueRecord(
                        **{
                            **existing.__dict__,
                            "owner": None,
                            "lease_until": None,
                        }
                    )
                if existing.resume_state in {
                    QueueState.PREPARED_ENCRYPTED,
                    QueueState.OBJECT_UPLOADED,
                    QueueState.INDEX_PUBLISHED,
                } or existing.state is QueueState.COMMITTED:
                    if stale_owner:
                        self._publish_record_unlocked(existing)
                    return existing
                updated = QueueRecord(
                    **{
                        **existing.__dict__,
                        "state": QueueState.PREPARED_ENCRYPTED,
                        "resume_state": QueueState.PREPARED_ENCRYPTED,
                        "metadata": delivery_metadata,
                        "is_final": existing.is_final or is_final,
                        "final_event_id": existing.final_event_id or (event_id if is_final else None),
                        "ciphertext_sha256": prepared.ciphertext_sha256,
                        "ciphertext_size": prepared.ciphertext_size,
                    }
                )
                self._publish_record_unlocked(updated)
                return updated
            if len(records) >= self.max_events:
                raise OutboxStorageError("queue-full")
            sequence = max((item.sequence for item in records), default=0) + 1
            recovered = QueueRecord(
                event_id=event_id,
                session_id=session_id,
                checkpoint=checkpoint,
                metadata=delivery_metadata,
                state=QueueState.PREPARED_ENCRYPTED,
                sequence=sequence,
                event_ids=(event_id,),
                is_final=is_final,
                final_event_id=event_id if is_final else None,
                resume_state=QueueState.PREPARED_ENCRYPTED,
                ciphertext_sha256=prepared.ciphertext_sha256,
                ciphertext_size=prepared.ciphertext_size,
            )
            current_bytes = sum(self._path(item.event_id).stat().st_size for item in records)
            encoded = json.dumps(recovered.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            if current_bytes + len(encoded) + self._prepared_bytes_unlocked() > self.max_bytes:
                raise OutboxStorageError("queue-full")
            self._publish_record_unlocked(recovered)
            return recovered

    def mark_uploaded(self, event_id: str, owner: str, *, object_key: str, ciphertext_size: int | None = None) -> QueueRecord:
        details: dict[str, object] = {"object_key": object_key}
        if ciphertext_size is not None:
            details["ciphertext_size"] = ciphertext_size
        return self.transition(event_id, owner, QueueState.OBJECT_UPLOADED, **details)

    def publish_index(self, event_id: str, owner: str, *, index_id: str) -> QueueRecord:
        return self.transition(event_id, owner, QueueState.INDEX_PUBLISHED, index_id=index_id)

    def retry(self, event_id: str, owner: str, *, reason_code: str = "retryable-failure") -> QueueRecord:
        return self.transition(event_id, owner, QueueState.RETRYABLE_FAILURE, reason_code=reason_code)

    def quarantine(self, event_id: str, owner: str, *, reason_code: str = "quarantined") -> QueueRecord:
        return self.transition(event_id, owner, QueueState.QUARANTINED, reason_code=reason_code)

    def commit(self, event_id: str, owner: str) -> QueueRecord:
        return self.transition(event_id, owner, QueueState.COMMITTED)
