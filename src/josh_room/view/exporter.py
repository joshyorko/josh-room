"""Single-file offline HTML view snapshot exporter with embedded room-view-v1 SQLite."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .contract import (
    ALLOWED_EMBED_MEDIA_TYPES,
    Attachment,
    ViewDescriptor,
    ViewState,
    ViewStateProvider,
    ViewValidationError,
    validate_attachment_name,
    validate_safe_label,
)
from .sanitizer import (
    DEFAULT_SANITIZATION_POLICY,
    SanitizationPolicy,
    SensitivityAssessment,
    resolve_sensitivity,
    sanitize_descriptor,
    sanitize_state,
)

SCHEMA_VERSION = "room-view-v1"
DEFAULT_MAX_SIZE_BYTES = 50 * 1024 * 1024
TEMPLATE_PATH = Path(__file__).resolve().parent / "assets" / "room-view-template.html"

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(b|kb|kib|mb|mib|gb|gib)?\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "kib": 1024,
    "mb": 1000 * 1000,
    "mib": 1024 * 1024,
    "gb": 1000 * 1000 * 1000,
    "gib": 1024 * 1024 * 1024,
}
_FORBIDDEN_EXTERNAL_URL_RE = re.compile(
    r"https?://(?:cdn\.|unpkg\.com|esm\.sh|cdnjs\.|fonts\.googleapis\.com|fonts\.gstatic\.com|jsdelivr\.net)",
    re.IGNORECASE,
)

SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE groups(id TEXT PRIMARY KEY, name TEXT, ord INT);
CREATE TABLE stages(id TEXT PRIMARY KEY, name TEXT, ord INT, terminal INT, failure INT);
CREATE TABLE items(id TEXT PRIMARY KEY, group_id TEXT, stage_id TEXT, kind TEXT, label TEXT, metric REAL, updated_at TEXT, fields_json TEXT);
CREATE TABLE steps(item_id TEXT, name TEXT, at TEXT);
CREATE TABLE attachments(item_id TEXT, name TEXT, media_type TEXT, size INT, sha256 TEXT, bytes BLOB, PRIMARY KEY(item_id, name));
CREATE TABLE attachment_blobs(sha256 TEXT PRIMARY KEY, bytes BLOB);
CREATE TABLE summary(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_items_group_stage ON items(group_id, stage_id);
CREATE INDEX idx_items_kind ON items(kind);
CREATE INDEX idx_steps_item ON steps(item_id);
CREATE INDEX idx_attachments_sha ON attachments(sha256);
"""


class SensitivityAckRequiredError(ValueError):
    """Raised when exporting sensitive data without explicit acknowledgment."""

    def __init__(self, assessment: SensitivityAssessment, preview: dict[str, object]):
        self.assessment = assessment
        self.preview = preview
        self.result = {
            "ok": False,
            "error_code": "sensitivity-acknowledgment-required",
            "requires_sensitivity_ack": True,
            "sensitivity": assessment.to_dict(),
            "preview": preview,
        }
        super().__init__(
            f"Export refused: data is marked {assessment.level!r} "
            f"({preview['item_count']} items, {preview['attachment_count']} attachments, "
            f"{preview['estimated_final_bytes']} bytes). "
            "Pass --sensitivity-ack to confirm export."
        )


class ExportSizeLimitError(ValueError):
    """Raised when the exported HTML file exceeds --max-size."""

    def __init__(self, actual_bytes: int, max_bytes: int, accounting: dict[str, object]):
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        self.result = {
            "ok": False,
            "error_code": "export-size-limit-exceeded",
            "actual_bytes": actual_bytes,
            "max_size_bytes": max_bytes,
            "accounting": accounting,
        }
        super().__init__(
            f"Export size {actual_bytes} bytes exceeds --max-size limit of {max_bytes} bytes"
        )


def parse_size_bytes(raw: str | int) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        if raw <= 0:
            raise ValueError("max size must be positive")
        return raw
    if not isinstance(raw, str):
        raise TypeError("invalid size specification")
    match = _SIZE_RE.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid size specification: {raw!r}")
    amount = float(match.group(1))
    unit = (match.group(2) or "").lower()
    value = int(amount * _SIZE_MULTIPLIERS[unit])
    if value <= 0:
        raise ValueError("max size must be positive")
    return value


def load_viewer_template(template_path: Path | None = None) -> str:
    path = template_path or TEMPLATE_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"Room View template not found at {path}; run scripts/build_view_template.py"
        )
    html = path.read_text(encoding="utf-8")
    if "</body>" not in html.lower():
        raise ValueError("viewer template is missing closing </body> tag")
    return html


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    html_bytes: bytes
    sqlite_bytes: bytes
    accounting: dict[str, object]
    sensitivity: SensitivityAssessment
    meta: dict[str, str]


