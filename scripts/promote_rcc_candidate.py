#!/usr/bin/env python3
"""Prepare a complete, receipt-bound RCC controller consumer update."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from scripts.build_controller_artifact import validate_receipt
from scripts.pin_controller_artifact import controller_artifact_pin
from scripts.rcc_maintenance import (
    LOCKS,
    _atomic_write_json,
    fetch_release,
    verify_release,
)

PLATFORM_ASSETS = {
    "linux-x64": "josh-room-controller-linux-amd64.rcca",
    "win32-x64": "josh-room-controller-windows-amd64.rcca",
}
FREEZES = {
    "linux-x64": "environment_linux_amd64_freeze.yaml",
    "win32-x64": "environment_windows_amd64_freeze.yaml",
}
ROBOT_FILES = (
    Path("vscode-extension/runtime/controller/robot.yaml"),
    Path("templates/room/vscode-extension/runtime/controller/robot.yaml"),
)
FREEZE_DIRS = (
    Path("vscode-extension/runtime/controller"),
    Path("templates/room/vscode-extension/runtime/controller"),
)
CONTROLLER_MANIFESTS = (
    Path("vscode-extension/runtime/controller-artifact-manifest.json"),
    Path("templates/room/vscode-extension/runtime/controller-artifact-manifest.json"),
)
RUNTIME_MANIFESTS = (
    Path("vscode-extension/runtime/manifest.json"),
    Path("templates/room/vscode-extension/runtime/manifest.json"),
)


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object in {path}")
    return value


def _candidate_robot(original: str) -> str:
    replacement = (
        "environmentConfigs:\n"
        f"  - {FREEZES['linux-x64']}\n"
        f"  - {FREEZES['win32-x64']}\n"
        "  - conda.yaml\n"
    )
    updated, count = re.subn(
        r"(?m)^environmentConfigs:\n(?:  - .*\n)+",
        replacement,
        original,
        count=1,
    )
    if count != 1:
        raise ValueError("controller robot.yaml does not have one environmentConfigs block")
    return updated


def _load_receipts(artifacts_root: Path, candidate: dict, source_sha: str) -> dict[str, dict]:
    receipts = {}
    for platform, filename in PLATFORM_ASSETS.items():
        directory = artifacts_root / f"rcc-controller-candidate-{platform}"
        receipt_path = directory / f"{Path(filename).stem}.json"
        archive_path = directory / filename
        freeze_path = directory / FREEZES[platform]
        receipt = _read_json(receipt_path)
        pin = candidate["platforms"].get(platform)
        if not pin:
            raise ValueError(f"RCC candidate is missing {platform} executable provenance")
        validate_receipt(receipt, expected_platform=platform, expected_rcc=candidate["version"])
        if receipt["source"] != source_sha:
            raise ValueError(f"{platform} receipt is stale for the requested source commit")
        if receipt["rcc_sha256"] != pin["sha256"]:
            raise ValueError(f"{platform} receipt RCC checksum differs from the verified candidate")
        if not archive_path.is_file() or _digest(archive_path) != receipt["archive"]["sha256"] or archive_path.stat().st_size != receipt["archive"]["size"]:
            raise ValueError(f"{platform} controller archive differs from its receipt")
        freeze = receipt["freeze"]
        if not freeze_path.is_file() or freeze_path.name != freeze["asset"] or _digest(freeze_path) != freeze["sha256"] or freeze_path.stat().st_size != freeze["size"]:
            raise ValueError(f"{platform} freeze differs from its receipt")
        receipts[platform] = {"receipt": receipt, "archive_path": archive_path, "freeze_path": freeze_path}
    return receipts


def _expected_assets(receipts: dict[str, dict]) -> dict[str, Path]:
    expected = {}
    for platform, items in receipts.items():
        expected[PLATFORM_ASSETS[platform]] = items["archive_path"]
        receipt_path = items["archive_path"].with_suffix(".json")
        expected[receipt_path.name] = receipt_path
    return expected


def verify_existing_release(release: dict, *, tag: str, source_sha: str, receipts: dict[str, dict]) -> list[str]:
    if release.get("tag_name") != tag or release.get("target_commitish") != source_sha:
        raise ValueError("existing controller artifact release tag/source does not match this candidate")
    if release.get("draft") is not False or release.get("prerelease") is not True:
        raise ValueError("existing controller artifact release is not the expected public prerelease")
    expected = _expected_assets(receipts)
    observed = {asset.get("name"): asset for asset in release.get("assets", []) if isinstance(asset, dict)}
    unexpected = set(observed) - set(expected)
    if unexpected:
        raise ValueError(f"existing controller artifact release has unexpected assets: {', '.join(sorted(unexpected))}")
    missing = []
    for name, path in expected.items():
        asset = observed.get(name)
        if asset is None:
            missing.append(name)
            continue
        if asset.get("digest") != f"sha256:{_digest(path)}" or asset.get("size") != path.stat().st_size:
            raise ValueError(f"existing immutable controller release asset conflicts with candidate: {name}")
    return missing


def verify_downloaded_assets(downloads: Path, receipts: dict[str, dict]) -> None:
    for name, expected_path in _expected_assets(receipts).items():
        actual = downloads / name
        if not actual.is_file() or _digest(actual) != _digest(expected_path) or actual.stat().st_size != expected_path.stat().st_size:
            raise ValueError(f"published controller release asset differs from candidate receipt: {name}")


def _build_update(root: Path, candidate: dict, receipts: dict[str, dict], source_sha: str, release_tag: str) -> list[tuple[Path, bytes]]:
    changed: list[tuple[Path, bytes]] = []
    platform_pins = candidate["platforms"]
    runtime_values = []
    for relative in RUNTIME_MANIFESTS:
        value = _read_json(root / relative)
        value.setdefault("rcc", {})["version"] = candidate["version"]
        value["rcc"]["platforms"] = platform_pins
        controller = value.setdefault("controller", {})
        controller.setdefault("environment_artifacts", {})
        for platform, items in receipts.items():
            receipt = items["receipt"]
            pin = controller_artifact_pin(receipt, items["archive_path"], release_tag, source_sha, platform)
            pin.update({
                "source_sha": source_sha,
                "rcc_version": candidate["version"],
                "rcc_sha256": receipt["rcc_sha256"],
                "input_digest": receipt["input_digest"],
                "freeze": receipt["freeze"],
            })
            controller["environment_artifacts"][platform] = pin
        controller["environment_artifact"] = controller["environment_artifacts"]["linux-x64"]
        runtime_values.append((root / relative, value))

    for relative in CONTROLLER_MANIFESTS:
        value = _read_json(root / relative)
        value["rcc"] = {"version": candidate["version"], "platforms": platform_pins}
        changed.append((root / relative, (json.dumps(value, indent=2) + "\n").encode()))

    for relative in LOCKS:
        value = _read_json(root / relative)
        linux_pin = platform_pins["linux-x64"]
        value["rcc"] = {
            **value.get("rcc", {}),
            "repository": "https://github.com/joshyorko/rcc",
            "version": candidate["version"],
            "source_sha": candidate["source_sha"],
            "managed_asset": linux_pin["asset"],
            "managed_asset_sha256": linux_pin["sha256"],
            "platforms": platform_pins,
        }
        controller = value.setdefault("controller", {})
        controller.update({
            "repository": "https://github.com/joshyorko/josh-room",
            "release_tag": release_tag,
            "source_sha": source_sha,
            "rcc_version": candidate["version"],
            "rcc_source_sha": candidate["source_sha"],
        })
        for platform, items in receipts.items():
            receipt = items["receipt"]
            controller.setdefault("platforms", {})[platform] = {
                "artifact_digest": receipt["artifact_digest"],
                "specification_digest": receipt["specification_digest"],
                "archive_sha256": receipt["archive"]["sha256"],
                "archive_size": receipt["archive"]["size"],
                "rcc_sha256": receipt["rcc_sha256"],
                "input_digest": receipt["input_digest"],
                "freeze_sha256": receipt["freeze"]["sha256"],
                "freeze_size": receipt["freeze"]["size"],
            }
        changed.append((root / relative, (json.dumps(value, indent=2) + "\n").encode()))

    for path, value in runtime_values:
        changed.append((path, (json.dumps(value, indent=2) + "\n").encode()))

    for relative in ROBOT_FILES:
        path = root / relative
        updated = _candidate_robot(path.read_text(encoding="utf-8"))
        changed.append((path, updated.encode()))

    for relative in FREEZE_DIRS:
        target_dir = root / relative
        for platform, items in receipts.items():
            changed.append((target_dir / FREEZES[platform], items["freeze_path"].read_bytes()))

    deduped: dict[Path, bytes] = {}
    for path, data in changed:
        deduped[path] = data
    return [(path, data) for path, data in deduped.items() if not path.is_file() or path.read_bytes() != data]


def promote_candidate(
    root: Path,
    candidate: dict,
    artifacts_root: Path,
    source_sha: str,
    *,
    verify_latest: bool = False,
    prepare: bool = True,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("candidate source must be a full lowercase Git SHA")
    if verify_latest:
        latest = verify_release(fetch_release())
        if latest["version"] != candidate.get("version") or latest["source_sha"] != candidate.get("source_sha") or latest["platforms"] != candidate.get("platforms"):
            raise ValueError("RCC release candidate was superseded before promotion")
    receipts = _load_receipts(artifacts_root, candidate, source_sha)
    input_identity = "-".join(receipts[platform]["receipt"]["input_digest"].split(":", 1)[1] for platform in PLATFORM_ASSETS)
    release_tag = f"rcc-{candidate['version']}-controller-{input_identity}"
    changes = _build_update(root, candidate, receipts, source_sha, release_tag)
    if not changes:
        return {"status": "PASS", "no_change": True, "changed_files": [], "candidate": candidate["version"]}
    if prepare:
        for path, data in changes:
            if path.suffix == ".json":
                value = json.loads(data)
                _atomic_write_json(path, value)
            else:
                temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                temporary.write_bytes(data)
                os.replace(temporary, path)
    return {
        "status": "PASS",
        "no_change": not changes,
        "changed_files": [str(path.relative_to(root)) for path, _data in changes],
        "candidate": candidate["version"],
        "rcc_source_sha": candidate["source_sha"],
        "source_sha": source_sha,
        "artifact_release_tag": release_tag,
        "platforms": {platform: items["receipt"] for platform, items in receipts.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--existing-release-json", type=Path)
    parser.add_argument("--downloads", type=Path)
    parser.add_argument("--release-tag")
    parser.add_argument("--plan-only", action="store_true", help="validate and report intended changes without writing consumer files")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    candidate_report = _read_json(args.candidate)
    candidate = candidate_report.get("candidate") or candidate_report
    report: dict[str, Any] = {"status": "BLOCKED"}
    try:
        receipts = _load_receipts(args.artifacts.resolve(), candidate, args.source_sha)
        if args.existing_release_json:
            if not args.release_tag:
                raise ValueError("--release-tag is required to verify a published release")
            release = _read_json(args.existing_release_json)
            missing = verify_existing_release(release, tag=args.release_tag, source_sha=args.source_sha, receipts=receipts)
            report.update(status="PASS", missing_assets=missing)
        elif args.downloads:
            verify_downloaded_assets(args.downloads.resolve(), receipts)
            report.update(status="PASS", downloaded_assets="VERIFIED")
        else:
            report.update(promote_candidate(
                root,
                candidate,
                args.artifacts.resolve(),
                args.source_sha,
                verify_latest=True,
                prepare=not args.plan_only,
            ))
    except Exception as error:  # noqa: BLE001 - always leave durable failure evidence.
        report["status"] = "FAIL"
        report["error"] = f"{type(error).__name__}: {error}"
    if args.report:
        report_path = args.report if args.report.is_absolute() else root / args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
