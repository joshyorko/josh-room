#!/usr/bin/env python3
"""Reproducible builder for the offline Josh Room View HTML template.

Embeds pinned libraries (three, sql.js + wasm, pdf.js + worker) verified
against src/josh_room/view/vendor-lock.json and inlines the shared viewer
HTML/CSS/JS with zero external network URLs and strict CSP.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import html as html_lib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEW_DIR = ROOT / "src/josh_room/view"
VIEWER_SRC = VIEW_DIR / "viewer"
LOCK_PATH = VIEW_DIR / "vendor-lock.json"
DEFAULT_OUTPUT = VIEW_DIR / "assets/room-view-template.html"

JS_MODULES_ORDER = (
    "data_layer.js",
    "layout.js",
    "scene3d.js",
    "aria_tree.js",
    "grid2d.js",
    "evidence.js",
    "sql_console.js",
    "app.js",
    "bootstrap.js",
)

_LIB_SCRIPT_RE = re.compile(
    r'<script\s+[^>]*id="lib-([^"]+)"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"__TPL_JOSH_ROOM_VIEW_[A-Z0-9_]+__")
_FORBIDDEN_CDN_RE = re.compile(
    r"https?://(?:cdn\.|unpkg\.com|esm\.sh|cdnjs\.|fonts\.googleapis\.com|fonts\.gstatic\.com|jsdelivr\.net)",
    re.IGNORECASE,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_gzip_header(gz_bytes: bytes) -> bytes:
    """Normalize RFC 1952 gzip header fields (mtime=0, OS=0xFF) for cross-version determinism.

    Python <=3.13 writes OS=0x03 on Unix while Python 3.14+ writes OS=0xFF.
    Normalizing byte 9 to 0xFF keeps gzip bytes identical across Python runtimes and platforms.
    """
    if len(gz_bytes) < 10 or gz_bytes[0] != 0x1F or gz_bytes[1] != 0x8B:
        return gz_bytes
    normalized = bytearray(gz_bytes)
    normalized[4:8] = b"\x00\x00\x00\x00"
    normalized[9] = 0xFF
    return bytes(normalized)


def _gzip_deterministic(data: bytes) -> bytes:
    return _normalize_gzip_header(gzip.compress(data, compresslevel=9, mtime=0))


def update_lock_from_vendor_dir(vendor_dir: Path, lock_path: Path = LOCK_PATH) -> dict:
    """Compute exact SHA-256 checksums for pinned library files and update vendor-lock.json."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for key, entry in lock["libraries"].items():
        path = vendor_dir / entry["file"]
        if not path.is_file():
            raise FileNotFoundError(f"missing pinned vendor file for {key}: {path}")
        raw = path.read_bytes()
        gz = _gzip_deterministic(raw)
        entry["raw_bytes"] = len(raw)
        entry["raw_sha256"] = _sha256(raw)
        entry["gzip_bytes"] = len(gz)
        entry["gzip_sha256"] = _sha256(gz)
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return lock


def _load_verified_libraries(
    lock: dict,
    *,
    vendor_dir: Path | None,
    existing_template: Path | None,
) -> dict[str, tuple[dict, bytes]]:
    """Return {lib_key: (meta, gzip_bytes)} verified against vendor-lock.json."""
    result: dict[str, tuple[dict, bytes]] = {}
    if vendor_dir is not None:
        for key, entry in lock["libraries"].items():
            path = vendor_dir / entry["file"]
            if not path.is_file():
                raise FileNotFoundError(f"missing vendor artifact {path}")
            raw = path.read_bytes()
            actual_raw_sha = _sha256(raw)
            if actual_raw_sha != entry["raw_sha256"]:
                raise ValueError(
                    f"SHA-256 mismatch for {key} ({entry['file']}): "
                    f"expected {entry['raw_sha256']}, got {actual_raw_sha}"
                )
            gz = _gzip_deterministic(raw)
            actual_gz_sha = _sha256(gz)
            if actual_gz_sha != entry["gzip_sha256"]:
                raise ValueError(
                    f"gzip SHA-256 mismatch for {key}: expected {entry['gzip_sha256']}, got {actual_gz_sha}"
                )
            result[key] = (entry, gz)
        return result

    if existing_template is None or not existing_template.is_file():
        raise FileNotFoundError(
            "No --vendor-dir supplied and no existing template available to reuse verified library blobs"
        )
    html_text = existing_template.read_text(encoding="utf-8")
    extracted = {
        lib_id: base64.b64decode("".join(body.split()))
        for lib_id, body in _LIB_SCRIPT_RE.findall(html_text)
    }
    for key, entry in lock["libraries"].items():
        if key not in extracted:
            raise ValueError(f"existing template is missing library blob lib-{key}")
        gz_cached = _normalize_gzip_header(extracted[key])
        raw = gzip.decompress(gz_cached)
        if _sha256(raw) != entry["raw_sha256"]:
            raise ValueError(f"raw SHA-256 verification failed for cached lib-{key}")
        # Prefer freshly normalized recompression when it matches the pinned digest,
        # and fall back to verified normalized cached bytes if host zlib flags differ.
        gz_det = _gzip_deterministic(raw)
        if _sha256(gz_det) == entry["gzip_sha256"]:
            result[key] = (entry, gz_det)
        elif _sha256(gz_cached) == entry["gzip_sha256"]:
            result[key] = (entry, gz_cached)
        else:
            raise ValueError(f"gzip SHA-256 verification failed for cached lib-{key}")
    return result