class FromStateProvider:
    """Offline ViewStateProvider loaded from a state JSON file and optional attachments directory."""

    def __init__(self, state_file: Path, attachments_dir: Path | None = None) -> None:
        self._state_file = Path(state_file)
        if not self._state_file.is_file() or self._state_file.is_symlink():
            raise ViewValidationError("invalid-state-file", "state file must be a regular file")
        try:
            payload = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ViewValidationError("invalid-state-file", "state file is not valid JSON") from error
        if not isinstance(payload, Mapping):
            raise ViewValidationError("invalid-state-file", "state file must be a JSON object")
        raw_descriptor = payload.get("descriptor")
        raw_state = payload.get("state")
        if not isinstance(raw_descriptor, Mapping) or not isinstance(raw_state, Mapping):
            raise ViewValidationError(
                "invalid-state-file",
                "state file must contain 'descriptor' and 'state' objects",
            )
        self._descriptor = ViewDescriptor.from_dict(raw_descriptor)
        self._state = ViewState.from_dict(raw_state)
        self._attachments_dir = Path(attachments_dir).resolve() if attachments_dir is not None else None
        if self._attachments_dir is not None and not self._attachments_dir.is_dir():
            raise ViewValidationError("invalid-attachments-dir", "attachments directory does not exist")

        # Index declared attachment refs so media_type/digest are strictly enforced
        self._refs: dict[tuple[str, str], tuple[str, str]] = {}
        for item in self._state.items:
            for ref in item.attachments:
                self._refs[(item.id, ref.name)] = (ref.media_type, ref.digest)

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        return self._state

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        if self._attachments_dir is None:
            return None
        safe_item = validate_safe_label(item_id, "item id")
        safe_name = validate_attachment_name(name)
        ref = self._refs.get((safe_item, safe_name))
        if ref is None:
            return None
        media_type, expected_digest = ref
        # Support either <dir>/<item_id>/<name> or flat <dir>/<name>
        candidates = (
            self._attachments_dir / safe_item / safe_name,
            self._attachments_dir / safe_name,
        )
        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.is_symlink():
                raise ViewValidationError("unsafe-attachment-path", "attachment symlink is forbidden")
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self._attachments_dir)
            except ValueError as error:
                raise ViewValidationError("unsafe-attachment-path", "attachment escapes directory") from error
            if not resolved.is_file():
                continue
            data = resolved.read_bytes()
            return Attachment(
                name=safe_name,
                media_type=media_type,
                data=data,
                sha256=expected_digest,
            )
        return None


