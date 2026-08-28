#!/usr/bin/env python3
"""Atomically pin a verified controller artifact into both runtime manifests."""

import argparse
import hashlib
import json
import os
from pathlib import Path


def pin_manifest(root: Path, template: Path, artifact: Path, receipt: Path, release_tag: str, platform: str = "linux-x64") -> None:
    receipt_value = json.loads(receipt.read_text())
    archive = receipt_value.get("archive", {})
    observed = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if observed != archive.get("sha256") or artifact.stat().st_size != archive.get("size"):
        raise ValueError("controller artifact archive SHA256 or size does not match receipt")
    if receipt_value.get("rcc_version") != "v18.19.3":
        raise ValueError("controller artifact receipt must use RCC v18.19.3")
    if receipt_value.get("platform") != platform:
        raise ValueError("controller artifact receipt platform does not match the requested pin")
    asset = artifact.name
    update = {
        "digest": receipt_value["artifact_digest"],
        "platform": platform,
        "archive": {
            "asset": asset,
            "url": f"https://github.com/joshyorko/josh-room/releases/download/{release_tag}/{asset}",
            "sha256": archive["sha256"],
            "size": archive["size"],
        },
    }
    values = [json.loads(path.read_text()) for path in (root, template)]
    for value in values:
        controller = value.setdefault("controller", {})
        controller.setdefault("environment_artifacts", {})[platform] = update
        if platform == "linux-x64":
            controller["environment_artifact"] = update
    for path, value in zip((root, template), values, strict=True):
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.replace(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--platform", choices=("linux-x64", "win32-x64"), default="linux-x64")
    args = parser.parse_args(argv)
    pin_manifest(args.root, args.template, args.artifact, args.receipt, args.release_tag, args.platform)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
