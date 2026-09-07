"""Verify an immutable tested VSIX against its pin and the release checkout."""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath


def verify(root: Path, candidate: Path, tag: str) -> dict:
    pin = json.loads((root / "release-promotion.json").read_text())
    version = pin["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or tag != f"v{version}-standalone-vsix":
        raise ValueError("release tag does not match candidate version")
    expected = pin["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid candidate checksum pin")
    observed = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if observed != expected:
        raise ValueError("candidate checksum does not match tested archive")
    source_root = (root / "vscode-extension").resolve()
    count = 0
    with zipfile.ZipFile(candidate) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        for name in names:
            if name in {"extension.vsixmanifest", "[Content_Types].xml"}:
                continue
            if not name.startswith("extension/") or "\\" in name or ".." in PurePosixPath(name).parts:
                raise ValueError("unexpected archive member")
            if name.endswith("/"):
                continue
            relative = name.removeprefix("extension/")
            relative = {"readme.md": "README.md", "LICENSE.txt": "LICENSE"}.get(relative, relative)
            source = source_root / relative
            if not source.resolve().is_relative_to(source_root) or source.is_symlink():
                raise ValueError("unsafe packaged source path")
            if source.read_bytes() != archive.read(name):
                raise ValueError(f"packaged source differs: {relative}")
            count += 1
        package = json.loads(archive.read("extension/package.json"))
        runtime = json.loads(archive.read("extension/runtime/manifest.json"))
        if package["version"] != version or runtime["extension_version"] != version:
            raise ValueError("packaged version differs from promotion pin")
        if archive.testzip() is not None:
            raise ValueError("archive integrity failure")
    return {"version": version, "sha256": observed, "source_files": count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.root, args.candidate, args.tag)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"candidate verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
