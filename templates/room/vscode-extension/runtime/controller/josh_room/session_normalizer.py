"""Bounded incremental normalization for the PCC session evidence boundary.

The normalizer consumes an already-open :class:`BoundedRecordStream` from the
Codex adapter.  It does not discover files, read the trigger queue, encrypt,
upload, or create durable plaintext.  Asset bytes are handed to an injected
ephemeral writer one bounded chunk at a time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .adapter_contract import (
    AdapterError,
    BoundedRecordStream,
    CancellationToken,
    Checkpoint,
    SourceRecord,
)
from .material_security import Decision as MaterialDecision
from .material_security import MaterialClass, scan_content
from .session_evidence import (
    ValidationDisposition,
    canonical_digest,
    canonical_json,
    validate_document,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SAFE_RECORD_KINDS = frozenset({
    "message",
    "session_meta",
    "tool_call",
    "tool_result",
    "usage",
    "approval",
    "repository",
    "asset",
    "asset_payload",
})
_FORBIDDEN_MARKERS = frozenset({
    "auth",
    "auth_refresh",
    "credential",
    "credentials",
    "keyring",
    "secret",
    "reasoning",
    "encrypted_reasoning",
    "hidden_reasoning",
    "internal_state",
})
_FORBIDDEN_KEYS = frozenset({
    "access_key",
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "id_token",
    "keyring",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "secret_key",
    "token",
})
_MEDIA_TYPES = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/gif": "image",
    "image/webp": "image",
    "audio/mpeg": "audio",
    "audio/wav": "audio",
    "video/mp4": "video",
    "application/pdf": "document",
    "application/zip": "archive",
    "application/gzip": "archive",
}


class NormalizationErrorCode(StrEnum):
    CANCELLED = "cancelled"
    INVALID_CONTEXT = "invalid-context"
    INVALID_RECORD = "invalid-record"
    RECORD_OVERSIZE = "record-oversize"
    SOURCE_CURSOR_INCONSISTENT = "source-cursor-inconsistent"
    SOURCE_LIMIT = "source-limit"
    SEGMENT_LIMIT = "segment-limit"
    ASSET_LIMIT = "asset-limit"
    ASSET_INVALID = "asset-invalid"
    ASSET_UNSUPPORTED = "asset-unsupported"
    ASSET_INCOMPLETE = "asset-incomplete"
    UNKNOWN_KIND = "unknown-kind"
    FORBIDDEN_MATERIAL = "forbidden-material"
    WRITER_FAILURE = "writer-failure"
    POLICY_DENIED = "policy-denied"
    WORKING_LIMIT = "working-limit"


class NormalizationError(RuntimeError):
    """Public-safe error containing only a stable reason code."""

    def __init__(self, code: NormalizationErrorCode):
        self.code = NormalizationErrorCode(code)
        super().__init__(self.code.value)


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


def _identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
    return value


def _safe_source(value: Mapping[str, object]) -> dict[str, object]:
    allowed = {"surface", "adapter", "adapter_version", "codex_version"}
    if (
        set(value) - allowed
        or not isinstance(value.get("surface"), str)
        or not isinstance(value.get("adapter"), str)
        or not isinstance(value.get("adapter_version"), str)
    ):
        raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
    result = dict(value)
    for key in allowed:
        if key in result:
            _identifier(result[key], field=key)
    if result["surface"] not in {"cli", "desktop", "vscode", "app-server", "import", "unknown"}:
        raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
    return result


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
    value = _ANSI.sub("", value)
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = "".join(char for char in value if char in {"\t", "\n"} or unicodedata.category(char) not in {"Cc", "Cf"})
    return value


def _safe_scalar(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
        return value
    if isinstance(value, str):
        return _safe_text(value)
    raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)


def _safe_metadata(value: object, *, depth: int = 0) -> object:
    if depth > 3:
        raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = key.lower().replace("-", "_") if isinstance(key, str) else ""
            if (
                not isinstance(key, str)
                or len(key) > 64
                or normalized_key in _FORBIDDEN_MARKERS
                or normalized_key in _FORBIDDEN_KEYS
            ):
                raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
            result[key] = _safe_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 128:
            raise NormalizationError(NormalizationErrorCode.RECORD_OVERSIZE)
        return [_safe_metadata(item, depth=depth + 1) for item in value]
    return _safe_scalar(value)


@dataclass(frozen=True, slots=True)
class NormalizationLimits:
    max_source_bytes: int = 8 * 1024 * 1024
    max_record_bytes: int = 512 * 1024
    max_segment_bytes: int = 256 * 1024
    max_segment_records: int = 128
    max_asset_bytes: int = 8 * 1024 * 1024
    max_assets_per_record: int = 8
    max_assets_per_segment: int = 32
    max_assets_per_session: int = 256
    max_session_bytes: int = 64 * 1024 * 1024
    max_working_bytes: int = 2 * 1024 * 1024
    max_dedupe_entries: int = 256

    def __post_init__(self) -> None:
        fields = (
            self.max_source_bytes,
            self.max_record_bytes,
            self.max_segment_bytes,
            self.max_segment_records,
            self.max_asset_bytes,
            self.max_assets_per_record,
            self.max_assets_per_segment,
            self.max_assets_per_session,
            self.max_session_bytes,
            self.max_working_bytes,
            self.max_dedupe_entries,
        )
        if (
            any(not _positive(item) for item in fields)
            or self.max_asset_bytes > self.max_session_bytes
            or self.max_assets_per_segment > self.max_assets_per_session
        ):
            raise ValueError("normalization limits are invalid")


@dataclass(frozen=True, slots=True)
class NormalizationContext:
    session_id: str
    profile_id: str
    workspace_id: str
    device_id: str
    source: Mapping[str, object]
    policy_decision: str = "allow"
    sensitivity: str = "unknown"

    def __post_init__(self) -> None:
        for value, field in (
            (self.session_id, "session_id"),
            (self.profile_id, "profile_id"),
            (self.workspace_id, "workspace_id"),
            (self.device_id, "device_id"),
        ):
            _identifier(value, field=field)
        if not isinstance(self.source, Mapping):
            raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
        object.__setattr__(self, "source", _safe_source(self.source))
        if self.policy_decision not in {"allow", "deny", "local-only", "quarantine"}:
            raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
        if self.sensitivity not in {"normal", "sensitive", "unknown"}:
            raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)


@dataclass(frozen=True, slots=True)
class AssetReceipt:
    asset_id: str
    sha256: str
    size: int
    media_category: str
    content_type: str

    def __post_init__(self) -> None:
        _identifier(self.asset_id, field="asset_id")
        _digest(self.sha256)
        if not _nonnegative(self.size) or self.media_category not in set(_MEDIA_TYPES.values()):
            raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE)
        if self.content_type not in _MEDIA_TYPES:
            raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE)


class AssetSink(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def commit(self) -> AssetReceipt: ...

    def abort(self) -> None: ...


class AssetWriter(Protocol):
    def open(self, asset_id: str, content_type: str, media_category: str) -> AssetSink: ...


@dataclass(frozen=True, slots=True)
class NormalizationEvent:
    kind: str
    document: Mapping[str, object]
    asset_receipt: AssetReceipt | None = None


@dataclass(frozen=True, slots=True)
class NormalizationMetrics:
    source_bytes: int = 0
    normalized_bytes: int = 0
    externalized_bytes: int = 0
    deduped_bytes: int = 0
    records_seen: int = 0
    records_emitted: int = 0
    records_quarantined: int = 0
    assets_externalized: int = 0
    assets_deduped: int = 0
    gaps: int = 0
    peak_working_bytes: int = 0


@dataclass(frozen=True, slots=True)
class NormalizationReceipt:
    status: str
    reason_codes: tuple[str, ...]
    metrics: NormalizationMetrics
    next_checkpoint: Checkpoint
    last_segment_sha256: str | None


@dataclass(slots=True)
class _AssetState:
    asset_id: str
    content_type: str
    media_category: str
    chunk_index: int
    tail: str
    sink: AssetSink
    digest: Any
    size: int = 0


def _checkpoint_document(start: Checkpoint, end: Checkpoint) -> dict[str, object]:
    if start.source_id != end.source_id or start.session_id != end.session_id:
        raise NormalizationError(NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT)
    return {
        "source": end.source_id,
        "representation": end.representation,
        "start": start.next_byte_offset,
        "end": end.next_byte_offset,
        "prefix_sha256": end.prefix_digest,
    }


def _digest_event(prefix: str, value: Mapping[str, object]) -> str:
    return f"{prefix}-{canonical_digest(value)[:48]}"


class SessionNormalizer:
    """Consume one #6 stream and yield bounded v1 evidence events."""

    def __init__(
        self,
        stream: BoundedRecordStream,
        context: NormalizationContext,
        *,
        prior_checkpoint: Checkpoint | None = None,
        previous_segment_sha256: str | None = None,
        finalize: bool = False,
        recovered: bool = False,
        limits: NormalizationLimits | None = None,
        asset_writer: AssetWriter | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if not isinstance(stream, BoundedRecordStream):
            raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
        if prior_checkpoint is not None and not isinstance(prior_checkpoint, Checkpoint):
            raise NormalizationError(NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT)
        if previous_segment_sha256 is not None:
            _digest(previous_segment_sha256)
        self.stream = stream
        self.context = context
        self.prior_checkpoint = prior_checkpoint
        self.previous_segment_sha256 = previous_segment_sha256
        self.finalize = bool(finalize)
        self.recovered = bool(recovered)
        self.limits = limits or NormalizationLimits()
        self.asset_writer = asset_writer
        self.cancellation = cancellation or CancellationToken()
        self._started = False
        self._closed = False
        self._active_asset: _AssetState | None = None
        self._dedupe: dict[str, dict[str, object]] = {}
        self._segment_records: list[dict[str, object]] = []
        self._segment_record_bytes = 2
        self._segment_start: Checkpoint | None = None
        self._segment_assets: dict[str, dict[str, object]] = {}
        self._pending_asset_events: list[NormalizationEvent] = []
        self._segment_gaps = 0
        self._segment_quarantined = 0
        self._record_asset_count = 0
        self._session_assets = 0
        self._session_bytes_seen = 0
        self._metrics = NormalizationMetrics()
        self._reasons: list[str] = []
        self._last_checkpoint = stream.result.next_checkpoint
        self._last_segment_sha256 = previous_segment_sha256

    @property
    def metrics(self) -> NormalizationMetrics:
        return self._metrics

    @property
    def receipt(self) -> NormalizationReceipt | None:
        if not self._started:
            return None
        status = "complete" if self.finalize and not self._reasons else ("quarantined" if self._reasons else "partial")
        if self.recovered and not self._reasons:
            status = "recovered"
        return NormalizationReceipt(status, tuple(self._reasons), self._metrics, self._last_checkpoint, self._last_segment_sha256)

    def normalize(self) -> Iterator[NormalizationEvent]:
        if self._started or self._closed:
            raise NormalizationError(NormalizationErrorCode.INVALID_CONTEXT)
        self._started = True
        initial = self.stream.result.next_checkpoint
        if self.prior_checkpoint is not None and initial != self.prior_checkpoint:
            raise NormalizationError(NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT)
        self._segment_start = initial
        expected_index = initial.next_record_index
        try:
            while True:
                self._check_cancelled()
                if self.context.policy_decision in {"deny", "quarantine"}:
                    self._record_reason(NormalizationErrorCode.POLICY_DENIED.value)
                    raise NormalizationError(NormalizationErrorCode.POLICY_DENIED)
                record_start = self._last_checkpoint
                try:
                    record = next(self.stream)
                except StopIteration:
                    break
                except AdapterError:
                    self._record_reason("source-stream-failure")
                    yield from self._flush_segment(status="quarantined")
                    return
                except Exception:  # noqa: BLE001 - source details are not public output
                    self._record_reason("source-stream-failure")
                    yield from self._flush_segment(status="quarantined")
                    return
                record_end = self.stream.result.next_checkpoint
                self._last_checkpoint = record_end
                self._record_asset_count = 0
                self._metrics = self._replace_metrics(records_seen=self._metrics.records_seen + 1)
                self._observe_working(len(record.content))
                self._session_bytes_seen += len(record.content)
                self._metrics = self._replace_metrics(source_bytes=self._metrics.source_bytes + len(record.content))
                if record.record_index != expected_index:
                    self._record_reason(NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT.value)
                    self._append_gap(record, NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT.value, record_start, record_end)
                    yield from self._flush_segment(status="quarantined")
                    return
                expected_index += 1
                if self._session_bytes_seen > self.limits.max_session_bytes or self._metrics.source_bytes > self.limits.max_source_bytes:
                    self._record_reason(NormalizationErrorCode.SOURCE_LIMIT.value)
                    self._append_gap(record, NormalizationErrorCode.SOURCE_LIMIT.value, record_start, record_end)
                    yield from self._flush_segment(status="quarantined")
                    return
                if len(record.content) > self.limits.max_record_bytes:
                    self._record_reason(NormalizationErrorCode.RECORD_OVERSIZE.value)
                    self._append_gap(record, NormalizationErrorCode.RECORD_OVERSIZE.value, record_start, record_end)
                    self._segment_quarantined += 1
                    continue
                if len(record.content) > self.limits.max_working_bytes:
                    self._record_reason(NormalizationErrorCode.WORKING_LIMIT.value)
                    self._append_gap(record, NormalizationErrorCode.WORKING_LIMIT.value, record_start, record_end)
                    self._segment_quarantined += 1
                    continue
                try:
                    normalized = self._normalize_record(record)
                except NormalizationError as error:
                    self._record_reason(error.code.value)
                    self._append_gap(record, error.code.value, record_start, record_end)
                    self._metrics = self._replace_metrics(records_quarantined=self._metrics.records_quarantined + 1)
                    self._segment_quarantined += 1
                    if error.code is NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT:
                        yield from self._flush_segment(status="quarantined")
                        return
                    continue
                if normalized is not None:
                    asset_event = normalized.pop("_asset_event", None)
                    if asset_event is not None:
                        self._pending_asset_events.append(asset_event)
                    if normalized.get("_emit") is not False:
                        if not self._fits(normalized):
                            if self._segment_records:
                                yield from self._flush_segment(status="partial")
                            if not self._fits(normalized):
                                self._record_reason(NormalizationErrorCode.SEGMENT_LIMIT.value)
                                self._append_gap(record, NormalizationErrorCode.SEGMENT_LIMIT.value, record_start, record_end)
                                self._segment_quarantined += 1
                                continue
                        self._append_record(normalized)
                if self._segment_records and (
                    len(self._segment_records) >= self.limits.max_segment_records
                    or self._segment_record_bytes > self.limits.max_segment_bytes
                ):
                    yield from self._flush_segment(status="partial")
            if self._active_asset is not None:
                self._abort_asset()
                self._record_reason(NormalizationErrorCode.ASSET_INCOMPLETE.value)
                self._append_gap(None, NormalizationErrorCode.ASSET_INCOMPLETE.value)
            status = (
                "recovered"
                if self.recovered and not self._reasons
                else "quarantined"
                if self._reasons
                else "complete"
                if self.finalize
                else "partial"
            )
            yield from self._flush_segment(status=status)
            if self.finalize:
                yield self._final_event()
        except GeneratorExit:
            self.close()
            raise
        except NormalizationError:
            self.close()
            raise
        finally:
            if self._active_asset is not None:
                self._abort_asset()
            self._closed = True

    def __iter__(self) -> Iterator[NormalizationEvent]:
        return self.normalize()

    def close(self) -> None:
        if self._active_asset is not None:
            self._abort_asset()
        self._closed = True

    def _check_cancelled(self) -> None:
        if self.cancellation.cancelled:
            self._record_reason(NormalizationErrorCode.CANCELLED.value)
            self.close()
            raise NormalizationError(NormalizationErrorCode.CANCELLED)

    def _replace_metrics(self, **changes: int) -> NormalizationMetrics:
        values = {
            "source_bytes": self._metrics.source_bytes,
            "normalized_bytes": self._metrics.normalized_bytes,
            "externalized_bytes": self._metrics.externalized_bytes,
            "deduped_bytes": self._metrics.deduped_bytes,
            "records_seen": self._metrics.records_seen,
            "records_emitted": self._metrics.records_emitted,
            "records_quarantined": self._metrics.records_quarantined,
            "assets_externalized": self._metrics.assets_externalized,
            "assets_deduped": self._metrics.assets_deduped,
            "gaps": self._metrics.gaps,
            "peak_working_bytes": self._metrics.peak_working_bytes,
        }
        values.update(changes)
        values["peak_working_bytes"] = max(values["peak_working_bytes"], self._segment_record_bytes)
        return NormalizationMetrics(**values)

    def _observe_working(self, size: int) -> None:
        self._metrics = self._replace_metrics(peak_working_bytes=max(self._metrics.peak_working_bytes, size))

    def _record_reason(self, reason: str) -> None:
        if reason not in self._reasons and len(self._reasons) < 32:
            self._reasons.append(reason)

    def _parse(self, record: SourceRecord) -> dict[str, object]:
        try:
            value = json.loads(record.content)
        except (UnicodeDecodeError, TypeError, ValueError):
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD) from None
        if not isinstance(value, dict):
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
        return value

    def _normalize_record(self, record: SourceRecord) -> dict[str, object] | None:
        if record.session_id != self.context.session_id or record.logical_source.value != "codex.transcript":
            raise NormalizationError(NormalizationErrorCode.SOURCE_CURSOR_INCONSISTENT)
        if record.material_class not in {"transcript", "metadata", "asset"}:
            raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
        safety = scan_content(
            (record.content,),
            adapter="codex-transcript",
            material_class=(
                MaterialClass.SESSION_ASSET
                if record.material_class == "asset"
                else MaterialClass.SESSION_METADATA
                if record.material_class == "metadata"
                else MaterialClass.SESSION_TRANSCRIPT
            ),
            max_bytes=self.limits.max_record_bytes,
        )
        if safety.decision is not MaterialDecision.ALLOW:
            raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
        value = self._parse(record)
        kind = value.get("record_kind")
        if not isinstance(kind, str):
            raise NormalizationError(NormalizationErrorCode.UNKNOWN_KIND)
        if kind.lower() in _FORBIDDEN_MARKERS or any(marker in kind.lower() for marker in ("reason", "credential", "auth", "secret")):
            raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
        if kind not in _SAFE_RECORD_KINDS:
            raise NormalizationError(NormalizationErrorCode.UNKNOWN_KIND)
        if self._active_asset is not None and kind != "asset_payload":
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.ASSET_INCOMPLETE)
        if kind == "asset_payload":
            return self._consume_asset(value)
        if kind == "asset":
            if value.get("decision") != "deferred":
                raise NormalizationError(NormalizationErrorCode.ASSET_UNSUPPORTED)
            digest = _digest(value.get("sha256"))
            size = value.get("bytes")
            if not _nonnegative(size):
                raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
            return {"record_kind": "asset-deferred", "bytes": size, "sha256": digest, "decision": "deferred"}
        if kind == "message":
            role = value.get("role")
            if role not in {"user", "assistant"}:
                raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
            return {"record_kind": "message", "role": role, "text": _safe_text(value.get("text"))}
        if kind == "session_meta":
            result = {"record_kind": kind}
            for key in ("session_id", "surface", "subagent_type"):
                if key in value:
                    result[key] = _safe_scalar(value[key])
            return result
        if kind == "repository":
            remote = value.get("remote")
            if (
                not isinstance(remote, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._/-]+", remote)
                or any(marker in remote for marker in ("@", "://", "?", "#"))
            ):
                raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
            result = {"record_kind": kind}
            for key in ("remote", "commit", "branch", "dirty"):
                if key in value:
                    result[key] = _safe_metadata(value[key])
            return result
        if kind in {"tool_call", "tool_result", "usage", "approval"}:
            result = {"record_kind": kind}
            for key, item in value.items():
                if key == "record_kind":
                    continue
                normalized_key = key.lower().replace("-", "_")
                if normalized_key in _FORBIDDEN_MARKERS or normalized_key in _FORBIDDEN_KEYS:
                    raise NormalizationError(NormalizationErrorCode.FORBIDDEN_MATERIAL)
                result[key] = _safe_metadata(item)
            return result
        raise NormalizationError(NormalizationErrorCode.UNKNOWN_KIND)

    def _consume_asset(self, value: Mapping[str, object]) -> dict[str, object] | None:
        required = {"record_kind", "asset_id", "chunk", "chunk_index", "final", "content_type"}
        if set(value) - required - {"media_category"} or not required <= set(value):
            raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
        asset_id = _identifier(value["asset_id"], field="asset_id")
        content_type = value["content_type"]
        if not isinstance(content_type, str) or content_type not in _MEDIA_TYPES:
            raise NormalizationError(NormalizationErrorCode.ASSET_UNSUPPORTED)
        category = value.get("media_category", _MEDIA_TYPES[content_type])
        if category != _MEDIA_TYPES[content_type]:
            raise NormalizationError(NormalizationErrorCode.ASSET_UNSUPPORTED)
        chunk = value["chunk"]
        index = value["chunk_index"]
        final = value["final"]
        if not isinstance(chunk, str) or len(chunk.encode("ascii", errors="ignore")) != len(chunk) or not _nonnegative(index) or type(final) is not bool:
            raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
        if len(chunk) > self.limits.max_record_bytes * 2:
            raise NormalizationError(NormalizationErrorCode.RECORD_OVERSIZE)
        if self._active_asset is None:
            if index != 0 or self.asset_writer is None:
                raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
            if self._record_asset_count >= self.limits.max_assets_per_record:
                raise NormalizationError(NormalizationErrorCode.ASSET_LIMIT)
            if (
                len(self._segment_assets) >= self.limits.max_assets_per_segment
                or self._session_assets >= self.limits.max_assets_per_session
                or len(self._dedupe) >= self.limits.max_dedupe_entries
            ):
                raise NormalizationError(NormalizationErrorCode.ASSET_LIMIT)
            try:
                sink = self.asset_writer.open(asset_id, content_type, category)
            except Exception:  # noqa: BLE001 - writer failures are public-safe
                raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE) from None
            self._active_asset = _AssetState(asset_id, content_type, category, 0, "", sink, hashlib.sha256())
            self._record_asset_count += 1
        state = self._active_asset
        if state.asset_id != asset_id or state.content_type != content_type or state.chunk_index != index:
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
        combined = state.tail + chunk
        usable = len(combined) - (len(combined) % 4)
        if final:
            usable = len(combined)
            if len(combined) % 4:
                self._abort_asset()
                raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
        encoded = combined[:usable]
        self._observe_working(len(combined))
        try:
            decoded = base64.b64decode(encoded.encode("ascii"), validate=True) if encoded else b""
        except (ValueError, binascii.Error):
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.ASSET_INVALID) from None
        if state.size + len(decoded) > self.limits.max_asset_bytes or self._session_bytes_seen + state.size + len(decoded) > self.limits.max_session_bytes:
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.ASSET_LIMIT)
        try:
            state.sink.write(decoded)
        except Exception:  # noqa: BLE001 - writer failures are public-safe
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE) from None
        state.digest.update(decoded)
        state.size += len(decoded)
        state.tail = combined[usable:]
        state.chunk_index += 1
        if not final:
            return {"_emit": False}
        if state.tail:
            self._abort_asset()
            raise NormalizationError(NormalizationErrorCode.ASSET_INVALID)
        digest = state.digest.hexdigest()
        ref = {
            "asset_id": "asset-" + canonical_digest({
                "session_id": self.context.session_id,
                "source_id": self._last_checkpoint.source_id,
                "source_asset_id": state.asset_id,
                "sha256": digest,
            })[:32],
            "sha256": digest,
            "size": state.size,
            "media_category": state.media_category,
        }
        duplicate = self._dedupe.get(digest)
        if duplicate is not None:
            try:
                state.sink.abort()
            except Exception as error:  # noqa: BLE001 - cleanup must not leak details
                del error
            self._active_asset = None
            self._metrics = self._replace_metrics(
                deduped_bytes=self._metrics.deduped_bytes + state.size,
                assets_deduped=self._metrics.assets_deduped + 1,
            )
            ref = duplicate
            asset_event = None
        else:
            if len(self._dedupe) >= self.limits.max_dedupe_entries:
                self._abort_asset()
                self._record_reason(NormalizationErrorCode.ASSET_LIMIT.value)
                raise NormalizationError(NormalizationErrorCode.ASSET_LIMIT)
            document = self._asset_document(ref, state.content_type)
            try:
                receipt = state.sink.commit()
            except Exception:  # noqa: BLE001 - writer failures are public-safe
                self._abort_asset()
                raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE) from None
            try:
                valid_receipt = (
                    isinstance(receipt, AssetReceipt)
                    and receipt.asset_id == state.asset_id
                    and receipt.sha256 == digest
                    and receipt.size == state.size
                    and receipt.content_type == state.content_type
                    and receipt.media_category == state.media_category
                )
            except Exception:  # noqa: BLE001 - malformed writer output is public-safe
                valid_receipt = False
            if not valid_receipt:
                self._abort_asset()
                raise NormalizationError(NormalizationErrorCode.WRITER_FAILURE)
            self._active_asset = None
            self._dedupe[digest] = ref
            self._metrics = self._replace_metrics(
                externalized_bytes=self._metrics.externalized_bytes + state.size,
                assets_externalized=self._metrics.assets_externalized + 1,
            )
            asset_event = NormalizationEvent("session-asset", document, receipt)
        self._segment_assets[ref["asset_id"]] = ref
        self._session_assets += 1 if duplicate is None else 0
        result = {"record_kind": "asset_ref", **ref}
        if asset_event is not None:
            result["_asset_event"] = asset_event
        return result

    def _asset_document(self, ref: Mapping[str, object], content_type: str) -> dict[str, object]:
        identity = {
            "session_id": self.context.session_id,
            "asset_id": ref["asset_id"],
            "sha256": ref["sha256"],
            "size": ref["size"],
            "content_type": content_type,
        }
        document: dict[str, object] = {
            "schema_name": "codex-session-evidence",
            "schema_version": {"major": 1, "minor": 0},
            "kind": "session-asset",
            "event_id": _digest_event("asset", identity),
            "session_id": self.context.session_id,
            "profile_id": self.context.profile_id,
            "workspace_id": self.context.workspace_id,
            "device_id": self.context.device_id,
            "asset_id": ref["asset_id"],
            "source_event_id": _digest_event("source", {"session_id": self.context.session_id, "source": self._last_checkpoint.source_id}),
            "sha256": ref["sha256"],
            "size": ref["size"],
            "media_category": ref["media_category"],
            "content_type": content_type,
            "capture": {"status": "complete", "policy_decision": self.context.policy_decision, "sensitivity": self.context.sensitivity, "counters": {}},
        }
        if validate_document(document).disposition is not ValidationDisposition.ACCEPTED:
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
        return document

    def _abort_asset(self) -> None:
        state = self._active_asset
        self._active_asset = None
        if state is not None:
            try:
                state.sink.abort()
            except Exception as error:  # noqa: BLE001 - cleanup must not leak details
                del error

    def _fits(self, record: Mapping[str, object]) -> bool:
        candidate = len(canonical_json(record))
        proposed = self._segment_record_bytes + candidate + (1 if self._segment_records else 0)
        return proposed <= self.limits.max_segment_bytes

    def _append_record(self, record: dict[str, object]) -> None:
        record.pop("_emit", None)
        record.pop("_asset_event", None)
        self._segment_records.append(record)
        self._segment_record_bytes += len(canonical_json(record)) + (1 if len(self._segment_records) > 1 else 0)
        self._metrics = self._replace_metrics(
            normalized_bytes=self._metrics.normalized_bytes + len(canonical_json(record)),
            records_emitted=self._metrics.records_emitted + 1,
        )

    def _append_gap(
        self,
        record: SourceRecord | None,
        reason: str,
        start: Checkpoint | None = None,
        end: Checkpoint | None = None,
    ) -> None:
        checkpoint = end or self.stream.result.next_checkpoint
        start_checkpoint = start or checkpoint
        gap = {
            "record_kind": "capture_gap",
            "reason_code": reason,
            "source": checkpoint.source_id,
            "representation": checkpoint.representation,
            "record_index": record.record_index if record is not None else checkpoint.next_record_index,
            "checkpoint": {
                "start": start_checkpoint.next_byte_offset,
                "end": checkpoint.next_byte_offset,
                "prefix_sha256": checkpoint.prefix_digest,
            },
        }
        if self._fits(gap):
            self._append_record(gap)
        else:
            self._record_reason(NormalizationErrorCode.SEGMENT_LIMIT.value)
        self._metrics = self._replace_metrics(gaps=self._metrics.gaps + 1)
        self._segment_gaps += 1

    def _flush_segment(self, *, status: str) -> Iterator[NormalizationEvent]:
        if not self._segment_records or self._segment_start is None:
            return
        end = self._last_checkpoint
        checkpoint = _checkpoint_document(self._segment_start, end)
        records = list(self._segment_records)
        content = canonical_json(records)
        identity = {
            "session_id": self.context.session_id,
            "source": checkpoint,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "previous_segment_sha256": self._last_segment_sha256,
        }
        event_id = _digest_event("segment", identity)
        document: dict[str, object] = {
            "schema_name": "codex-session-evidence",
            "schema_version": {"major": 1, "minor": 0},
            "kind": "session-segment",
            "event_id": event_id,
            "session_id": self.context.session_id,
            "source": dict(self.context.source),
            "profile_id": self.context.profile_id,
            "workspace_id": self.context.workspace_id,
            "device_id": self.context.device_id,
            "checkpoint": checkpoint,
            "previous_segment_sha256": self._last_segment_sha256,
            "record_count": len(records),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "content_size": len(content),
            "records": records,
            "asset_refs": sorted(self._segment_assets.values(), key=lambda value: str(value["asset_id"])),
            "capture": {
                "status": "quarantined" if status == "partial" and (self._segment_gaps or self._segment_quarantined) else status,
                "policy_decision": self.context.policy_decision,
                "sensitivity": self.context.sensitivity,
                "counters": {"gaps": self._segment_gaps, "quarantined": self._segment_quarantined},
            },
        }
        if document["previous_segment_sha256"] is None:
            document.pop("previous_segment_sha256")
        validation = validate_document(document)
        if validation.disposition is not ValidationDisposition.ACCEPTED:
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
        segment_digest = canonical_digest(document)
        self._last_segment_sha256 = segment_digest
        self._segment_start = end
        self._segment_records = []
        self._segment_record_bytes = 2
        self._segment_assets = {}
        self._segment_gaps = 0
        self._segment_quarantined = 0
        assets = list(self._pending_asset_events)
        self._pending_asset_events = []
        yield from sorted(assets, key=lambda event: str(event.document["asset_id"]))
        yield NormalizationEvent("session-segment", document)

    def _final_event(self) -> NormalizationEvent:
        checkpoint = self._last_checkpoint
        final_status = "quarantined" if self._reasons else ("recovered" if self.recovered else "complete")
        identity = {"session_id": self.context.session_id, "checkpoint": checkpoint.to_dict(), "last_segment": self._last_segment_sha256, "status": final_status}
        document: dict[str, object] = {
            "schema_name": "codex-session-evidence",
            "schema_version": {"major": 1, "minor": 0},
            "kind": "session-final",
            "event_id": _digest_event("final", identity),
            "session_id": self.context.session_id,
            "source": dict(self.context.source),
            "profile_id": self.context.profile_id,
            "workspace_id": self.context.workspace_id,
            "device_id": self.context.device_id,
            "checkpoint": {"source": checkpoint.source_id, "representation": checkpoint.representation, "start": checkpoint.next_byte_offset, "end": checkpoint.next_byte_offset, "prefix_sha256": checkpoint.prefix_digest},
            "record_count": 0,
            "content_sha256": hashlib.sha256(canonical_json([])).hexdigest(),
            "content_size": len(canonical_json([])),
            "capture": {"status": final_status, "policy_decision": self.context.policy_decision, "sensitivity": self.context.sensitivity, "counters": {"gaps": self._metrics.gaps}},
        }
        if self._last_segment_sha256 is not None:
            document["last_segment_sha256"] = self._last_segment_sha256
        validation = validate_document(document)
        if validation.disposition is not ValidationDisposition.ACCEPTED:
            raise NormalizationError(NormalizationErrorCode.INVALID_RECORD)
        return NormalizationEvent("session-final", document)


__all__ = [
    "AssetReceipt",
    "AssetSink",
    "AssetWriter",
    "NormalizationContext",
    "NormalizationError",
    "NormalizationErrorCode",
    "NormalizationEvent",
    "NormalizationLimits",
    "NormalizationMetrics",
    "NormalizationReceipt",
    "SessionNormalizer",
]
