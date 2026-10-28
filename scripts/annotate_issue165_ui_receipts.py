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
        entries.append({"path": name, "sha256": sha256(destination), "bytes": destination.stat().st_size, "boxed_control": label, "rectangle": list(box)})

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
        "fixture": "Disposable copy of candidate VSIX with test-only in-memory synthetic MinIO Dimension, Room, and two portable JAT rows. The test-only copy injects catalog/lifecycle state and stubs controller calls. Screenshot 05 removes the synthetic Latest row in memory and invokes the real TreeView provider reload override. No provider/controller writes, credentials, or real storage operations. This is native UI presentation evidence, not installed-package or storage integration evidence.",
        "fixture_harness": str(fixture_harness.relative_to(ROOT)),
        "fixture_harness_sha256": sha256(fixture_harness),
        "annotations": "Pillow overlays added after capture: red bounding rectangles and numbered captions; original renderer pixels retained beneath overlays.",
        "modal_capture": "Not captured. Trace confirms the real deleteRoom handler called showWarningMessage with the synthetic Room, Dimension, one recovery point, and irreversible warning. CDP showed no dialog DOM. A forced-X11 attempt queried only numeric _NET_WM_PID and transient properties for the isolated process tree; no distinct modal window was found. The focused X11 window was not owned by the isolated profile, so it was not captured. No confirmation was accepted.",
        "screenshots": entries,
    }
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
