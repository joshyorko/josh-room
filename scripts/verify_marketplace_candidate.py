#!/usr/bin/env python3
"""Verify that a VSIX is the exact VSIX pinned by this repository's promotion."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAMESPACE = "{http://schemas.microsoft.com/developer/vsx-schema/2011}"


def verify_candidate(vsix: Path, promotion_path: Path, manifest_path: Path) -> dict[str, str]:
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_version = promotion.get("version")
    expected_sha = promotion.get("sha256")
    if not isinstance(expected_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", expected_version):
        raise ValueError("invalid promotion version")
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("invalid promotion checksum")

    if manifest.get("version") != expected_version:
        raise ValueError("promotion version does not match extension package.json")
    for key in ("name", "publisher"):
        if not manifest.get(key):
            raise ValueError(f"extension package.json is missing {key}")

    hasher = hashlib.sha256()
    with vsix.open("rb") as candidate:
        for chunk in iter(lambda: candidate.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    if digest != expected_sha:
        raise ValueError(f"VSIX SHA-256 does not match release-promotion.json ({digest})")

    try:
        with zipfile.ZipFile(vsix) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("VSIX contains duplicate archive members")
            package = json.loads(archive.read("extension/package.json"))
            xml = ElementTree.fromstring(archive.read("extension.vsixmanifest"))
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ValueError(f"VSIX archive integrity failure: {corrupt_member}")
    except ValueError:
        raise
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, ElementTree.ParseError) as exc:
        raise ValueError(f"invalid VSIX package: {exc}") from exc

    for key in ("name", "publisher", "version", "displayName", "description"):
        if package.get(key) != manifest.get(key):
            raise ValueError(f"packaged extension {key} does not match source package.json")

    identity = xml.find(f".//{PACKAGE_NAMESPACE}Identity")
    if identity is None:
        raise ValueError("VSIX manifest has no package identity")
    package_identity = {
        "name": identity.get("Id"),
        "publisher": identity.get("Publisher"),
        "version": identity.get("Version"),
    }
    if any(package_identity[key] != manifest[key] for key in package_identity):
        raise ValueError("VSIX manifest identity does not match source package.json")

    return {
        "extension_id": f"{manifest['publisher']}.{manifest['name']}",
        "version": expected_version,
        "sha256": digest,
        "vsix": str(vsix.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vsix", type=Path, help="accepted/promoted VSIX file")
    parser.add_argument("--promotion", type=Path, default=ROOT / "release-promotion.json")
    parser.add_argument("--manifest", type=Path, default=ROOT / "vscode-extension/package.json")
    args = parser.parse_args()
    try:
        result = verify_candidate(args.vsix, args.promotion, args.manifest)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"candidate verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"verified": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
