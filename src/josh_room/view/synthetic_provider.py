"""Deterministic synthetic ViewStateProvider fixtures for demos, CI, and receipts.

Uses 100% synthetic identifiers, timestamps, metrics, source excerpts, diffs,
JSON trees, run records, PDFs, PNGs, and HTML reports. Never reads host state,
host filesystems, credentials, or private workspaces.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib
from collections.abc import Mapping
from types import MappingProxyType

from .contract import (
    Attachment,
    AttachmentRef,
    GroupState,
    ItemState,
    LegendEntry,
    MetricDescriptor,
    NodeState,
    ObjectLink,
    PreviewContent,
    SqlPreset,
    StageDescriptor,
    StepState,
    ViewDescriptor,
    ViewState,
)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def generate_synthetic_pdf(title: str, subtitle: str, pages: int = 3) -> bytes:
    """Generate a deterministic, spec-valid multi-page PDF 1.4 document."""
    objects: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    # 1: Catalog, 2: Pages placeholder, 3: Helvetica font, 4: Courier font
    add_obj(b"<< /Type /Catalog /Pages 2 0 R >>")
    add_obj(b"")  # placeholder for /Pages
    add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")

    page_ids: list[int] = []
    for page_num in range(1, pages + 1):
        lines = [
            "0.06 0.09 0.15 rg 0 0 612 792 re f",
            "0.11 0.16 0.25 rg 36 690 540 66 re f",
            "0.25 0.71 0.55 rg 36 686 540 4 re f",
            f"0.94 0.96 0.99 rg BT /F1 18 Tf 52 724 Td ({_pdf_escape(title)}) Tj ET",
            f"0.68 0.78 0.90 rg BT /F2 11 Tf 52 702 Td ({_pdf_escape(subtitle)}  |  Page {page_num} of {pages}) Tj ET",
            "0.12 0.17 0.26 rg 36 460 540 200 re f",
            "0.24 0.44 0.66 RG 1 w 36 460 540 200 re S",
            "0.88 0.93 0.98 rg BT /F1 13 Tf 52 634 Td (SYNTHETIC VERIFICATION RECEIPT) Tj ET",
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 608 Td (Section {page_num}: deterministic synthetic evidence artifact) Tj ET",
            "0.72 0.82 0.92 rg BT /F2 10 Tf 52 588 Td (Classification: SYNTHETIC FIXTURE ONLY - NO PRIVATE DATA) Tj ET",
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 568 Td (Gate check {page_num}.1: PASS   |   Gate check {page_num}.2: VERIFIED) Tj ET",
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 548 Td (Metric quantile sample: {120 * page_num} ms   |   Digest verified) Tj ET",
            "0.25 0.71 0.55 rg 64 480 60 44 re f",
            "0.25 0.71 0.55 rg 144 480 60 62 re f",
            "0.94 0.40 0.23 rg 224 480 60 36 re f",
            "0.54 0.58 0.65 rg 304 480 60 28 re f",
            "0.31 0.85 1.00 rg 384 480 60 56 re f",
            f"0.65 0.75 0.86 rg BT /F2 9 Tf 52 60 Td (Josh Room View synthetic PDF fixture - page {page_num}/{pages}) Tj ET",
        ]
        stream = "\n".join(lines).encode("ascii")
        content_id = add_obj(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        page_id = add_obj(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{index} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def generate_synthetic_png(width: int = 320, height: int = 180, *, variant: str = "baseline") -> bytes:
    """Generate a deterministic, valid RGB PNG synthetic visual comparison image."""
    rows = bytearray()
    is_after = variant != "baseline"
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if y < height // 3:
                t = y / max(1, height // 3)
                r = int(8 + 22 * t)
                g = int(14 + 30 * t)
                b = int(26 + 44 * t)
            else:
                r, g, b = 15, 23, 36
                if x % 24 == 0 or y % 20 == 0:
                    r, g, b = 36, 62, 92
            if 50 <= x <= 102 and (78 if not is_after else 66) <= y <= 152:
                r, g, b = (63, 182, 139)
            elif 132 <= x <= 184 and (92 if not is_after else 56) <= y <= 152:
                r, g, b = (240, 103, 58) if not is_after else (63, 182, 139)
            elif 214 <= x <= 266 and 104 <= y <= 152:
                r, g, b = (138, 147, 166)
            if x < 2 or x >= width - 2 or y < 2 or y >= height - 2:
                r, g, b = (79, 216, 255) if is_after else (95, 118, 148)
            rows.extend((r, g, b))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def generate_synthetic_html_report(item_id: str, *, include_script_probe: bool = False) -> bytes:
    """Generate a synthetic HTML report (optionally with an inert script tag probe)."""
    script_block = (
        '<script>window.__JOSH_ROOM_HTML_ATTACHMENT_XSS__ = "EXECUTED";</script>'
        if include_script_probe
        else ""
    )
    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Synthetic Report {item_id}</title></head>
<body>
  <h1>Synthetic Verification Report ({item_id})</h1>
  <p>Generated strictly from synthetic test data for offline Room View verification.</p>
  {script_block}
  <table>
    <thead><tr><th>Check</th><th>Baseline</th><th>Candidate</th><th>Outcome</th></tr></thead>
    <tbody>
      <tr><td>Schema validation</td><td>12 ms</td><td>11 ms</td><td>PASS</td></tr>
      <tr><td>Artifact digest</td><td>sha256-ok</td><td>sha256-ok</td><td>PASS</td></tr>
      <tr><td>Sanitizer sweep</td><td>0 leaks</td><td>0 leaks</td><td>PASS</td></tr>
    </tbody>
  </table>
</body>
</html>
"""
    return html.encode("utf-8")


_DEFAULT_STAGES = (
    StageDescriptor(id="ingest", name="Ingest", order=0),
    StageDescriptor(id="normalize", name="Normalize", order=1),
    StageDescriptor(id="build", name="Build", order=2),
    StageDescriptor(id="verify", name="Verify", order=3),
    StageDescriptor(id="evidence", name="Evidence", order=4),
    StageDescriptor(id="release", name="Release", order=5, terminal=True),
)

_DEFAULT_LEGEND = MappingProxyType(
    {
        "pass": LegendEntry(label="Pass", color="#3FB68B", shape="solid"),
        "fail": LegendEntry(label="Fail", color="#F0673A", shape="notch"),
        "skip": LegendEntry(label="Skip", color="#8A93A6", shape="hollow"),
    }
)

_DEFAULT_SQL_PRESETS = (
    SqlPreset(
        id="failures",
        name="All failures",
        sql=(
            "SELECT id, group_id, stage_id, label, metric, updated_at "
            "FROM items WHERE kind = 'fail' ORDER BY metric DESC, id ASC"
        ),
        description="Every failing item ordered by highest duration metric.",
    ),
    SqlPreset(
        id="group-stage-counts",
        name="Counts by group & stage",
        sql=(
            "SELECT g.name AS grp, s.name AS stage, i.kind, COUNT(*) AS count, "
            "ROUND(AVG(i.metric), 1) AS avg_ms "
            "FROM items i "
            "JOIN groups g ON g.id = i.group_id "
            "JOIN stages s ON s.id = i.stage_id "
            "GROUP BY g.ord, s.ord, i.kind ORDER BY g.ord, s.ord, i.kind"
        ),
        description="Roll-up across every group and stage pedestal.",
    ),
    SqlPreset(
        id="slowest-20",
        name="Top 20 longest items",
        sql=(
            "SELECT id, group_id, stage_id, kind, label, metric "
            "FROM items ORDER BY metric DESC LIMIT 20"
        ),
        description="Tallest towers by metric value.",
    ),
    SqlPreset(
        id="attachments-summary",
        name="Attached evidence",
        sql=(
            "SELECT item_id, name, media_type, size, substr(sha256, 1, 12) AS sha_prefix "
            "FROM attachments ORDER BY item_id, name"
        ),
        description="All evidence documents and images linked to items.",
    ),
)


