"""Verify an immutable tested VSIX against its pin and the release checkout."""

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath


def verify(
    root: Path,
    candidate: Path,
    *,
    tag: str | None = None,
    version: str | None = None,
    source_sha: str | None = None,
    ci_run_id: int | None = None,
) -> dict:
    package = json.loads((root / "vscode-extension/package.json").read_text())
    package_version = package.get("version")
    if not isinstance(package_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", package_version):
        raise ValueError("source package has an invalid stable version")
    if tag is not None:
        pin = json.loads((root / "release-promotion.json").read_text())
        pinned_version = pin["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", pinned_version) or tag != f"v{pinned_version}-standalone-vsix":
            raise ValueError("release tag does not match candidate version")
        if version is not None and version != pinned_version:
            raise ValueError("requested candidate version differs from release pin")
        version = pinned_version
        expected = pin["sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid candidate checksum pin")
    else:
        version = version or package_version
        expected = None
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or version != package_version:
        raise ValueError("candidate version differs from the exact source package")
    observed = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if expected is not None and observed != expected:
        raise ValueError("candidate checksum does not match tested archive")
    source_root = (root / "vscode-extension").resolve()
    ignore_file = source_root / ".vscodeignore"
    ignores = ignore_file.read_text().splitlines() if ignore_file.exists() else []
    if any(pattern.startswith("!") for pattern in ignores):
        raise ValueError("promotion verifier does not support negated ignore patterns")
    expected_sources = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*") if path.is_file()
        and path.name not in {".vscodeignore", ".gitignore"}
        and not any(fnmatch.fnmatch(path.relative_to(source_root).as_posix(), pattern)
                    for pattern in ignores if pattern and not pattern.startswith("#"))
    }
    observed_sources = set()
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
            observed_sources.add(relative)
            count += 1
        if observed_sources != expected_sources:
            raise ValueError("archive/source file inventory differs")
        package = json.loads(archive.read("extension/package.json"))
        runtime = json.loads(archive.read("extension/runtime/manifest.json"))
        if package["version"] != version or runtime["extension_version"] != version:
            raise ValueError("packaged version differs from promotion pin")
        if archive.testzip() is not None:
            raise ValueError("archive integrity failure")
    result = {
        "schema_version": 1,
        "version": version,
        "artifact": candidate.name,
        "sha256": observed,
        "size": candidate.stat().st_size,
        "source_files": count,
    }
    if source_sha is not None:
        if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("candidate source must be a full lowercase Git SHA")
        result["source_sha"] = source_sha
    if ci_run_id is not None:
        if type(ci_run_id) is not int or ci_run_id < 1:
            raise ValueError("candidate CI run ID must be a positive integer")
        result["ci_run_id"] = ci_run_id
        result["ci_workflow_path"] = ".github/workflows/ci.yml"
    return result


def verify_report(receipt: dict, observed: dict) -> None:
    if not isinstance(receipt, dict):
        raise TypeError("candidate receipt must be a JSON object")
    keys = {"schema_version", "version", "artifact", "sha256", "size", "source_files", "source_sha", "ci_run_id", "ci_workflow_path"}
    if set(receipt) != keys or any(receipt.get(key) != observed.get(key) for key in keys):
        raise ValueError("candidate receipt differs from the exact source, CI run, or VSIX bytes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--tag")
    parser.add_argument("--version")
    parser.add_argument("--source-sha")
    parser.add_argument("--ci-run-id", type=int)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--verify-report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            args.root,
            args.candidate,
            tag=args.tag,
            version=args.version,
            source_sha=args.source_sha,
            ci_run_id=args.ci_run_id,
        )
        if args.verify_report:
            verify_report(json.loads(args.verify_report.read_text(encoding="utf-8")), result)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(f"candidate verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
