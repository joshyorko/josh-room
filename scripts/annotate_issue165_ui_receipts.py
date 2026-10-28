#!/usr/bin/env python3
"""Annotate genuine issue-165 VS Code CDP captures without changing captured UI pixels."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/receipts/issue-165"
CAPTURES = (
    (Path("/tmp/issue165-01-room-inline-trash.png"), "01-room-inline-trash.png", (177, 160, 204, 185), "1  Delete Snapshot on Room hover"),
    (Path("/tmp/issue165-02-recovery-context.png"), "02-recovery-point-context.png", (128, 198, 422, 230), "2  Delete Recovery Point on selected JAT"),
    (Path("/tmp/issue165-03-room-context.png"), "03-delete-room-context.png", (128, 248, 412, 280), "3  Separate Delete Room action"),
    (Path("/tmp/issue165-05-post-synthetic-tree.png"), "05-post-synthetic-tree.png", (48, 179, 462, 210), "5  Fixture-only state change + TreeView reload"),
    (Path("/tmp/issue165-06-real-minio-post-delete-tree.png"), "06-real-minio-post-delete-tree.png", (48, 181, 462, 212), "6  Real MinIO delete; one Latest JAT remains"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    entries = []
    for source, name, box, label in CAPTURES:
        if not source.is_file():
            raise SystemExit(f"missing genuine CDP capture: {source}")
        image = Image.open(source).convert("RGB")
        if image.size != (1440, 900):
            raise SystemExit(f"unexpected image size for {source}: {image.size}")
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rectangle(box, outline=(255, 48, 48, 255), width=5)
        x, y, pad = 24, 20, 12
        text_box = draw.textbbox((0, 0), label, font=font)
        draw.rounded_rectangle((x, y, x + text_box[2] + 2 * pad, y + text_box[3] + 2 * pad), radius=8, fill=(12, 18, 28, 230), outline=(255, 220, 0, 255), width=3)
        draw.text((x + pad, y + pad), label, font=font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 255))
        destination = OUTPUT / name
        image.save(destination, optimize=True)
        entry = {"path": name, "sha256": sha256(destination), "bytes": destination.stat().st_size, "boxed_control": label, "rectangle": list(box)}
        if name.startswith("06-"):
            entry["evidence_level"] = "unmodified VSIX with local disposable MinIO controller integration"
        elif name.startswith("05-"):
            entry["evidence_level"] = "test-only in-memory fixture; TreeView refresh only"
        else:
            entry["evidence_level"] = "test-only in-memory fixture; UI presentation only"
        entries.append(entry)

    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    vsix = ROOT / "vscode-extension/josh-room-0.1.40.vsix"
    fixture_harness = ROOT / "scripts/prepare_issue165_ui_fixture.js"
    metadata = {
        "issue": 165,
        "source_sha": source_sha,
        "candidate_vsix": "vscode-extension/josh-room-0.1.40.vsix",
        "candidate_vsix_sha256": sha256(vsix),
        "capture_date": "2026-10-28",
        "host": "Dakota-NVIDIA; active kdlocpanda Wayland session",
        "application": "VS Code Insiders 1.145.0",
        "capture": "CDP Page.captureScreenshot via loopback 127.0.0.1; 1440x900; only uniquely identified disposable Insiders renderer",
        "fixture": "Screenshots 01-03 and 05 use a disposable modified candidate copy with test-only in-memory state and stubbed controller calls. Screenshot 06 uses the original unmodified 0.1.40 VSIX with a disposable local MinIO server, generated credentials, a CLI-created Room/two logical recovery points, and a controller-enforced deletion of one recovery point; the real VSIX TreeView was refreshed from the controller catalog.",
        "fixture_harness": str(fixture_harness.relative_to(ROOT)),
        "fixture_harness_sha256": sha256(fixture_harness),
        "real_minio_controller_receipt": "real-minio-controller-receipt.json",
        "annotations": "Pillow overlays added after capture: red bounding rectangles and numbered captions; original renderer pixels retained beneath overlays.",
        "modal_capture": "Not captured. Trace confirms the real deleteRoom handler called showWarningMessage with the synthetic Room, Dimension, one recovery point, and irreversible warning. The CDP renderer had no dialog DOM. GNOME ScreenshotWindow was introspected and is available, but the focused target remained the workbench rather than a verified modal; it was not called to avoid a generic-window capture. A forced-X11 scan of numeric PID/transient properties for the isolated process tree found no distinct modal window. No confirmation was accepted.",
        "screenshots": entries,
    }
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