class SyntheticPipelineProvider:
    """Reference ViewStateProvider backed purely by deterministic synthetic data.

    Presets:
    - ``default``: 3 groups x 6 stages = 300 items. Exercises normal pedestals
      (<=12 children), >16 sibling aggregation (+N more), and >50 group
      stage-collapse in one cohesive dataset.
    - ``small``: 10 items across 2 groups x 3 stages (spacious small-N check).
    - ``large``: 540 items across 4 groups x 6 stages (>500 scaling & perf budget).
    - ``sensitive``: Same structure as ``default`` marked ``confidential`` to
      test export refusal without explicit acknowledgment.
    """

    def __init__(self, *, preset: str = "default") -> None:
        if preset not in {"default", "small", "large", "sensitive"}:
            raise ValueError(f"unknown synthetic preset: {preset}")
        self.preset = preset
        self._attachments: dict[tuple[str, str], Attachment] = {}
        self._descriptor = self._build_descriptor()
        self._state = self._build_state()

    def _build_descriptor(self) -> ViewDescriptor:
        stages = _DEFAULT_STAGES[:3] if self.preset == "small" else _DEFAULT_STAGES
        sensitivity = "confidential" if self.preset == "sensitive" else "synthetic"
        title_suffix = {
            "default": "Synthetic Room Pipeline (300 items)",
            "small": "Synthetic Room Pipeline (10 items)",
            "large": "Synthetic Room Pipeline (540 items)",
            "sensitive": "Synthetic Confidential Pipeline",
        }[self.preset]
        return ViewDescriptor(
            title=title_suffix,
            stages=stages,
            group_label="Sector",
            legend=_DEFAULT_LEGEND,
            metric=MetricDescriptor(name="duration", min=10.0, max=4800.0, unit="ms"),
            view="3d",
            tool="josh-room-synthetic",
            tool_version="1.0.0",
            sensitivity=sensitivity,
            sql_presets=_DEFAULT_SQL_PRESETS,
        )

    def _register_attachment(self, item_id: str, attachment: Attachment) -> AttachmentRef:
        self._attachments[(item_id, attachment.name)] = attachment
        return AttachmentRef(
            name=attachment.name,
            media_type=attachment.media_type,
            size=attachment.size,
            digest=attachment.sha256,
        )

    def _build_state(self) -> ViewState:
        shared_pdf = generate_synthetic_pdf(
            "SYNTHETIC EVIDENCE BUNDLE",
            "Sector verification baseline (deduplicated)",
            pages=3,
        )
        fail_pdf = generate_synthetic_pdf(
            "SYNTHETIC FAILURE DIAGNOSTIC",
            "Synthetic gate failure trace and timings",
            pages=3,
        )
        png_before = generate_synthetic_png(variant="baseline")
        png_after = generate_synthetic_png(variant="candidate")

        if self.preset == "small":
            group_stage_counts: list[tuple[GroupState, dict[str, int]]] = [
                (GroupState(id="sector-alpha", name="Sector Alpha", order=0), {"ingest": 2, "normalize": 2, "build": 1}),
                (GroupState(id="sector-beta", name="Sector Beta", order=1), {"ingest": 2, "normalize": 1, "build": 2}),
            ]
        elif self.preset == "large":
            group_stage_counts = [
                (
                    GroupState(id="sector-alpha", name="Sector Alpha", order=0),
                    {"ingest": 8, "normalize": 10, "build": 12, "verify": 22, "evidence": 18, "release": 10},
                ),
                (
                    GroupState(id="sector-beta", name="Sector Beta", order=1),
                    {"ingest": 16, "normalize": 20, "build": 24, "verify": 28, "evidence": 22, "release": 20},
                ),
                (
                    GroupState(id="sector-gamma", name="Sector Gamma", order=2),
                    {"ingest": 24, "normalize": 28, "build": 30, "verify": 34, "evidence": 24, "release": 20},
                ),
                (
                    GroupState(id="sector-delta", name="Sector Delta", order=3),
                    {"ingest": 28, "normalize": 30, "build": 32, "verify": 30, "evidence": 26, "release": 24},
                ),
            ]
        else:
            group_stage_counts = [
                (
                    GroupState(id="sector-alpha", name="Sector Alpha", order=0),
                    {"ingest": 6, "normalize": 7, "build": 8, "verify": 9, "evidence": 6, "release": 6},
                ),
                (
                    GroupState(id="sector-beta", name="Sector Beta", order=1),
                    {"ingest": 5, "normalize": 6, "build": 7, "verify": 20, "evidence": 5, "release": 5},
                ),
                (
                    GroupState(id="sector-gamma", name="Sector Gamma", order=2),
                    {"ingest": 30, "normalize": 35, "build": 40, "verify": 45, "evidence": 35, "release": 25},
                ),
            ]

        groups = tuple(group for group, _ in group_stage_counts)
        items: list[ItemState] = []
        steps: list[StepState] = []
        stage_names = [s.id for s in self._descriptor.stages]

        metric_curve = (
            18.0, 26.5, 42.0, 58.0, 84.0, 115.0, 165.0, 240.0,
            340.0, 490.0, 720.0, 980.0, 1340.0, 1890.0, 2650.0, 3920.0,
        )
        counter = 0
        pass_count = 0
        fail_count = 0
        skip_count = 0

        for group, counts in group_stage_counts:
            for stage_idx, stage_id in enumerate(stage_names):
                n = counts.get(stage_id, 0)
                for local_idx in range(n):
                    counter += 1
                    item_id = f"item-{counter:04d}"
                    mod = (counter * 7 + stage_idx * 3 + local_idx) % 25
                    if mod in {2, 9, 17, 23} or (stage_id == "verify" and local_idx in {0, 1}):
                        kind = "fail"
                        fail_count += 1
                    elif mod in {5, 13, 21}:
                        kind = "skip"
                        skip_count += 1
                    else:
                        kind = "pass"
                        pass_count += 1

                    base_metric = metric_curve[(counter * 5 + local_idx * 3 + stage_idx) % len(metric_curve)]
                    if kind == "fail":
                        base_metric = round(min(4750.0, base_metric * 1.35 + 180.0), 1)
                    elif kind == "skip":
                        base_metric = round(max(12.0, base_metric * 0.25), 1)
                    else:
                        base_metric = round(base_metric, 1)

                    minute = (counter % 58) + 1
                    second = (counter * 13) % 59
                    updated_at = f"2026-11-20T15:{minute:02d}:{second:02d}Z"
                    label = f"{stage_id}-{local_idx + 1:02d}.{kind}"

                    att_refs: list[AttachmentRef] = []
                    if counter <= 18 or local_idx == 0 or (kind == "fail" and counter <= 80):
                        pdf_bytes = fail_pdf if kind == "fail" else shared_pdf
                        att_refs.append(
                            self._register_attachment(
                                item_id,
                                Attachment(
                                    name="evidence-report.pdf",
                                    media_type="application/pdf",
                                    data=pdf_bytes,
                                ),
                            )
                        )
                    if counter <= 12 or (kind == "fail" and local_idx == 0):
                        att_refs.append(
                            self._register_attachment(
                                item_id,
                                Attachment(
                                    name="before.png",
                                    media_type="image/png",
                                    data=png_before,
                                ),
                            )
                        )
                        att_refs.append(
                            self._register_attachment(
                                item_id,
                                Attachment(
                                    name="after.png",
                                    media_type="image/png",
                                    data=png_after,
                                ),
                            )
                        )
                    if counter in {1, 2, 3, 4} or (stage_id == "verify" and local_idx == 0):
                        report_bytes = generate_synthetic_html_report(
                            item_id,
                            include_script_probe=(counter == 1 or (stage_id == "verify" and local_idx == 0)),
                        )
                        att_refs.append(
                            self._register_attachment(
                                item_id,
                                Attachment(
                                    name="comparison.html",
                                    media_type="text/html",
                                    data=report_bytes,
                                ),
                            )
                        )

                    previews: list[PreviewContent] = []
                    if kind == "fail" and (counter <= 18 or local_idx == 0):
                        run_payload = {
                            "run_id": item_id,
                            "stage": stage_id,
                            "status": "failed",
                            "exit_code": 1,
                            "duration_ms": base_metric,
                            "error": f"Synthetic assertion mismatch in {stage_id} check",
                            "steps": [
                                {"name": "prepare-fixture", "status": "pass", "duration_ms": 18},
                                {"name": f"execute-{stage_id}", "status": "fail", "duration_ms": int(base_metric)},
                            ],
                            "logs": [
                                f"[INFO] starting synthetic {stage_id} gate for {item_id}",
                                f"[ERROR] expected digest match on synthetic candidate ({label})",
                            ],
                        }
                        previews.append(
                            PreviewContent(
                                slot="execution",
                                media_type="application/x-josh-run+json",
                                title="Synthetic Execution Trace",
                                body=json.dumps(run_payload, indent=2),
                                language="json",
                                summary=f"Gate exited with code 1 in {base_metric} ms",
                            )
                        )
                    elif counter <= 4:
                        previews.append(
                            PreviewContent(
                                slot="manifest",
                                media_type="application/json",
                                title="Stage Receipt JSON",
                                body=json.dumps(
                                    {
                                        "item_id": item_id,
                                        "sector": group.id,
                                        "stage": stage_id,
                                        "outcome": kind,
                                        "duration_ms": base_metric,
                                    },
                                    indent=2,
                                ),
                                language="json",
                            )
                        )

                    digest_hex = hashlib.sha256(item_id.encode()).hexdigest()
                    fields: Mapping[str, str | int | float | bool | None] = MappingProxyType(
                        {
                            "suite": f"synthetic-{group.id}",
                            "attempt": 1 if kind != "fail" else 2,
                            "worker_slot": f"slot-{(counter % 4) + 1}",
                            "digest_tag": f"sha-{digest_hex[:4]}-{digest_hex[4:8]}",
                            "deterministic": True,
                        }
                    )
                    items.append(
                        ItemState(
                            id=item_id,
                            group=group.id,
                            stage=stage_id,
                            kind=kind,
                            label=label,
                            updated_at=updated_at,
                            metric=base_metric,
                            attachments=tuple(att_refs),
                            previews=tuple(previews),
                            fields=fields,
                        )
                    )
                    for step_pos in range(stage_idx + 1):
                        step_min = max(0, minute - (stage_idx - step_pos))
                        steps.append(
                            StepState(
                                item_id=item_id,
                                name=stage_names[step_pos],
                                at=f"2026-11-20T15:{step_min:02d}:{second:02d}Z",
                            )
                        )

        return ViewState(
            taken_at="2026-11-20T15:42:00Z",
            since="2026-11-20T14:00:00Z",
            groups=groups,
            items=tuple(items),
            steps=tuple(steps),
            summary=MappingProxyType(
                {
                    "total_items": len(items),
                    "pass_count": pass_count,
                    "fail_count": fail_count,
                    "skip_count": skip_count,
                    "fixture": f"synthetic-{self.preset}",
                }
            ),
        )

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        return self._state

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        return self._attachments.get((item_id, name))