def _build_sqlite_bytes(
    descriptor: ViewDescriptor,
    state: ViewState,
    provider: ViewStateProvider,
    sensitivity: SensitivityAssessment,
    *,
    include_attachments: bool = True,
    room_id: str | None = None,
) -> tuple[bytes, dict[str, object], dict[str, str]]:
    """Build a VACUUMed room-view-v1 SQLite database in a private temp directory."""
    meta: dict[str, str] = {
        "schema_version": SCHEMA_VERSION,
        "taken_at": state.taken_at,
        "since": state.since,
        "title": descriptor.title,
        "descriptor_json": json.dumps(descriptor.to_dict(), sort_keys=True, separators=(",", ":")),
        "sensitivity": sensitivity.level,
        "sensitivity_banner": sensitivity.banner_text,
        "tool": descriptor.tool,
        "tool_version": descriptor.tool_version,
    }
    if room_id is not None:
        meta["room_id"] = validate_safe_label(room_id, "room_id")
    if state.stale is not None:
        meta["stale_json"] = json.dumps(state.stale.to_dict(), sort_keys=True, separators=(",", ":"))

    referenced_attachment_count = 0
    referenced_attachment_bytes = 0
    embedded_attachment_count = 0
    unique_blobs: dict[str, bytes] = {}
    skipped_disallowed_count = 0
    missing_attachment_count = 0
    per_media_type: dict[str, dict[str, int]] = {}

    with tempfile.TemporaryDirectory(prefix="josh-room-view-db-") as tmp:
        os.chmod(tmp, 0o700)
        db_path = Path(tmp) / "view.sqlite"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(SCHEMA_SQL)
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                sorted(meta.items()),
            )
            conn.executemany(
                "INSERT INTO groups(id, name, ord) VALUES (?, ?, ?)",
                [
                    (group.id, group.name, group.order if group.order is not None else index)
                    for index, group in enumerate(state.groups)
                ],
            )
            conn.executemany(
                "INSERT INTO stages(id, name, ord, terminal, failure) VALUES (?, ?, ?, ?, ?)",
                [
                    (stage.id, stage.name, stage.order, int(stage.terminal), int(stage.failure))
                    for stage in descriptor.stages
                ],
            )
            conn.executemany(
                (
                    "INSERT INTO items(id, group_id, stage_id, kind, label, metric, updated_at, fields_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                [
                    (
                        item.id,
                        item.group,
                        item.stage,
                        item.kind,
                        item.label,
                        item.metric,
                        item.updated_at,
                        json.dumps(dict(item.fields), sort_keys=True, separators=(",", ":")),
                    )
                    for item in state.items
                ],
            )
            conn.executemany(
                "INSERT INTO steps(item_id, name, at) VALUES (?, ?, ?)",
                [(step.item_id, step.name, step.at) for step in state.steps],
            )
            conn.executemany(
                "INSERT INTO summary(key, value) VALUES (?, ?)",
                [
                    (key, json.dumps(value) if not isinstance(value, str) else value)
                    for key, value in sorted(state.summary.items())
                ],
            )

            attachment_rows: list[tuple[str, str, str, int, str, bytes | None]] = []
            for item in state.items:
                for ref in item.attachments:
                    referenced_attachment_count += 1
                    referenced_attachment_bytes += ref.size
                    mt_entry = per_media_type.setdefault(
                        ref.media_type, {"count": 0, "referenced_bytes": 0, "embedded_count": 0}
                    )
                    mt_entry["count"] += 1
                    mt_entry["referenced_bytes"] += ref.size

                    if not include_attachments:
                        attachment_rows.append((item.id, ref.name, ref.media_type, ref.size, ref.digest, None))
                        continue

                    if ref.media_type not in ALLOWED_EMBED_MEDIA_TYPES:
                        skipped_disallowed_count += 1
                        attachment_rows.append((item.id, ref.name, ref.media_type, ref.size, ref.digest, None))
                        continue

                    if ref.digest in unique_blobs:
                        # Deduplicated: payload already stored in attachment_blobs
                        embedded_attachment_count += 1
                        mt_entry["embedded_count"] += 1
                        attachment_rows.append((item.id, ref.name, ref.media_type, ref.size, ref.digest, None))
                        continue

                    att = provider.attachment(item.id, ref.name)
                    if att is None:
                        missing_attachment_count += 1
                        attachment_rows.append((item.id, ref.name, ref.media_type, ref.size, ref.digest, None))
                        continue
                    if att.media_type != ref.media_type or att.sha256 != ref.digest:
                        raise ViewValidationError(
                            "attachment-mismatch",
                            f"attachment {item.id}/{ref.name} does not match declared metadata",
                        )
                    unique_blobs[att.sha256] = att.data
                    embedded_attachment_count += 1
                    mt_entry["embedded_count"] += 1
                    # Keep bytes NULL in attachments and store unique bytes once in attachment_blobs
                    # to achieve genuine SHA-256 deduplication in the SQLite file.
                    attachment_rows.append((item.id, ref.name, ref.media_type, att.size, att.sha256, None))

            conn.executemany(
                "INSERT INTO attachment_blobs(sha256, bytes) VALUES (?, ?)",
                sorted(unique_blobs.items()),
            )
            conn.executemany(
                "INSERT INTO attachments(item_id, name, media_type, size, sha256, bytes) VALUES (?, ?, ?, ?, ?, ?)",
                attachment_rows,
            )
            conn.commit()
            conn.execute("VACUUM")
            conn.close()
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        sqlite_bytes = db_path.read_bytes()

    unique_blob_bytes = sum(len(blob) for blob in unique_blobs.values())
    stats: dict[str, object] = {
        "item_count": len(state.items),
        "group_count": len(state.groups),
        "stage_count": len(descriptor.stages),
        "step_count": len(state.steps),
        "attachment_count": referenced_attachment_count,
        "embedded_attachment_count": embedded_attachment_count,
        "unique_attachment_blobs": len(unique_blobs),
        "attachments_referenced_bytes": referenced_attachment_bytes,
        "attachments_unique_bytes": unique_blob_bytes,
        "attachments_dedup_saved_bytes": max(0, referenced_attachment_bytes - unique_blob_bytes)
        if include_attachments
        else 0,
        "attachments_skipped_disallowed": skipped_disallowed_count,
        "attachments_missing": missing_attachment_count,
        "attachments_included": include_attachments,
        "attachments_by_media_type": per_media_type,
        "db_raw_bytes": len(sqlite_bytes),
    }
    return sqlite_bytes, stats, meta