def build_template_html(
    *,
    vendor_dir: Path | None = None,
    existing_template: Path | None = DEFAULT_OUTPUT,
    lock_path: Path = LOCK_PATH,
    viewer_src: Path = VIEWER_SRC,
) -> tuple[str, dict]:
    """Assemble the complete deterministic offline HTML template."""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    libs = _load_verified_libraries(
        lock,
        vendor_dir=vendor_dir,
        existing_template=existing_template,
    )

    shell_html = (viewer_src / "index.html").read_text(encoding="utf-8")
    css_text = (viewer_src / "styles.css").read_text(encoding="utf-8")

    js_parts: list[str] = []
    js_digests: dict[str, str] = {}
    for name in JS_MODULES_ORDER:
        source_path = viewer_src / name
        content = source_path.read_text(encoding="utf-8")
        if "</script" in content.lower():
            raise ValueError(f"viewer source {name} contains forbidden </script sequence")
        js_digests[name] = _sha256(content.encode("utf-8"))
        js_parts.append(f"// === {name} ===\n{content}")
    combined_js = "\n\n".join(js_parts)

    lib_tags: list[str] = []
    lib_meta_summary: dict[str, dict[str, object]] = {}
    for key in sorted(libs.keys()):
        entry, gz = libs[key]
        b64 = base64.b64encode(gz).decode("ascii")
        # Wrap at 100 chars for readable, diff-friendly deterministic output
        wrapped = "\n".join(b64[i : i + 100] for i in range(0, len(b64), 100))
        lib_tags.append(
            f'<script type="application/octet-stream" id="lib-{key}" '
            f'data-encoding="gzip" data-mime="{entry["media_type"]}" '
            f'data-package="{entry["package"]}@{entry["version"]}" '
            f'data-sha256="{entry["raw_sha256"]}">\n{wrapped}\n</script>'
        )
        lib_meta_summary[key] = {
            "package": entry["package"],
            "version": entry["version"],
            "license": entry["license"],
            "raw_bytes": entry["raw_bytes"],
            "raw_sha256": entry["raw_sha256"],
            "gzip_bytes": entry["gzip_bytes"],
        }

    source_digest = _sha256(
        json.dumps(
            {
                "shell": _sha256(shell_html.encode("utf-8")),
                "styles": _sha256(css_text.encode("utf-8")),
                "js": js_digests,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    meta_payload = {
        "schema_version": 1,
        "template_version": lock["template_version"],
        "viewer_source_sha256": source_digest,
        "libraries": lib_meta_summary,
    }
    meta_attr = html_lib.escape(
        json.dumps(meta_payload, sort_keys=True, separators=(",", ":")),
        quote=True,
    )

    rendered = (
        shell_html.replace("__TPL_JOSH_ROOM_VIEW_META__", meta_attr)
        .replace("__TPL_JOSH_ROOM_VIEW_STYLES__", css_text)
        .replace("__TPL_JOSH_ROOM_VIEW_LIBRARIES__", "\n".join(lib_tags))
        .replace("__TPL_JOSH_ROOM_VIEW_SCRIPT__", combined_js)
    )

    leftover = _PLACEHOLDER_RE.findall(rendered)
    if leftover:
        raise ValueError(f"template contains unreplaced placeholders: {leftover}")

    shell_without_blobs = re.sub(
        r'<script[^>]+type="application/octet-stream"[^>]*>.*?</script>',
        "",
        rendered,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if _FORBIDDEN_CDN_RE.search(shell_without_blobs):
        raise ValueError("built template contains a forbidden external CDN URL")

    return rendered, meta_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", type=Path, help="directory containing pinned library artifacts")
    parser.add_argument("--update-lock", action="store_true", help="update vendor-lock.json checksums from --vendor-dir")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output HTML template path")
    parser.add_argument("--check", action="store_true", help="verify checked-in template matches fresh reproducible build")
    args = parser.parse_args(argv)

    if args.update_lock:
        if args.vendor_dir is None:
            parser.error("--update-lock requires --vendor-dir")
        update_lock_from_vendor_dir(args.vendor_dir)

    rendered, meta = build_template_html(
        vendor_dir=args.vendor_dir,
        existing_template=args.output,
    )
    rendered_bytes = rendered.encode("utf-8")

    if args.check:
        if not args.output.is_file():
            print(f"FAIL: missing template {args.output}", file=sys.stderr)
            return 1
        actual = args.output.read_bytes()
        if actual != rendered_bytes:
            print(
                f"FAIL: template {args.output} is stale (expected {_sha256(rendered_bytes)}, got {_sha256(actual)})",
                file=sys.stderr,
            )
            return 1
        print(f"OK: template matches reproducible build ({len(actual):,} bytes, sha256={_sha256(actual)[:16]})")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(rendered_bytes)
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output),
                "bytes": len(rendered_bytes),
                "sha256": _sha256(rendered_bytes),
                "template_version": meta["template_version"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