# --- Flagship Synthetic UNIX Filesystem Navigator Provider (synthetic-unix) ---

_UNIX_LEGEND = MappingProxyType(
    {
        "source": LegendEntry(label="Source", color="#4CC9F0", shape="solid"),
        "config": LegendEntry(label="Config", color="#F4A261", shape="solid"),
        "pass": LegendEntry(label="Verified", color="#3FB68B", shape="solid"),
        "fail": LegendEntry(label="Failed", color="#F0673A", shape="notch"),
        "artifact": LegendEntry(label="Artifact", color="#9B8CFF", shape="solid"),
        "skip": LegendEntry(label="Redacted", color="#8A93A6", shape="hollow"),
    }
)

_UNIX_STAGES = (
    StageDescriptor(id="root", name="Root", order=0),
    StageDescriptor(id="config", name="Config", order=1),
    StageDescriptor(id="source", name="Source", order=2),
    StageDescriptor(id="runtime", name="Runtime", order=3),
    StageDescriptor(id="verify", name="Verify", order=4, failure=True),
    StageDescriptor(id="artifact", name="Artifact", order=5, terminal=True),
)

_UNIX_SQL_PRESETS = (
    SqlPreset(
        id="unix-tree",
        name="UNIX directory tree",
        sql=(
            "SELECT path, node_kind, kind, metric, summary "
            "FROM nodes ORDER BY path ASC"
        ),
        description="Complete synthetic UNIX hierarchy across arbitrary depths.",
    ),
    SqlPreset(
        id="failing-objects",
        name="Failing runs & patches",
        sql=(
            "SELECT id, path, node_kind, metric, summary "
            "FROM nodes WHERE kind = 'fail' ORDER BY metric DESC"
        ),
        description="Objects marked failed across the filesystem.",
    ),
    SqlPreset(
        id="lineage-links",
        name="Producer -> Output -> Consumer links",
        sql=(
            "SELECT s.path AS source_path, l.relation, t.path AS target_path, l.label "
            "FROM links l "
            "JOIN nodes s ON s.id = l.source_id "
            "JOIN nodes t ON t.id = l.target_id "
            "ORDER BY s.path, l.relation"
        ),
        description="Trace producer, output, consumer, and cause relationships.",
    ),
    SqlPreset(
        id="preview-inventory",
        name="Read-only preview slots",
        sql=(
            "SELECT n.path, p.slot, p.media_type, p.status, p.reason "
            "FROM previews p JOIN nodes n ON n.id = p.node_id "
            "ORDER BY n.path, p.slot"
        ),
        description="Available, redacted, omitted, and unavailable content slots.",
    ),
)