def inject_before_last_body(template_html: str, snippet: str) -> str:
    """Inject markup immediately before the LAST </body> tag."""
    lower = template_html.lower()
    idx = lower.rfind("</body>")
    if idx == -1:
        raise ValueError("viewer template has no closing </body> tag")
    return template_html[:idx] + snippet + "\n" + template_html[idx:]


def build_offline_export(
    provider: ViewStateProvider,
    *,
    include_attachments: bool = True,
    max_size_bytes: int | None = DEFAULT_MAX_SIZE_BYTES,
    sensitivity_ack: bool = False,
    workspace: Path | None = None,
    room_id: str | None = None,
    template_path: Path | None = None,
    policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
) -> ExportArtifact:
    """Build a complete self-contained offline HTML view snapshot in memory."""
    raw_descriptor = provider.describe()
    raw_state = provider.state()
    extra_sanitizer = getattr(provider, "sanitize", None)
    if not callable(extra_sanitizer):
        extra_sanitizer = None

    descriptor = sanitize_descriptor(raw_descriptor, policy=policy)
    state = sanitize_state(descriptor, raw_state, policy=policy, extra_sanitizer=extra_sanitizer)
    sensitivity = resolve_sensitivity(descriptor, workspace=workspace)

    template_html = load_viewer_template(template_path)
    template_bytes = len(template_html.encode("utf-8"))

    sqlite_bytes, db_stats, meta = _build_sqlite_bytes(
        descriptor,
        state,
        provider,
        sensitivity,
        include_attachments=include_attachments,
        room_id=room_id,
    )
    db_gzip = gzip.compress(sqlite_bytes, compresslevel=9, mtime=0)
    db_b64 = base64.b64encode(db_gzip).decode("ascii")
    db_sha256 = hashlib.sha256(sqlite_bytes).hexdigest()

    db_tag = (
        f'<script id="room-view-db" type="application/octet-stream" '
        f'data-schema="{SCHEMA_VERSION}" data-encoding="gzip" '
        f'data-sha256="{db_sha256}">\n{db_b64}\n</script>'
    )
    final_html = inject_before_last_body(template_html, db_tag)
    final_bytes = final_html.encode("utf-8")

    accounting: dict[str, object] = {
        **db_stats,
        "template_bytes": template_bytes,
        "db_gzip_bytes": len(db_gzip),
        "db_encoded_bytes": len(db_b64),
        "db_sha256": db_sha256,
        "final_html_bytes": len(final_bytes),
        "estimated_final_bytes": len(final_bytes),
    }

    if sensitivity.requires_acknowledgment and not sensitivity_ack:
        raise SensitivityAckRequiredError(sensitivity, accounting)

    if max_size_bytes is not None and len(final_bytes) > max_size_bytes:
        raise ExportSizeLimitError(len(final_bytes), max_size_bytes, accounting)

    # Guardrail: no external CDN URLs in the non-blob shell of exported HTML
    shell_without_blobs = re.sub(
        r'<script[^>]+type="application/octet-stream"[^>]*>.*?</script>',
        "",
        final_html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if _FORBIDDEN_EXTERNAL_URL_RE.search(shell_without_blobs):
        raise ValueError("exported HTML contains a forbidden external CDN URL")

    return ExportArtifact(
        html_bytes=final_bytes,
        sqlite_bytes=sqlite_bytes,
        accounting=accounting,
        sensitivity=sensitivity,
        meta=meta,
    )


def write_export_atomic(artifact: ExportArtifact, output_path: Path) -> Path:
    """Atomically write an ExportArtifact to output_path via a private temp file."""
    target = Path(output_path)
    if target.is_symlink():
        raise ValueError("refusing to write view snapshot through a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(artifact.html_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o600)
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def export_view_snapshot(
    provider: ViewStateProvider,
    output_path: Path,
    *,
    include_attachments: bool = True,
    max_size_bytes: int | None = DEFAULT_MAX_SIZE_BYTES,
    sensitivity_ack: bool = False,
    workspace: Path | None = None,
    room_id: str | None = None,
    template_path: Path | None = None,
    policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
) -> dict[str, object]:
    """Export and atomically write a single-file offline HTML view snapshot."""
    artifact = build_offline_export(
        provider,
        include_attachments=include_attachments,
        max_size_bytes=max_size_bytes,
        sensitivity_ack=sensitivity_ack,
        workspace=workspace,
        room_id=room_id,
        template_path=template_path,
        policy=policy,
    )
    written = write_export_atomic(artifact, output_path)
    return {
        "ok": True,
        "output": str(written),
        "schema_version": SCHEMA_VERSION,
        "sha256": hashlib.sha256(artifact.html_bytes).hexdigest(),
        "sensitivity": artifact.sensitivity.to_dict(),
        "accounting": artifact.accounting,
    }
