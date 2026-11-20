"""Metadata-only inspector for offline Room View HTML snapshots (no rendering)."""

from __future__ import annotations

import base64
import gzip
import hashlib
import html as html_lib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

_META_TAG_RE = re.compile(
    r'<meta\s+name="josh-room-view"\s+content="([^"]+)"\s*/?>',
    re.IGNORECASE,
)
_DB_SCRIPT_RE = re.compile(
    r'<script\s+([^>]*id="room-view-db"[^>]*)>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r'([a-z0-9_-]+)="([^"]*)"', re.IGNORECASE)
_LIB_SCRIPT_RE = re.compile(
    r'<script\s+([^>]*id="lib-[^"]+"[^>]*)>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_EXTERNAL_URL_RE = re.compile(r"https?://[^\s'\"`<>)]+", re.IGNORECASE)


def _parse_attrs(attr_str: str) -> dict[str, str]:
    return {k.lower(): html_lib.unescape(v) for k, v in _ATTR_RE.findall(attr_str)}


def inspect_view_snapshot(path: Path) -> dict[str, object]:
    """Inspect an exported Room View HTML snapshot without rendering or executing JS."""
    target = Path(path)
    if not target.is_file() or target.is_symlink():
        raise ValueError("snapshot path must be a regular HTML file")
    raw_bytes = target.read_bytes()
    html_text = raw_bytes.decode("utf-8")

    meta_match = _META_TAG_RE.search(html_text)
    template_meta: dict[str, object] = {}
    if meta_match:
        template_meta = json.loads(html_lib.unescape(meta_match.group(1)))

    # Collect embedded library metadata without executing anything
    embedded_libs: dict[str, dict[str, object]] = {}
    for attrs_str, body in _LIB_SCRIPT_RE.findall(html_text):
        attrs = _parse_attrs(attrs_str)
        lib_id = attrs.get("id", "")
        b64 = "".join(body.split())
        embedded_libs[lib_id] = {
            "encoding": attrs.get("data-encoding", "raw"),
            "media_type": attrs.get("data-mime", "application/octet-stream"),
            "declared_sha256": attrs.get("data-sha256"),
            "encoded_bytes": len(b64),
        }

    db_matches = list(_DB_SCRIPT_RE.finditer(html_text))
    if not db_matches:
        raise ValueError("HTML file does not contain an embedded room-view-db payload")
    db_match = db_matches[-1]
    db_attrs = _parse_attrs(db_match.group(1))
    schema_attr = db_attrs.get("data-schema", "")
    encoding = db_attrs.get("data-encoding", "raw")
    declared_db_sha = db_attrs.get("data-sha256")
    db_b64 = "".join(db_match.group(2).split())

    raw_payload = base64.b64decode(db_b64)
    sqlite_bytes = gzip.decompress(raw_payload) if encoding == "gzip" else raw_payload
    actual_db_sha = hashlib.sha256(sqlite_bytes).hexdigest()
    if declared_db_sha and declared_db_sha != actual_db_sha:
        raise ValueError("embedded SQLite SHA-256 digest mismatch")

    # Inspect shell (excluding octet-stream script blobs) for stray external URLs
    shell = re.sub(
        r'<script[^>]+type="application/octet-stream"[^>]*>.*?</script>',
        "",
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Exclude harmless SVG/W3C XML namespace URIs if any
    shell_urls = [
        url
        for url in sorted(set(_EXTERNAL_URL_RE.findall(shell)))
        if not url.startswith(("http://www.w3.org/", "https://www.w3.org/"))
    ]

    with tempfile.TemporaryDirectory(prefix="josh-room-view-inspect-") as tmp:
        os.chmod(tmp, 0o700)
        db_file = Path(tmp) / "inspect.sqlite"
        db_file.write_bytes(sqlite_bytes)
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            db_meta = dict(conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall())
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            table_counts: dict[str, int] = {}
            for name in ("meta", "groups", "stages", "items", "steps", "attachments", "attachment_blobs", "summary"):
                if name in tables:
                    table_counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])

            att_total_count, att_total_bytes = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM attachments"
            ).fetchone()
            blob_count, blob_bytes = (
                conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(bytes)), 0) FROM attachment_blobs").fetchone()
                if "attachment_blobs" in tables
                else (0, 0)
            )
            media_breakdown = {
                row[0]: {"count": int(row[1]), "bytes": int(row[2])}
                for row in conn.execute(
                    "SELECT media_type, COUNT(*), COALESCE(SUM(size), 0) FROM attachments GROUP BY media_type ORDER BY media_type"
                ).fetchall()
            }
            summary_rows = dict(conn.execute("SELECT key, value FROM summary ORDER BY key").fetchall())
        finally:
            conn.close()

    descriptor_summary: dict[str, object] = {}
    if "descriptor_json" in db_meta:
        try:
            desc = json.loads(db_meta["descriptor_json"])
            descriptor_summary = {
                "title": desc.get("title"),
                "view": desc.get("view"),
                "group_label": desc.get("group_label"),
                "stage_count": len(desc.get("stages", [])),
                "tool": desc.get("tool"),
                "tool_version": desc.get("tool_version"),
                "sensitivity": desc.get("sensitivity"),
            }
        except ValueError:
            descriptor_summary = {}

    public_meta = {k: v for k, v in db_meta.items() if k != "descriptor_json"}
    return {
        "ok": True,
        "file": str(target),
        "file_size_bytes": len(raw_bytes),
        "file_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "schema_version": db_meta.get("schema_version") or schema_attr,
        "db_encoding": encoding,
        "db_raw_bytes": len(sqlite_bytes),
        "db_encoded_bytes": len(db_b64),
        "db_sha256": actual_db_sha,
        "meta": public_meta,
        "descriptor": descriptor_summary,
        "summary": summary_rows,
        "table_counts": table_counts,
        "attachments": {
            "count": int(att_total_count),
            "referenced_bytes": int(att_total_bytes),
            "unique_blobs": int(blob_count),
            "embedded_blob_bytes": int(blob_bytes),
            "by_media_type": media_breakdown,
        },
        "template": template_meta,
        "embedded_libraries": embedded_libs,
        "external_urls": shell_urls,
    }