class SyntheticUnixFsProvider:
    """Wholly synthetic UNIX filesystem hierarchy demonstrating NAVIGATE & INSPECT.

    Provides recognizable UNIX paths (/, /etc, /src, /usr, /var, /home and nested
    subdirectories up to 4 levels deep) with bounded sanitized source/diff/JSON/log/
    execution previews, PDF/PNG/inert-HTML evidence, explicit redacted/omitted states,
    and clickable producer -> output -> consumer relationship links.

    Strictly synthetic: never reads the host filesystem or any private data.
    """

    def __init__(self) -> None:
        self._attachments: dict[tuple[str, str], Attachment] = {}
        self._descriptor = self._build_descriptor()
        self._state = self._build_state()

    def _build_descriptor(self) -> ViewDescriptor:
        return ViewDescriptor(
            title="UNIX Filesystem — Synthetic Room Snapshot",
            stages=_UNIX_STAGES,
            group_label="Directory",
            legend=_UNIX_LEGEND,
            metric=MetricDescriptor(name="weight", min=1.0, max=420.0, unit="KB"),
            view="3d",
            tool="josh-room-unix-demo",
            tool_version="1.0.0",
            sensitivity="synthetic",
            sql_presets=_UNIX_SQL_PRESETS,
        )

    def _register_attachment(self, node_id: str, attachment: Attachment) -> AttachmentRef:
        self._attachments[(node_id, attachment.name)] = attachment
        return AttachmentRef(
            name=attachment.name,
            media_type=attachment.media_type,
            size=attachment.size,
            digest=attachment.sha256,
        )

    def _build_state(self) -> ViewState:
        ts = "2026-11-20T16:15:00Z"
        audit_pdf = generate_synthetic_pdf(
            "UNIX ROOM AUDIT GUIDE",
            "Synthetic verification handbook & boundary checklist",
            pages=3,
        )
        gate_pdf = generate_synthetic_pdf(
            "GATE-VERIFY FAILURE RECEIPT",
            "Synthetic execution trace for var/runs/gate-verify",
            pages=3,
        )
        png_before = generate_synthetic_png(variant="baseline")
        png_after = generate_synthetic_png(variant="candidate")
        report_html = generate_synthetic_html_report("file-var-report-html", include_script_probe=True)

        scheduler_py = (
            '"""Synthetic Room gate scheduler (wholly synthetic demo source)."""\n\n'
            "from dataclasses import dataclass\n\n\n"
            "@dataclass(frozen=True)\n"
            "class GateJob:\n"
            "    job_id: str\n"
            "    budget_ms: int\n"
            "    strict_sanitizer: bool = True\n\n\n"
            "def select_next_gate(jobs: list[GateJob]) -> GateJob | None:\n"
            "    ordered = sorted(jobs, key=lambda j: (not j.strict_sanitizer, j.budget_ms))\n"
            "    return ordered[0] if ordered else None\n\n\n"
            "def evaluate_budget(elapsed_ms: float, limit_ms: float) -> str:\n"
            "    if elapsed_ms > limit_ms:\n"
            '        return "fail"\n'
            '    return "pass"\n'
        )
        scheduler_diff = (
            "--- a/src/core/scheduler.py\n"
            "+++ b/src/core/scheduler.py\n"
            "@@ -14,5 +14,7 @@\n"
            " def evaluate_budget(elapsed_ms: float, limit_ms: float) -> str:\n"
            "-    if elapsed_ms >= limit_ms:\n"
            "+    # Enforce strict sanitizer budget margin before artifact publish\n"
            "+    if elapsed_ms > limit_ms:\n"
            '         return "fail"\n'
            '     return "pass"\n'
        )
        sanitizer_patch = (
            "--- a/src/core/sanitizer_rules.py\n"
            "+++ b/src/core/sanitizer_rules.py\n"
            "@@ -21,6 +21,8 @@\n"
            " def check_descriptor_labels(labels: list[str]) -> bool:\n"
            "-    return all(len(item) <= 96 for item in labels)\n"
            "+    # Regression under test: missing forbidden-marker sweep on legend\n"
            "+    for item in labels:\n"
            "+        if len(item) > 96:\n"
            "+            return False\n"
            "+    return True\n"
        )
        envelope_py = (
            '"""Synthetic outer envelope framing helper."""\n\n'
            "HEADER_MAGIC = b'JRM1'\n\n\n"
            "def pack_frame(tag: str, payload: bytes) -> bytes:\n"
            "    tag_bytes = tag.encode('ascii')[:16].ljust(16, b'\\x00')\n"
            "    return HEADER_MAGIC + tag_bytes + len(payload).to_bytes(4, 'big') + payload\n"
        )
        framing_py = (
            '"""Chunk size calculator for synthetic deterministic archives."""\n\n'
            "def split_chunks(size_bytes: int, chunk_bytes: int = 65536) -> int:\n"
            "    return max(1, (size_bytes + chunk_bytes - 1) // chunk_bytes)\n"
        )
        test_gate_py = (
            "import pytest\n\n\n"
            "def test_descriptor_sanitizer_blocks_bearer_in_legend():\n"
            '    """Synthetic regression test linked to gate-verify failure."""\n'
            "    sample = {'label': 'synthetic-redacted-probe'}\n"
            "    assert sample['label'] != 'leaked-marker'\n\n\n"
            "def test_budget_gate_enforces_upper_bound():\n"
            "    assert 312 > 250  # triggers synthetic gate-verify alert\n"
        )
        test_codec_py = (
            "def test_envelope_roundtrip_synthetic():\n"
            "    frame_header = b'JRM1'\n"
            "    assert len(frame_header) == 4\n"
        )
        test_bounds_py = (
            "def test_preview_cap_enforced():\n"
            "    max_bytes = 16384\n"
            "    assert max_bytes == 16 * 1024\n"
        )
        room_conf = (
            "# Synthetic Room View Configuration (example.invalid)\n"
            "[viewer]\n"
            "bind_host = 127.0.0.1\n"
            "read_only = true\n"
            "default_theme = night\n"
            "offline_csp = connect-src 'none'\n\n"
            "[export]\n"
            "schema = room-view-v1\n"
            "max_size_mb = 50\n"
            "require_sensitivity_ack = true\n"
        )
        policy_json = json.dumps(
            {
                "schema": "synthetic-room-policy-v1",
                "classification": "synthetic",
                "network": {"offline_connect_src": "none", "external_cdn": False},
                "allowlisted_previews": [
                    "text/plain",
                    "text/x-diff",
                    "application/json",
                    "application/x-josh-run+json",
                ],
                "allowlisted_attachments": [
                    "application/pdf",
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                    "text/html",
                ],
                "actions_surface": {"enabled": False, "deferred_to": "post-202-opt-in"},
            },
            indent=2,
        )
        room_check_unit = (
            "[Unit]\n"
            "Description=Synthetic Room Read-Only Verification Check\n"
            "After=local-fs.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=room-ctl verify --synthetic\n"
            "NoNewPrivileges=true\n"
        )
        snapshot_timer = (
            "[Timer]\n"
            "OnCalendar=hourly\n"
            "Persistent=true\n"
            "Unit=room-check.unit\n"
        )
        room_ctl_sh = (
            "#!/bin/sh\n"
            "# Synthetic read-only Room View launcher\n"
            "set -eu\n"
            'exec python3 -m josh_room view inspect "$1" --json\n'
        )
        fsn_export_sh = (
            "#!/bin/sh\n"
            "# Synthetic offline snapshot packager\n"
            "set -eu\n"
            "python3 -m josh_room view export --provider synthetic-unix --output snapshot.html\n"
        )
        rules_py = (
            '"""Synthetic verifier rule catalog."""\n\n'
            "RULES = (\n"
            '    ("NO_CDN", "Offline snapshot must make zero network requests"),\n'
            '    ("NO_POST", "Localhost viewer rejects all mutating HTTP verbs"),\n'
            '    ("INERT_HTML", "HTML attachments are parsed inertly, never executed"),\n'
            ")\n"
        )
        schema_json = json.dumps(
            {
                "schema_version": "room-view-v1",
                "tables": ["meta", "groups", "stages", "items", "nodes", "previews", "links", "steps", "attachments"],
                "read_only": True,
                "max_preview_chars": 16384,
            },
            indent=2,
        )
        gate_verify_run = json.dumps(
            {
                "run_id": "run-gate-verify-042",
                "command": "pytest src/tests/test_gate.py --synthetic",
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 1842,
                "error": "Gate budget check failed: sanitizer.patch skipped legend sweep (312 ms > 250 ms)",
                "steps": [
                    {"name": "load-policy", "status": "pass", "duration_ms": 42, "at": "2026-11-20T16:12:04Z"},
                    {"name": "compile-scheduler", "status": "pass", "duration_ms": 188, "at": "2026-11-20T16:12:05Z"},
                    {"name": "run-sanitizer-gate", "status": "fail", "duration_ms": 1340, "at": "2026-11-20T16:12:06Z"},
                    {"name": "emit-artifacts", "status": "pass", "duration_ms": 272, "at": "2026-11-20T16:12:07Z"},
                ],
                "logs": [
                    "16:12:04Z INFO  loaded synthetic policy from etc/policy.json",
                    "16:12:05Z INFO  evaluated src/core/scheduler.py (budget=250ms)",
                    "16:12:06Z WARN  detected unpatched legend sweep in src/core/sanitizer.patch",
                    "16:12:06Z ERROR gate-verify failed with exit_code=1 (elapsed=312ms)",
                    "16:12:07Z INFO  wrote synthetic report to var/artifacts/report.html",
                ],
                "outputs": ["var/artifacts/report.html", "var/log/pipeline/run-042.log"],
            },
            indent=2,
        )
        build_template_run = json.dumps(
            {
                "run_id": "run-build-template-019",
                "command": "python scripts/build_view_template.py --check",
                "status": "pass",
                "exit_code": 0,
                "duration_ms": 284,
                "steps": [
                    {"name": "verify-vendor-lock", "status": "pass", "duration_ms": 96, "at": "2026-11-20T16:10:10Z"},
                    {"name": "normalize-gzip-os-byte", "status": "pass", "duration_ms": 112, "at": "2026-11-20T16:10:11Z"},
                    {"name": "assert-zero-cdn", "status": "pass", "duration_ms": 76, "at": "2026-11-20T16:10:11Z"},
                ],
                "logs": [
                    "16:10:10Z INFO  verified 5 pinned libraries (three, sql.js, pdf.js)",
                    "16:10:11Z INFO  gzip header byte 9 normalized to 0xFF",
                    "16:10:11Z PASS  offline template matches reproducible digest",
                ],
                "outputs": ["var/artifacts/manifest.json"],
            },
            indent=2,
        )
        policy_sweep_run = json.dumps(
            {
                "run_id": "run-policy-sweep-008",
                "command": "pytest tests/test_public_storage_safety.py",
                "status": "pass",
                "exit_code": 0,
                "duration_ms": 146,
                "steps": [
                    {"name": "scan-tracked-files", "status": "pass", "duration_ms": 102, "at": "2026-11-20T16:08:00Z"},
                    {"name": "check-synthetic-fixtures", "status": "pass", "duration_ms": 44, "at": "2026-11-20T16:08:01Z"},
                ],
                "logs": ["16:08:01Z PASS  0 private endpoints or secrets found"],
            },
            indent=2,
        )
        run_042_log = (
            "2026-11-20T16:12:04Z [INFO]  gate-verify starting on synthetic snapshot\n"
            "2026-11-20T16:12:05Z [INFO]  scheduler target=src/core/scheduler.py limit=250ms\n"
            "2026-11-20T16:12:06Z [WARN]  patch src/core/sanitizer.patch lacks legend check\n"
            "2026-11-20T16:12:06Z [ERROR] assertion failed: elapsed_ms=312 > limit_ms=250\n"
            "2026-11-20T16:12:07Z [INFO]  captured PDF and visual diff under var/artifacts\n"
            "2026-11-20T16:12:07Z [FAIL]  exited with code 1\n"
        )
        run_041_log = (
            "2026-11-20T15:40:01Z [INFO]  gate-verify starting on baseline snapshot\n"
            "2026-11-20T15:40:02Z [PASS]  all 18 synthetic checks passed in 118ms\n"
        )
        manifest_json = json.dumps(
            {
                "artifact": "room-view-template.html",
                "schema_version": "room-view-v1",
                "libraries": {
                    "three": {"version": "0.170.0", "license": "MIT"},
                    "sql.js": {"version": "1.12.0", "license": "MIT"},
                    "pdfjs-dist": {"version": "4.10.38", "license": "Apache-2.0"},
                },
                "network_requests": 0,
                "deterministic_gzip_os_byte": "0xFF",
            },
            indent=2,
        )
        frame_v1_json = json.dumps(
            {"vector": "frame-v1", "magic": "JRM1", "payload_bytes": 128, "expected": "valid"},
            indent=2,
        )
        session_json = json.dumps(
            {
                "operator": "synth-ops",
                "endpoint": "https://example.invalid",
                "mode": "read-only",
                "focus_targets": ["var/runs/gate-verify", "src/core/scheduler.py", "etc/policy.json"],
            },
            indent=2,
        )
        root_makefile = (
            "# Synthetic Room Workspace Makefile\n"
            ".PHONY: verify export inspect\n\n"
            "verify:\n"
            "\tpytest tests/test_room_view.py -q\n\n"
            "export:\n"
            "\tpython -m josh_room view export --provider synthetic-unix --output snapshot.html\n"
        )
        root_readme = (
            "# Synthetic UNIX Room Filesystem\n\n"
            "Click any directory pedestal (/etc, /src, /usr, /var, /home) to fly\n"
            "along its ground conduit and explore nested subdirectories.\n"
            "Click any file, run, or artifact tower to spotlight it and inspect\n"
            "bounded read-only source, diffs, JSON trees, execution timelines,\n"
            "PDFs, visual comparisons, and producer -> output -> consumer links.\n"
        )

        # Register binary attachments for PDF / image / inert HTML nodes
        att_audit_pdf = self._register_attachment(
            "file-usr-audit-pdf",
            Attachment(name="audit-guide.pdf", media_type="application/pdf", data=audit_pdf),
        )
        att_gate_pdf = self._register_attachment(
            "run-var-gate-verify",
            Attachment(name="evidence-report.pdf", media_type="application/pdf", data=gate_pdf),
        )
        att_gate_before = self._register_attachment(
            "run-var-gate-verify",
            Attachment(name="before.png", media_type="image/png", data=png_before),
        )
        att_gate_after = self._register_attachment(
            "run-var-gate-verify",
            Attachment(name="after.png", media_type="image/png", data=png_after),
        )
        att_report_html = self._register_attachment(
            "file-var-report-html",
            Attachment(name="comparison.html", media_type="text/html", data=report_html),
        )
        att_report_pdf = self._register_attachment(
            "file-var-report-html",
            Attachment(name="evidence-report.pdf", media_type="application/pdf", data=gate_pdf),
        )
        att_report_before = self._register_attachment(
            "file-var-report-html",
            Attachment(name="before.png", media_type="image/png", data=png_before),
        )
        att_report_after = self._register_attachment(
            "file-var-report-html",
            Attachment(name="after.png", media_type="image/png", data=png_after),
        )
        att_diff_before = self._register_attachment(
            "file-var-visual-diff",
            Attachment(name="before.png", media_type="image/png", data=png_before),
        )
        att_diff_after = self._register_attachment(
            "file-var-visual-diff",
            Attachment(name="after.png", media_type="image/png", data=png_after),
        )
        att_runbook_pdf = self._register_attachment(
            "file-home-runbook-pdf",
            Attachment(name="runbook.pdf", media_type="application/pdf", data=audit_pdf),
        )

        nodes: tuple[NodeState, ...] = (
            # Depth 0: Root `/` + iconic root files so root slab feels alive
            NodeState(
                id="dir-root",
                parent_id=None,
                node_kind="directory",
                label="/",
                updated_at=ts,
                summary="Synthetic UNIX root directory",
                order=0,
            ),
            NodeState(
                id="file-root-readme",
                parent_id="dir-root",
                node_kind="file",
                label="README.md",
                kind="source",
                metric=14.2,
                updated_at=ts,
                summary="Overview of synthetic UNIX Room workspace",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="README.md",
                        body=root_readme,
                        language="markdown",
                    ),
                ),
                links=(ObjectLink(relation="references", target_id="file-etc-policy-json", label="Workspace policy"),),
                fields={"mode": "0644", "owner": "synth-ops"},
            ),
            NodeState(
                id="file-root-makefile",
                parent_id="dir-root",
                node_kind="file",
                label="Makefile",
                kind="config",
                metric=38.0,
                updated_at=ts,
                summary="Synthetic verification and snapshot export targets",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="Makefile",
                        body=root_makefile,
                        language="makefile",
                    ),
                ),
                links=(ObjectLink(relation="producer", target_id="run-var-build-template", label="Triggers template check"),),
                fields={"mode": "0644", "owner": "synth-ops"},
            ),
            NodeState(
                id="file-root-manifest",
                parent_id="dir-root",
                node_kind="file",
                label="room.manifest.json",
                kind="pass",
                metric=76.5,
                updated_at=ts,
                summary="Root snapshot contract metadata",
                order=2,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="room.manifest.json",
                        body=manifest_json,
                        language="json",
                    ),
                ),
                fields={"mode": "0644", "schema": "room-view-v1"},
            ),
            # --- Branch 1: /etc ---
            NodeState(
                id="dir-etc",
                parent_id="dir-root",
                node_kind="directory",
                label="etc",
                updated_at=ts,
                summary="Declarative viewer and boundary configuration",
                order=0,
            ),
            NodeState(
                id="file-etc-room-conf",
                parent_id="dir-etc",
                node_kind="file",
                label="room.conf",
                kind="config",
                metric=46.0,
                updated_at=ts,
                summary="Localhost read-only viewer & export limits",
                order=0,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title="room.conf",
                        body=room_conf,
                        language="ini",
                    ),
                ),
                links=(ObjectLink(relation="configures", target_id="run-var-gate-verify", label="Enforces read-only bounds"),),
                fields={"mode": "0640", "format": "ini"},
            ),
            NodeState(
                id="file-etc-policy-json",
                parent_id="dir-etc",
                node_kind="file",
                label="policy.json",
                kind="config",
                metric=128.4,
                updated_at=ts,
                summary="Allowlisted preview & attachment policy tree",
                order=1,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="policy.json",
                        body=policy_json,
                        language="json",
                    ),
                ),
                links=(ObjectLink(relation="verifies", target_id="file-var-report-html", label="Consumes verification report"),),
                fields={"mode": "0644", "format": "json"},
            ),
            NodeState(
                id="file-etc-vault-policy",
                parent_id="dir-etc",
                node_kind="file",
                label="vault.policy",
                kind="skip",
                metric=18.5,
                updated_at=ts,
                summary="Redacted by boundary sanitization policy",
                order=2,
                previews=(
                    PreviewContent(
                        slot="policy",
                        media_type="text/plain",
                        title="vault.policy",
                        status="redacted",
                        body=None,
                        reason="redacted-by-policy: secret-key material is never exposed in Room View",
                    ),
                ),
                fields={"mode": "0600", "redacted": True},
            ),
            NodeState(
                id="dir-etc-systemd",
                parent_id="dir-etc",
                node_kind="directory",
                label="systemd",
                updated_at=ts,
                summary="Synthetic scheduled verification unit definitions",
                order=3,
            ),
            NodeState(
                id="file-etc-room-unit",
                parent_id="dir-etc-systemd",
                node_kind="file",
                label="room-check.unit",
                kind="config",
                metric=54.0,
                updated_at=ts,
                summary="Oneshot verification service definition",
                order=0,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title="room-check.unit",
                        body=room_check_unit,
                        language="ini",
                    ),
                ),
                fields={"mode": "0644", "unit_type": "oneshot"},
            ),
            NodeState(
                id="file-etc-timer",
                parent_id="dir-etc-systemd",
                node_kind="file",
                label="snapshot.timer",
                kind="pass",
                metric=22.0,
                updated_at=ts,
                summary="Hourly synthetic check schedule",
                order=1,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title="snapshot.timer",
                        body=snapshot_timer,
                        language="ini",
                    ),
                ),
                fields={"mode": "0644", "cadence": "hourly"},
            ),
            # --- Branch 2: /src (4 levels deep!) ---
            NodeState(
                id="dir-src",
                parent_id="dir-root",
                node_kind="directory",
                label="src",
                updated_at=ts,
                summary="Synthetic Room source modules and verification suites",
                order=1,
            ),
            NodeState(
                id="file-src-pyproject",
                parent_id="dir-src",
                node_kind="file",
                label="pyproject.toml",
                kind="config",
                metric=148.0,
                updated_at=ts,
                summary="Synthetic package manifest and build metadata",
                order=0,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title="pyproject.toml",
                        body=(
                            "[project]\n"
                            'name = "synthetic-room-core"\n'
                            'version = "1.0.0"\n'
                            'requires-python = ">=3.11"\n'
                        ),
                        language="ini",
                    ),
                ),
                fields={"format": "toml", "owner": "core"},
            ),
            NodeState(
                id="file-src-init",
                parent_id="dir-src",
                node_kind="file",
                label="__init__.py",
                kind="source",
                metric=82.0,
                updated_at=ts,
                summary="Synthetic package entrypoint exports",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="__init__.py",
                        body='"""Synthetic UNIX demo package."""\n\n__all__ = ["scheduler", "envelope"]\n',
                        language="python",
                    ),
                ),
                fields={"language": "python", "owner": "core"},
            ),
            NodeState(
                id="dir-src-core",
                parent_id="dir-src",
                node_kind="directory",
                label="core",
                updated_at=ts,
                summary="Scheduler, sanitizer patches, and envelope codec",
                order=2,
            ),
            NodeState(
                id="file-src-scheduler",
                parent_id="dir-src-core",
                node_kind="file",
                label="scheduler.py",
                kind="source",
                metric=248.0,
                updated_at=ts,
                summary="Gate budget scheduler with unified diff",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="scheduler.py",
                        body=scheduler_py,
                        language="python",
                        summary="Budget scheduler source (19 lines)",
                    ),
                    PreviewContent(
                        slot="diff",
                        media_type="text/x-diff",
                        title="scheduler.py diff",
                        body=scheduler_diff,
                        language="diff",
                        summary="Strict budget threshold patch",
                    ),
                ),
                links=(
                    ObjectLink(relation="producer", target_id="run-var-gate-verify", label="Executed by gate-verify"),
                    ObjectLink(relation="references", target_id="file-etc-policy-json", label="Reads allowlist policy"),
                ),
                fields={"language": "python", "lines": 19, "owner": "core"},
            ),
            NodeState(
                id="file-src-sanitizer-patch",
                parent_id="dir-src-core",
                node_kind="file",
                label="sanitizer.patch",
                kind="fail",
                metric=342.0,
                updated_at=ts,
                summary="Failing sanitizer regression patch flagged by gate-verify",
                order=1,
                previews=(
                    PreviewContent(
                        slot="diff",
                        media_type="text/x-diff",
                        title="sanitizer.patch (unified diff)",
                        body=sanitizer_patch,
                        language="diff",
                        summary="Missing legend sweep caught in gate-verify",
                    ),
                ),
                links=(
                    ObjectLink(relation="caused_by", target_id="run-var-gate-verify", label="Triggered gate-verify failure"),
                    ObjectLink(relation="references", target_id="file-src-scheduler", label="Adjacent core module"),
                ),
                fields={"format": "unified-diff", "gate_status": "blocked"},
            ),
            NodeState(
                id="dir-src-codec",
                parent_id="dir-src-core",
                node_kind="directory",
                label="codec",
                updated_at=ts,
                summary="Deterministic outer framing codec (depth 3)",
                order=2,
            ),
            NodeState(
                id="file-src-envelope",
                parent_id="dir-src-codec",
                node_kind="file",
                label="envelope.py",
                kind="source",
                metric=164.0,
                updated_at=ts,
                summary="Outer frame packing helper",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="envelope.py",
                        body=envelope_py,
                        language="python",
                    ),
                ),
                fields={"language": "python", "layer": "codec"},
            ),
            NodeState(
                id="file-src-framing",
                parent_id="dir-src-codec",
                node_kind="file",
                label="framing.py",
                kind="pass",
                metric=92.0,
                updated_at=ts,
                summary="Deterministic chunk sizing helper",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="framing.py",
                        body=framing_py,
                        language="python",
                    ),
                ),
                fields={"language": "python", "layer": "codec"},
            ),
            NodeState(
                id="dir-src-vectors",
                parent_id="dir-src-codec",
                node_kind="directory",
                label="vectors",
                updated_at=ts,
                summary="Synthetic codec conformance vectors (depth 4)",
                order=2,
            ),
            NodeState(
                id="file-src-vec-json",
                parent_id="dir-src-vectors",
                node_kind="file",
                label="frame-v1.json",
                kind="pass",
                metric=44.0,
                updated_at=ts,
                summary="Valid framing vector JSON",
                order=0,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="frame-v1.json",
                        body=frame_v1_json,
                        language="json",
                    ),
                ),
                fields={"depth": 4, "vector": "v1"},
            ),
            NodeState(
                id="file-src-vec-bin",
                parent_id="dir-src-vectors",
                node_kind="file",
                label="malformed.bin",
                kind="skip",
                metric=12.0,
                updated_at=ts,
                summary="Binary test vector (metadata only, not previewed)",
                order=1,
                previews=(
                    PreviewContent(
                        slot="binary",
                        media_type="application/octet-stream",
                        title="malformed.bin",
                        status="unavailable",
                        body=None,
                        reason="binary-not-previewed: octet-stream payloads are excluded from inline inspection",
                    ),
                ),
                fields={"depth": 4, "binary": True},
            ),
            NodeState(
                id="dir-src-tests",
                parent_id="dir-src",
                node_kind="directory",
                label="tests",
                updated_at=ts,
                summary="Focused synthetic pytest verification suite",
                order=1,
            ),
            NodeState(
                id="file-src-test-gate",
                parent_id="dir-src-tests",
                node_kind="file",
                label="test_gate.py",
                kind="fail",
                metric=286.0,
                updated_at=ts,
                summary="Failing budget & sanitizer regression test",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="test_gate.py",
                        body=test_gate_py,
                        language="python",
                    ),
                ),
                links=(
                    ObjectLink(relation="verifies", target_id="file-src-scheduler", label="Verifies scheduler.py"),
                    ObjectLink(relation="producer", target_id="run-var-gate-verify", label="Fails in gate-verify"),
                ),
                fields={"framework": "pytest", "assertions": 2},
            ),
            NodeState(
                id="file-src-test-codec",
                parent_id="dir-src-tests",
                node_kind="file",
                label="test_codec.py",
                kind="pass",
                metric=112.0,
                updated_at=ts,
                summary="Passing envelope codec tests",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="test_codec.py",
                        body=test_codec_py,
                        language="python",
                    ),
                ),
                fields={"framework": "pytest", "assertions": 1},
            ),
            NodeState(
                id="file-src-test-bounds",
                parent_id="dir-src-tests",
                node_kind="file",
                label="test_bounds.py",
                kind="pass",
                metric=84.0,
                updated_at=ts,
                summary="Passing preview size cap tests",
                order=2,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="test_bounds.py",
                        body=test_bounds_py,
                        language="python",
                    ),
                ),
                fields={"framework": "pytest", "assertions": 1},
            ),
            # --- Branch 3: /usr ---
            NodeState(
                id="dir-usr",
                parent_id="dir-root",
                node_kind="directory",
                label="usr",
                updated_at=ts,
                summary="Synthetic binaries, verifier libraries, and audit docs",
                order=2,
            ),
            NodeState(
                id="file-usr-release",
                parent_id="dir-usr",
                node_kind="file",
                label="os-release",
                kind="config",
                metric=104.0,
                updated_at=ts,
                summary="Synthetic POSIX runtime release descriptor",
                order=0,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title="os-release",
                        body='NAME="Synthetic Room UNIX"\nVERSION="1.0 (Isla Demo)"\nMODE="read-only"\n',
                        language="ini",
                    ),
                ),
                fields={"mode": "0644"},
            ),
            NodeState(
                id="file-usr-license",
                parent_id="dir-usr",
                node_kind="file",
                label="NOTICES.md",
                kind="pass",
                metric=186.0,
                updated_at=ts,
                summary="Pinned third-party license summary (MIT & Apache-2.0)",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="NOTICES.md",
                        body=(
                            "# Third-Party Notices\n\n"
                            "- three@0.170.0 (MIT)\n"
                            "- sql.js@1.12.0 (MIT)\n"
                            "- pdfjs-dist@4.10.38 (Apache-2.0)\n"
                        ),
                        language="markdown",
                    ),
                ),
                fields={"licenses": "MIT,Apache-2.0"},
            ),
            NodeState(
                id="dir-usr-bin",
                parent_id="dir-usr",
                node_kind="directory",
                label="bin",
                updated_at=ts,
                summary="Read-only Room View CLI entry wrappers",
                order=2,
            ),
            NodeState(
                id="file-usr-room-ctl",
                parent_id="dir-usr-bin",
                node_kind="file",
                label="room-ctl",
                kind="pass",
                metric=68.0,
                updated_at=ts,
                summary="Metadata-only inspection CLI wrapper",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="room-ctl",
                        body=room_ctl_sh,
                        language="shell",
                    ),
                ),
                fields={"mode": "0755", "shell": "sh"},
            ),
            NodeState(
                id="file-usr-fsn-export",
                parent_id="dir-usr-bin",
                node_kind="file",
                label="fsn-export",
                kind="pass",
                metric=94.0,
                updated_at=ts,
                summary="Offline HTML snapshot exporter script",
                order=1,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="fsn-export",
                        body=fsn_export_sh,
                        language="shell",
                    ),
                ),
                links=(ObjectLink(relation="producer", target_id="run-var-build-template", label="Builds offline HTML"),),
                fields={"mode": "0755", "shell": "sh"},
            ),
            NodeState(
                id="dir-usr-lib",
                parent_id="dir-usr",
                node_kind="directory",
                label="lib",
                updated_at=ts,
                summary="Shared verifier modules",
                order=1,
            ),
            NodeState(
                id="dir-usr-verifier",
                parent_id="dir-usr-lib",
                node_kind="directory",
                label="verifier",
                updated_at=ts,
                summary="Static rule catalog and SQLite schema metadata",
                order=0,
            ),
            NodeState(
                id="file-usr-rules",
                parent_id="dir-usr-verifier",
                node_kind="file",
                label="rules.py",
                kind="source",
                metric=172.0,
                updated_at=ts,
                summary="Boundary rule definitions (NO_CDN, NO_POST, INERT_HTML)",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="rules.py",
                        body=rules_py,
                        language="python",
                    ),
                ),
                links=(ObjectLink(relation="configures", target_id="run-var-gate-verify", label="Supplies verifier rules"),),
                fields={"language": "python", "rule_count": 3},
            ),
            NodeState(
                id="file-usr-schema",
                parent_id="dir-usr-verifier",
                node_kind="file",
                label="schema.json",
                kind="config",
                metric=118.0,
                updated_at=ts,
                summary="room-view-v1 SQLite schema summary",
                order=1,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="schema.json",
                        body=schema_json,
                        language="json",
                    ),
                ),
                fields={"schema": "room-view-v1"},
            ),
            NodeState(
                id="dir-usr-share",
                parent_id="dir-usr",
                node_kind="directory",
                label="share",
                updated_at=ts,
                summary="Shared documentation and synthetic guides",
                order=2,
            ),
            NodeState(
                id="dir-usr-docs",
                parent_id="dir-usr-share",
                node_kind="directory",
                label="docs",
                updated_at=ts,
                summary="Audit guides and PDF reference bundles",
                order=0,
            ),
            NodeState(
                id="file-usr-audit-pdf",
                parent_id="dir-usr-docs",
                node_kind="artifact",
                label="audit-guide.pdf",
                kind="artifact",
                metric=310.0,
                updated_at=ts,
                summary="3-page synthetic PDF audit guide (lazy pdf.js)",
                order=0,
                attachments=(att_audit_pdf,),
                fields={"pages": 3, "viewer": "pdf.js"},
            ),
            # --- Branch 4: /var (Execution Runs, Logs, Artifacts — Lineage Hub!) ---
            NodeState(
                id="dir-var",
                parent_id="dir-root",
                node_kind="directory",
                label="var",
                updated_at=ts,
                summary="Execution runs, logs, and verified evidence artifacts",
                order=3,
            ),
            NodeState(
                id="file-var-state",
                parent_id="dir-var",
                node_kind="file",
                label="state.json",
                kind="artifact",
                metric=210.0,
                updated_at=ts,
                summary="Latest synthetic state summary pointer",
                order=0,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="state.json",
                        body=json.dumps(
                            {"snapshot": "synthetic-unix", "read_only": True, "active_alert": "var/runs/gate-verify"},
                            indent=2,
                        ),
                        language="json",
                    ),
                ),
                links=(ObjectLink(relation="references", target_id="run-var-gate-verify", label="Latest failing gate"),),
                fields={"schema": "room-view-v1"},
            ),
            NodeState(
                id="file-var-status",
                parent_id="dir-var",
                node_kind="file",
                label="health.ok",
                kind="pass",
                metric=96.0,
                updated_at=ts,
                summary="Localhost collector health receipt",
                order=1,
                previews=(
                    PreviewContent(
                        slot="log",
                        media_type="text/plain",
                        title="health.ok",
                        body="2026-11-20T16:15:00Z [PASS] collector healthy, zero stale errors\n",
                        language="log",
                    ),
                ),
                fields={"stale": False},
            ),
            NodeState(
                id="dir-var-runs",
                parent_id="dir-var",
                node_kind="directory",
                label="runs",
                updated_at=ts,
                summary="Recorded verification and template build runs",
                order=2,
            ),
            NodeState(
                id="run-var-gate-verify",
                parent_id="dir-var-runs",
                node_kind="run",
                label="gate-verify",
                kind="fail",
                metric=412.0,
                updated_at=ts,
                summary="Failed gate run with timeline, logs, PDF, and visual diff",
                order=0,
                attachments=(att_gate_pdf, att_gate_before, att_gate_after),
                previews=(
                    PreviewContent(
                        slot="execution",
                        media_type="application/x-josh-run+json",
                        title="Execution Timeline & Logs",
                        body=gate_verify_run,
                        language="json",
                        summary="Exited 1 after 1842 ms on run-sanitizer-gate",
                    ),
                ),
                links=(
                    ObjectLink(relation="caused_by", target_id="file-src-sanitizer-patch", label="Caused by sanitizer.patch"),
                    ObjectLink(relation="output", target_id="file-var-report-html", label="Emitted report.html"),
                    ObjectLink(relation="output", target_id="file-var-log-042", label="Wrote run-042.log"),
                ),
                fields={"exit_code": 1, "duration_ms": 1842, "runner": "synthetic-ci"},
            ),
            NodeState(
                id="run-var-build-template",
                parent_id="dir-var-runs",
                node_kind="run",
                label="build-template",
                kind="pass",
                metric=196.0,
                updated_at=ts,
                summary="Reproducible offline template verification run",
                order=1,
                previews=(
                    PreviewContent(
                        slot="execution",
                        media_type="application/x-josh-run+json",
                        title="Build Template Run",
                        body=build_template_run,
                        language="json",
                        summary="Exited 0 in 284 ms (deterministic gzip verified)",
                    ),
                ),
                links=(ObjectLink(relation="output", target_id="file-var-manifest-json", label="Produced manifest.json"),),
                fields={"exit_code": 0, "duration_ms": 284},
            ),
            NodeState(
                id="run-var-policy-sweep",
                parent_id="dir-var-runs",
                node_kind="run",
                label="policy-sweep",
                kind="pass",
                metric=134.0,
                updated_at=ts,
                summary="Public storage safety and boundary sweep",
                order=2,
                previews=(
                    PreviewContent(
                        slot="execution",
                        media_type="application/x-josh-run+json",
                        title="Policy Sweep Run",
                        body=policy_sweep_run,
                        language="json",
                        summary="Exited 0 in 146 ms (0 violations)",
                    ),
                ),
                fields={"exit_code": 0, "duration_ms": 146},
            ),
            NodeState(
                id="dir-var-log",
                parent_id="dir-var",
                node_kind="directory",
                label="log",
                updated_at=ts,
                summary="Sanitized pipeline log hierarchy",
                order=1,
            ),
            NodeState(
                id="dir-var-log-pipeline",
                parent_id="dir-var-log",
                node_kind="directory",
                label="pipeline",
                updated_at=ts,
                summary="Per-run bounded log excerpts",
                order=0,
            ),
            NodeState(
                id="file-var-log-042",
                parent_id="dir-var-log-pipeline",
                node_kind="file",
                label="run-042.log",
                kind="fail",
                metric=224.0,
                updated_at=ts,
                summary="Sanitized error log excerpt from gate-verify",
                order=0,
                previews=(
                    PreviewContent(
                        slot="log",
                        media_type="text/plain",
                        title="run-042.log",
                        body=run_042_log,
                        language="log",
                        summary="6 lines, includes ERROR and FAIL markers",
                    ),
                ),
                links=(ObjectLink(relation="artifact_of", target_id="run-var-gate-verify", label="Log of gate-verify"),),
                fields={"lines": 6, "severity": "ERROR"},
            ),
            NodeState(
                id="file-var-log-041",
                parent_id="dir-var-log-pipeline",
                node_kind="file",
                label="run-041.log",
                kind="pass",
                metric=72.0,
                updated_at=ts,
                summary="Previous passing baseline log",
                order=1,
                previews=(
                    PreviewContent(
                        slot="log",
                        media_type="text/plain",
                        title="run-041.log",
                        body=run_041_log,
                        language="log",
                    ),
                ),
                fields={"lines": 2, "severity": "INFO"},
            ),
            NodeState(
                id="file-var-log-audit",
                parent_id="dir-var-log-pipeline",
                node_kind="file",
                label="audit-2026.log",
                kind="skip",
                metric=28.0,
                updated_at=ts,
                summary="Omitted large archival log (explicit unavailable state)",
                order=2,
                previews=(
                    PreviewContent(
                        slot="log",
                        media_type="text/plain",
                        title="audit-2026.log",
                        status="omitted",
                        body=None,
                        reason="omitted-exceeds-preview-cap: archive log omitted from bounded preview",
                    ),
                ),
                fields={"omitted": True},
            ),
            NodeState(
                id="dir-var-artifacts",
                parent_id="dir-var",
                node_kind="directory",
                label="artifacts",
                updated_at=ts,
                summary="HTML reports, visual comparisons, and build manifests",
                order=2,
            ),
            NodeState(
                id="file-var-report-html",
                parent_id="dir-var-artifacts",
                node_kind="artifact",
                label="report.html",
                kind="artifact",
                metric=368.0,
                updated_at=ts,
                summary="Inert DOMParser HTML report + PDF + before/after PNGs",
                order=0,
                attachments=(att_report_pdf, att_report_before, att_report_after, att_report_html),
                links=(
                    ObjectLink(relation="artifact_of", target_id="run-var-gate-verify", label="Produced by gate-verify"),
                    ObjectLink(relation="consumer", target_id="file-etc-policy-json", label="Evaluated against policy.json"),
                ),
                fields={"inert_html": True, "attachments": 4},
            ),
            NodeState(
                id="file-var-visual-diff",
                parent_id="dir-var-artifacts",
                node_kind="artifact",
                label="visual-diff.png",
                kind="artifact",
                metric=256.0,
                updated_at=ts,
                summary="Side-by-side synthetic visual baseline vs candidate PNGs",
                order=1,
                attachments=(att_diff_before, att_diff_after),
                links=(ObjectLink(relation="artifact_of", target_id="run-var-gate-verify", label="Captured during gate-verify"),),
                fields={"width": 320, "height": 180},
            ),
            NodeState(
                id="file-var-manifest-json",
                parent_id="dir-var-artifacts",
                node_kind="artifact",
                label="manifest.json",
                kind="pass",
                metric=142.0,
                updated_at=ts,
                summary="Reproducible template build receipt JSON",
                order=2,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="manifest.json",
                        body=manifest_json,
                        language="json",
                    ),
                ),
                links=(ObjectLink(relation="artifact_of", target_id="run-var-build-template", label="Built by build-template"),),
                fields={"schema": "room-view-v1", "offline": True},
            ),
            # --- Branch 5: /home ---
            NodeState(
                id="dir-home",
                parent_id="dir-root",
                node_kind="directory",
                label="home",
                updated_at=ts,
                summary="Synthetic operator home directory (no real host data)",
                order=4,
            ),
            NodeState(
                id="file-home-profile",
                parent_id="dir-home",
                node_kind="file",
                label=".profile",
                kind="config",
                metric=132.0,
                updated_at=ts,
                summary="Synthetic shell defaults for read-only Room View",
                order=0,
                previews=(
                    PreviewContent(
                        slot="config",
                        media_type="text/plain",
                        title=".profile",
                        body="# Synthetic operator profile\nexport JOSH_ROOM_VIEW_THEME=night\n",
                        language="shell",
                    ),
                ),
                fields={"owner": "synth-ops"},
            ),
            NodeState(
                id="dir-home-ops",
                parent_id="dir-home",
                node_kind="directory",
                label="synth-ops",
                updated_at=ts,
                summary="Synthetic operator notes, session config, and runbook",
                order=1,
            ),
            NodeState(
                id="file-home-notes",
                parent_id="dir-home-ops",
                node_kind="file",
                label="notes.md",
                kind="source",
                metric=62.0,
                updated_at=ts,
                summary="Synthetic operator inspection checklist",
                order=0,
                previews=(
                    PreviewContent(
                        slot="source",
                        media_type="text/plain",
                        title="notes.md",
                        body=(
                            "# Synthetic Ops Triage Notes\n\n"
                            "1. Inspect var/runs/gate-verify for exit code 1 details.\n"
                            "2. Follow caused_by link to src/core/sanitizer.patch.\n"
                            "3. Compare visual diff under var/artifacts/report.html.\n"
                        ),
                        language="markdown",
                    ),
                ),
                links=(ObjectLink(relation="references", target_id="run-var-gate-verify", label="Active triage target"),),
                fields={"owner": "synth-ops"},
            ),
            NodeState(
                id="file-home-session",
                parent_id="dir-home-ops",
                node_kind="file",
                label="session.json",
                kind="config",
                metric=88.0,
                updated_at=ts,
                summary="Synthetic operator session metadata (no credentials)",
                order=1,
                previews=(
                    PreviewContent(
                        slot="json",
                        media_type="application/json",
                        title="session.json",
                        body=session_json,
                        language="json",
                    ),
                ),
                fields={"owner": "synth-ops", "read_only": True},
            ),
            NodeState(
                id="file-home-runbook-pdf",
                parent_id="dir-home-ops",
                node_kind="artifact",
                label="runbook.pdf",
                kind="artifact",
                metric=274.0,
                updated_at=ts,
                summary="3-page synthetic PDF runbook (deduplicated blob)",
                order=2,
                attachments=(att_runbook_pdf,),
                fields={"pages": 3, "deduplicated": True},
            ),
        )

        # Project top-level dirs & leaf nodes into legacy groups/items too for full SQL & backwards-compat parity
        by_id = {n.id: n for n in nodes}

        def top_branch(node: NodeState) -> str:
            cur = node
            if cur.parent_id == "dir-root" and cur.node_kind != "directory":
                return "dir-root"
            while cur.parent_id is not None and cur.parent_id != "dir-root":
                cur = by_id[cur.parent_id]
            return cur.id

        groups = (
            GroupState(id="dir-root", name="/", order=0),
            GroupState(id="dir-etc", name="/etc", order=1),
            GroupState(id="dir-src", name="/src", order=2),
            GroupState(id="dir-usr", name="/usr", order=3),
            GroupState(id="dir-var", name="/var", order=4),
            GroupState(id="dir-home", name="/home", order=5),
        )
        kind_to_stage = {
            "config": "config",
            "source": "source",
            "pass": "runtime",
            "fail": "verify",
            "artifact": "artifact",
            "skip": "config",
        }
        items: list[ItemState] = []
        steps: list[StepState] = []
        for node in nodes:
            if node.node_kind == "directory":
                continue
            grp = top_branch(node)
            stg = kind_to_stage.get(node.kind or "pass", "runtime")
            items.append(
                ItemState(
                    id=node.id,
                    group=grp,
                    stage=stg,
                    kind=node.kind or "pass",
                    label=node.label,
                    updated_at=node.updated_at,
                    metric=node.metric,
                    attachments=node.attachments,
                    previews=node.previews,
                    links=node.links,
                    fields=node.fields,
                )
            )
            if node.id == "run-var-gate-verify":
                steps.extend(
                    (
                        StepState(item_id=node.id, name="load-policy", at="2026-11-20T16:12:04Z"),
                        StepState(item_id=node.id, name="compile-scheduler", at="2026-11-20T16:12:05Z"),
                        StepState(item_id=node.id, name="run-sanitizer-gate", at="2026-11-20T16:12:06Z"),
                        StepState(item_id=node.id, name="emit-artifacts", at="2026-11-20T16:12:07Z"),
                    )
                )
            elif node.node_kind == "run":
                steps.append(StepState(item_id=node.id, name="completed", at=ts))

        fail_count = sum(1 for n in nodes if n.node_kind != "directory" and n.kind == "fail")
        dir_count = sum(1 for n in nodes if n.node_kind == "directory")
        leaf_count = len(nodes) - dir_count

        return ViewState(
            taken_at=ts,
            since="2026-11-20T15:00:00Z",
            groups=groups,
            items=tuple(items),
            nodes=nodes,
            steps=tuple(steps),
            summary=MappingProxyType(
                {
                    "fixture": "synthetic-unix",
                    "total_nodes": len(nodes),
                    "directories": dir_count,
                    "files_and_runs": leaf_count,
                    "fail_count": fail_count,
                }
            ),
        )

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        return self._state

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        return self._attachments.get((item_id, name))
