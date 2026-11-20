"""Deterministic synthetic ViewStateProvider fixture for demos, CI, and receipts.

Uses 100% synthetic identifiers, timestamps, metrics, PDFs, PNGs, and HTML
reports. Never reads host state, credentials, or private workspaces.
"""

from __future__ import annotations

import hashlib
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
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 608 Td (Section {page_num}: deterministic pipeline evidence artifact) Tj ET",
            "0.72 0.82 0.92 rg BT /F2 10 Tf 52 588 Td (Classification: SYNTHETIC FIXTURE ONLY - NO PRIVATE DATA) Tj ET",
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 568 Td (Stage check {page_num}.1: PASS   |   Stage check {page_num}.2: VERIFIED) Tj ET",
            f"0.72 0.82 0.92 rg BT /F2 10 Tf 52 548 Td (Metric quantile sample: {120 * page_num} ms   |   Digest verified) Tj ET",
            # Synthetic bar graphic
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
        rows.append(0)  # PNG filter type 0 (None)
        for x in range(width):
            # Dark landscape horizon + grid + pedestals visual motif
            if y < height // 3:
                t = y / max(1, height // 3)
                r = int(8 + 22 * t)
                g = int(14 + 30 * t)
                b = int(26 + 44 * t)
            else:
                r, g, b = 15, 23, 36
                if x % 24 == 0 or y % 20 == 0:
                    r, g, b = 36, 62, 92
            # Draw three synthetic comparison bars
            if 50 <= x <= 102 and (78 if not is_after else 66) <= y <= 152:
                r, g, b = (63, 182, 139)
            elif 132 <= x <= 184 and (92 if not is_after else 56) <= y <= 152:
                r, g, b = (240, 103, 58) if not is_after else (63, 182, 139)
            elif 214 <= x <= 266 and 104 <= y <= 152:
                r, g, b = (138, 147, 166)
            # Crisp border
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
  <h1>Synthetic Pipeline Comparison Report ({item_id})</h1>
  <p>Generated strictly from synthetic test data for offline Room View verification.</p>
  {script_block}
  <table>
    <thead><tr><th>Check</th><th>Baseline</th><th>Candidate</th><th>Outcome</th></tr></thead>
    <tbody>
      <tr><td>Schema validation</td><td>12 ms</td><td>11 ms</td><td>PASS</td></tr>
      <tr><td>Artifact digest</td><td>sha256-ok</td><td>sha256-ok</td><td>PASS</td></tr>
      <tr><td>Boundary sweep</td><td>0 issues</td><td>0 issues</td><td>PASS</td></tr>
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
        # Shared deterministic synthetic artifacts (also exercises SHA-256 dedup!)
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
            # Default & sensitive: 3 groups x 6 stages = exactly 300 items.
            # - sector-alpha (42 items): all stages <= 10, expanded cleanly on overview
            # - sector-beta  (48 items): verify stage has 20 items (>16 -> 11 + '+9 more')
            # - sector-gamma (210 items): >50 items in group -> stage summaries until focused
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

        # Skewed deterministic metric curve across 5 natural quantile bands
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
                    # Deterministic mix: ~72% pass, ~16% fail, ~12% skip
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
                    second = ((counter * 13) % 59)
                    updated_at = f"2026-11-20T15:{minute:02d}:{second:02d}Z"
                    label = f"{stage_id}-{local_idx + 1:02d}.{kind}"

                    att_refs: list[AttachmentRef] = []
                    # Attach rich synthetic evidence to representative items (and first several items)
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
                        # Include a harmless script probe on item-0001 and verify first failures
                        # to verify inert DOMParser rendering never executes scripts.
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
                            fields=fields,
                        )
                    )
                    # Record stage progression steps for lineage timeline
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
