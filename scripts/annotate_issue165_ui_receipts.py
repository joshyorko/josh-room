#!/usr/bin/env python3
"""Annotate genuine issue-165 VS Code CDP captures without changing captured UI pixels."""
from __future__ import annotations

import argparse
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
    (Path("/tmp/issue165-xvfb-modal-probe.png"), "04-delete-room-confirmation.png", (0, 0, 548, 76), "4  Real MinIO Delete Room warning; cancel only"),
    (Path("/tmp/issue165-05-post-synthetic-tree.png"), "05-post-synthetic-tree.png", (48, 179, 462, 210), "5  Fixture-only state change + TreeView reload"),
    (Path("/tmp/issue165-06-real-minio-post-delete-tree.png"), "06-real-minio-post-delete-tree.png", (48, 181, 462, 212), "6  Real MinIO delete; one Latest JAT remains"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def annotate_hosted_receipt(receipt_path: Path) -> None:
    """Box only controls whose actual DOM rectangles were captured by the driver."""
    receipt = json.loads(receipt_path.read_text())
    directory = receipt_path.parent
    for capture in receipt.get("screenshots", []):
        raw = directory / capture["raw"]
        destination = directory / capture["path"]
        if raw.parent != directory or destination.parent != directory:
            raise ValueError("capture filenames must stay in their receipt directory")
        image = Image.open(raw).convert("RGB")
        if image.size != (1440, 900):
            raise ValueError("unexpected hosted capture size")
        draw = ImageDraw.Draw(image)
        for box in capture["boxes"]:
            x, y, width, height = (box[key] for key in ("x", "y", "width", "height"))
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1441 or y + height > 901:
                raise ValueError("capture bounding box is outside the viewport")
            draw.rectangle((x, y, x + width, y + height), outline="red", width=4)
        draw.rectangle((12, 868, 1428, 894), fill="black")
        draw.text((20, 874), capture["label"], fill="white")
        image.save(destination, optimize=True)
        capture["raw_sha256"] = sha256(raw)
        capture["sha256"] = sha256(destination)
        capture["annotation"] = "post-capture boxes from measured workbench DOM rectangles"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    font = ImageFont.truetype(str(font_path), 22) if font_path.exists() else ImageFont.load_default()
    entries = []
    for source, name, box, label in CAPTURES:
        destination = OUTPUT / name
        if source.is_file():
            image = Image.open(source).convert("RGB")
        elif destination.is_file():
            image = Image.open(destination).convert("RGB")
        else:
            raise SystemExit(f"missing genuine screenshot or committed receipt: {source}")
        if image.size != (1440, 900):
            raise SystemExit(f"unexpected image size for {source}: {image.size}")
        draw = ImageDraw.Draw(image, "RGBA")
        if source.is_file():
            draw.rectangle(box, outline=(255, 48, 48, 255), width=5)
        additional_boxes = []
        if source.is_file() and name.startswith("04-"):
            button_box = (274, 82, 548, 115)
            draw.rectangle(button_box, outline=(255, 48, 48, 255), width=5)
            additional_boxes.append(button_box)
        if source.is_file():
            x, y, pad = (590, 20, 12) if name.startswith("04-") else (24, 20, 12)
            text_box = draw.textbbox((0, 0), label, font=font)
            draw.rounded_rectangle((x, y, x + text_box[2] + 2 * pad, y + text_box[3] + 2 * pad), radius=8, fill=(12, 18, 28, 230), outline=(255, 220, 0, 255), width=3)
            draw.text((x + pad, y + pad), label, font=font, fill=(255, 255, 255, 255), stroke_width=1, stroke_fill=(0, 0, 0, 255))
            image.save(destination, optimize=True)
        entry = {"path": name, "sha256": sha256(destination), "bytes": destination.stat().st_size, "boxed_control": label, "rectangle": list(box)}
        if additional_boxes:
            entry["additional_rectangles"] = [list(item) for item in additional_boxes]
        if name.startswith("04-"):
            entry["evidence_level"] = "unmodified VSIX; actual controller warning for disposable MinIO; dismissed with Escape"
            entry["capture_method"] = "ffmpeg x11grab of Xvfb :187 private 1440x900 display containing only isolated VS Code Insiders"
        elif name.startswith("06-"):
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
        "capture": "CDP Page.captureScreenshot for 01-03 and 05-06; ffmpeg x11grab of private Xvfb :187 for 04; all captures 1440x900 and limited to isolated VS Code windows",
        "fixture": "Screenshots 01-03 and 05 use a disposable modified candidate copy with test-only in-memory state and stubbed controller calls. Screenshots 04 and 06 use the original unmodified 0.1.40 VSIX against a disposable loopback MinIO server with generated credentials and CLI-created Rooms/two logical recovery points. #04 is the actual Delete Room confirmation for Issue165 Real Room (two recovery points), dismissed with Escape. #06 follows an actual controller deletion of one recovery point and a fresh TreeView catalog refresh.",
        "fixture_harness": str(fixture_harness.relative_to(ROOT)),
        "fixture_harness_sha256": sha256(fixture_harness),
        "real_minio_controller_receipt": "real-minio-controller-receipt.json",
        "annotations": "Pillow overlays added after capture: red bounding rectangles and numbered captions; original renderer pixels retained beneath overlays.",
        "modal_capture": "Captured as screenshot 04 from the actual unmodified VSIX modal on private Xvfb :187 using ffmpeg x11grab; red boxes identify irreversible warning and Delete Room button. Room was Issue165 Real Room in issue165-local-dimension with two controller-listed recovery points. Modal dismissed with Escape; fresh controller list verified revision 2 and both snapshots unchanged. No Room deletion was confirmed.",
        "screenshots": entries,
    }
    (OUTPUT / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.receipt:
        annotate_hosted_receipt(args.receipt)
    else:
        main()
