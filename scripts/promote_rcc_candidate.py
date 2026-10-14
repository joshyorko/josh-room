#!/usr/bin/env python3
"""Prepare a complete, receipt-bound controller and JAT runtime consumer update."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ``python scripts/promote_rcc_candidate.py`` sets sys.path[0] to this
# directory.  Add the repository root so the same imports work in Actions,
# local CLI use, and module-based tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_controller_artifact import validate_receipt
from scripts.jat_runtime_candidate import (
    JAT_RELEASE_LINE,
)
from scripts.jat_runtime_candidate import (
    fetch_latest_candidate as fetch_latest_jat_candidate,
)
from scripts.jat_runtime_candidate import (
    validate_candidate as validate_jat_candidate,
)
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
PACKAGE_MANIFESTS = (
    Path("vscode-extension/package.json"),
    Path("templates/room/vscode-extension/package.json"),
)
TEMPLATE_MANIFEST = Path("templates/room/devcontainer-template.json")
BOOTSTRAP_FILES = (
    Path(".devcontainer/bootstrap.sh"),
    Path("templates/room/.devcontainer/bootstrap.sh"),
)
RELEASE_CANDIDATE_LOCKS = (
    Path("release-lock.json"),
    Path("templates/room/vscode-extension/runtime/release-lock.json"),
)
VERSION_LINE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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


def verify_existing_release(
    release: dict,
    *,
    tag: str,
    source_sha: str,
    tag_target_sha: str,
    receipts: dict[str, dict],
) -> list[str]:
    if release.get("tag_name") != tag or release.get("target_commitish") != source_sha:
        raise ValueError("existing controller artifact release tag/source does not match this candidate")
    if tag_target_sha != source_sha:
        raise ValueError("existing controller artifact Git tag does not resolve to the qualified source commit")
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


def _jat_release_version(tag: str) -> tuple[int, int, int]:
    match = JAT_RELEASE_LINE.fullmatch(tag)
    if not match:
        raise ValueError("pinned JAT runtime release has an unsupported tag")
    return tuple(map(int, match.groups()))


def _jat_tag_from_manifest(jat: dict[str, Any]) -> str:
    tag = jat.get("release_tag")
    if isinstance(tag, str) and JAT_RELEASE_LINE.fullmatch(tag):
        return tag
    artifacts = jat.get("environment_artifacts")
    if isinstance(artifacts, dict):
        linux = artifacts.get("linux-x64")
        archive = linux.get("archive") if isinstance(linux, dict) else None
        url = archive.get("url") if isinstance(archive, dict) else None
        if isinstance(url, str):
            match = re.search(r"/releases/download/([^/]+)/[^/]+$", url)
            if match and JAT_RELEASE_LINE.fullmatch(match.group(1)):
                return match.group(1)
    raise ValueError("pinned JAT runtime has no valid immutable release tag")


def _validate_jat_candidate(root: Path, candidate: dict, jat_candidate: dict) -> dict[str, Any]:
    expected_rcc = {
        "version": candidate.get("version"),
        "source_sha": candidate.get("source_sha"),
        "platforms": candidate.get("platforms"),
    }
    verified = validate_jat_candidate(jat_candidate, expected_rcc=expected_rcc)
    current_values = []
    for relative in RUNTIME_MANIFESTS:
        current = _read_json(root / relative).get("jat")
        current_values.append(current if isinstance(current, dict) else {})
    current_sources = {value.get("git_sha") for value in current_values}
    if len(current_sources) != 1 or not next(iter(current_sources)):
        raise ValueError("current JAT source pins differ across the consumer manifests")
    previous_source_sha = next(iter(current_sources))
    lock_sources = [
        _read_json(root / relative).get("jat", {}).get("git_sha")
        for relative in LOCKS
        if (root / relative).is_file()
    ]
    if any(value is not None and value != previous_source_sha for value in lock_sources):
        raise ValueError("current JAT source pins differ across consumer manifests and release locks")
    for current in current_values:
        current_tag = _jat_tag_from_manifest(current)
        if _jat_release_version(verified["release_tag"]) < _jat_release_version(current_tag):
            raise ValueError("JAT runtime candidate would downgrade the immutable release pin")
        if verified["release_tag"] == current_tag:
            if previous_source_sha != verified["source_sha"]:
                raise ValueError("a published JAT release tag cannot change its pinned producer source")
            current_artifacts = current.get("environment_artifacts")
            if isinstance(current_artifacts, dict):
                current_artifacts = {
                    platform: {
                        key: value
                        for key, value in artifact.items()
                        if key not in {"rcc_version", "rcc_source_sha"}
                    }
                    for platform, artifact in current_artifacts.items()
                }
            if current_artifacts != verified.get("environment_artifacts") or current.get("rcc", {}).get("version") != verified["rcc_version"]:
                raise ValueError("JAT runtime candidate conflicts with an immutable release tag already pinned by this repository")
    verified["source_handoff"] = {
        "from": previous_source_sha,
        "to": verified["source_sha"],
        "release_tag": verified["release_tag"],
        "requires_consumer_review": previous_source_sha != verified["source_sha"],
        "state": "awaiting_consumer_pr_review" if previous_source_sha != verified["source_sha"] else "same_pinned_source",
    }
    return verified


def _next_extension_version(root: Path) -> str:
    versions = {_read_json(root / relative).get("version") for relative in PACKAGE_MANIFESTS}
    if len(versions) != 1:
        raise ValueError("extension package versions differ across consumer manifests")
    current = next(iter(versions))
    runtime_versions = {_read_json(root / relative).get("extension_version") for relative in RUNTIME_MANIFESTS}
    candidate_versions = {
        version
        for relative in RELEASE_CANDIDATE_LOCKS
        if (root / relative).is_file()
        if (version := _read_json(root / relative).get("candidate_version")) is not None
    }
    if runtime_versions != {current} or (candidate_versions and candidate_versions != {current}):
        raise ValueError("extension package, runtime, and candidate lock versions are not synchronized")
    match = VERSION_LINE.fullmatch(str(current))
    if not match:
        raise ValueError("extension package version is not a stable semantic version")
    major, minor, patch = map(int, match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _version_update(
    root: Path,
    version: str,
    base_changes: list[tuple[Path, bytes]],
) -> list[tuple[Path, bytes]]:
    planned = {path: data for path, data in base_changes}

    def read(path: Path) -> dict[str, Any]:
        data = planned.get(path)
        return json.loads(data) if data is not None else _read_json(path)

    changed: list[tuple[Path, bytes]] = []
    current_versions = {_read_json(root / relative).get("version") for relative in PACKAGE_MANIFESTS}
    if len(current_versions) != 1:
        raise ValueError("extension package versions differ across consumer manifests")
    current_version = next(iter(current_versions))
    for relative in PACKAGE_MANIFESTS:
        path = root / relative
        value = read(path)
        value["version"] = version
        changed.append((path, (json.dumps(value, indent=2) + "\n").encode()))
    template_path = root / TEMPLATE_MANIFEST
    template = read(template_path)
    template_version = template.get("version")
    match = VERSION_LINE.fullmatch(str(template_version))
    if not match:
        raise ValueError("Dev Container template version is not a stable semantic version")
    major, minor, patch = map(int, match.groups())
    template["version"] = f"{major}.{minor}.{patch + 1}"
    changed.append((template_path, (json.dumps(template, indent=2) + "\n").encode()))
    for relative in RUNTIME_MANIFESTS:
        path = root / relative
        value = read(path)
        value["extension_version"] = version
        changed.append((path, (json.dumps(value, indent=2) + "\n").encode()))
    for relative in RELEASE_CANDIDATE_LOCKS:
        path = root / relative
        value = read(path)
        value["candidate_version"] = version
        changed.append((path, (json.dumps(value, indent=2) + "\n").encode()))
    current_extension_id = f"joshyorko.josh-room-{current_version}"
    next_extension_id = f"joshyorko.josh-room-{version}"
    for relative in BOOTSTRAP_FILES:
        path = root / relative
        text = path.read_text(encoding="utf-8")
        if current_extension_id not in text:
            raise ValueError(f"template bootstrap does not pin the current extension version: {relative}")
        changed.append((path, text.replace(current_extension_id, next_extension_id).encode()))
    return [(path, data) for path, data in changed if not path.is_file() or path.read_bytes() != data]


def _build_update(
    root: Path,
    candidate: dict,
    receipts: dict[str, dict],
    source_sha: str,
    release_tag: str,
    jat_candidate: dict[str, Any],
) -> list[tuple[Path, bytes]]:
    changed: list[tuple[Path, bytes]] = []
    platform_pins = candidate["platforms"]
    runtime_values = []
    for relative in RUNTIME_MANIFESTS:
        value = _read_json(root / relative)
        jat = value.setdefault("jat", {})
        jat["git_sha"] = jat_candidate["source_sha"]
        jat["release_tag"] = jat_candidate["release_tag"]
        jat["source_archive"] = json.loads(json.dumps(jat_candidate["source_archive"]))
        jat["producer_workflow_run"] = jat_candidate.get("workflow_run")
        jat["rcc"] = {
            "version": jat_candidate["rcc_version"],
            "source_sha": candidate["source_sha"],
            "platforms": json.loads(json.dumps(candidate["platforms"])),
        }
        jat["environment_artifacts"] = json.loads(json.dumps(jat_candidate["environment_artifacts"]))
        for artifact in jat["environment_artifacts"].values():
            artifact["rcc_version"] = jat_candidate["rcc_version"]
            artifact["rcc_source_sha"] = candidate["source_sha"]
        jat["environment_artifact"] = jat["environment_artifacts"]["linux-x64"]
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
        lock_path = root / relative
        if lock_path.is_file():
            value = _read_json(lock_path)
        elif relative == LOCKS[1] and (root / LOCKS[0]).is_file():
            # The repository historically had only the root lock. Bootstrap
            # the template copy from it on first promotion so the two
            # consumer manifests become coherent in one atomic promotion.
            value = _read_json(root / LOCKS[0])
        else:
            raise FileNotFoundError(f"required consumer release lock is missing: {lock_path}")
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
        jat_lock = value.setdefault("jat", {})
        jat_lock["git_sha"] = jat_candidate["source_sha"]
        jat_lock["version"] = f"git-{jat_candidate['source_sha'][:8]}"
        jat_lock["release_tag"] = jat_candidate["release_tag"]
        jat_lock["producer_workflow_run"] = jat_candidate.get("workflow_run")
        jat_lock["rcc"] = {
            "version": jat_candidate["rcc_version"],
            "source_sha": candidate["source_sha"],
            "platforms": json.loads(json.dumps(candidate["platforms"])),
        }
        jat_lock_artifacts = {}
        for platform, lock_platform in (("linux-x64", "linux_amd64"), ("win32-x64", "windows_amd64")):
            artifact = json.loads(json.dumps(jat_candidate["environment_artifacts"][platform]))
            artifact["rcc_version"] = jat_candidate["rcc_version"]
            artifact["rcc_source_sha"] = candidate["source_sha"]
            archive = artifact["archive"]
            jat_lock_artifacts[lock_platform] = {
                "rcc_artifact_digest": artifact["digest"],
                "rcc_specification_digest": artifact["specification_digest"],
                "archive_sha256": archive["sha256"],
                "archive_size": archive["size"],
                "rcc_version": jat_candidate["rcc_version"],
                "rcc_source_sha": candidate["source_sha"],
            }
        jat_lock["environment_artifacts"] = jat_lock_artifacts
        linux_jat = jat_candidate["environment_artifacts"]["linux-x64"]
        jat_lock["environment_artifact"] = {
            "release_tag": jat_candidate["release_tag"],
            "archive_url": linux_jat["archive"]["url"],
            "archive_sha256": linux_jat["archive"]["sha256"],
            "archive_size": linux_jat["archive"]["size"],
            "rcc_artifact_digest": linux_jat["digest"],
            "rcc_specification_digest": linux_jat["specification_digest"],
            "rcc_version": jat_candidate["rcc_version"],
            "rcc_source_sha": candidate["source_sha"],
            "platform": "linux_amd64",
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
    jat_candidate: dict[str, Any],
    verify_latest: bool = False,
    prepare: bool = True,
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("candidate source must be a full lowercase Git SHA")
    verified_jat = _validate_jat_candidate(root, candidate, jat_candidate)
    if verify_latest:
        latest = verify_release(fetch_release())
        if latest["version"] != candidate.get("version") or latest["source_sha"] != candidate.get("source_sha") or latest["platforms"] != candidate.get("platforms"):
            raise ValueError("RCC release candidate was superseded before promotion")
        latest_jat = fetch_latest_jat_candidate({
            "version": candidate["version"],
            "source_sha": candidate["source_sha"],
            "platforms": candidate["platforms"],
        })
        if any(latest_jat.get(key) != verified_jat.get(key) for key in (
            "release_tag", "source_sha", "rcc_version", "rcc_source_sha", "rcc_platforms", "environment_artifacts"
        )):
            raise ValueError("JAT runtime candidate was superseded before promotion")
    receipts = _load_receipts(artifacts_root, candidate, source_sha)
    input_identity = "-".join(receipts[platform]["receipt"]["input_digest"].split(":", 1)[1] for platform in PLATFORM_ASSETS)
    release_tag = f"rcc-{candidate['version']}-controller-{source_sha}-{input_identity}"
    changes = _build_update(root, candidate, receipts, source_sha, release_tag, verified_jat)
    if not changes:
        return {"status": "PASS", "no_change": True, "changed_files": [], "candidate": candidate["version"]}
    release_version = _next_extension_version(root)
    merged_changes = {path: data for path, data in changes}
    merged_changes.update(_version_update(root, release_version, changes))
    changes = [(path, data) for path, data in merged_changes.items()]
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
        "jat_release_tag": verified_jat["release_tag"],
        "jat_source_sha": verified_jat["source_sha"],
        "jat_source_archive": verified_jat["source_archive"],
        "jat_source_handoff": verified_jat["source_handoff"],
        "release_version": release_version,
        "jat_rcc_version": verified_jat["rcc_version"],
        "platforms": {platform: items["receipt"] for platform, items in receipts.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--jat-candidate", type=Path)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--existing-release-json", type=Path)
    parser.add_argument("--tag-target-sha", help="peeled Git tag target resolved from origin")
    parser.add_argument("--downloads", type=Path)
    parser.add_argument("--release-tag")
    parser.add_argument("--plan-only", action="store_true", help="validate and report intended changes without writing consumer files")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    candidate_report = _read_json(args.candidate)
    candidate = candidate_report.get("candidate") or candidate_report
    jat_candidate = _read_json(args.jat_candidate) if args.jat_candidate else None
    report: dict[str, Any] = {"status": "BLOCKED"}
    try:
        receipts = _load_receipts(args.artifacts.resolve(), candidate, args.source_sha)
        if args.existing_release_json or args.downloads:
            verified_jat = None
        elif jat_candidate is None:
            raise ValueError("--jat-candidate is required to prepare paired controller and JAT runtime pins")
        else:
            verified_jat = _validate_jat_candidate(root, candidate, jat_candidate)
        if args.existing_release_json:
            if not args.release_tag:
                raise ValueError("--release-tag is required to verify a published release")
            if not args.tag_target_sha:
                raise ValueError("--tag-target-sha is required to verify a published release")
            release = _read_json(args.existing_release_json)
            missing = verify_existing_release(
                release,
                tag=args.release_tag,
                source_sha=args.source_sha,
                tag_target_sha=args.tag_target_sha,
                receipts=receipts,
            )
            report.update(status="PASS", missing_assets=missing)
        elif args.downloads:
            verify_downloaded_assets(args.downloads.resolve(), receipts)
            report.update(status="PASS", downloaded_assets="VERIFIED")
        else:
            assert verified_jat is not None
            report.update(promote_candidate(
                root,
                candidate,
                args.artifacts.resolve(),
                args.source_sha,
                jat_candidate=verified_jat,
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
