#!/usr/bin/env python3
"""Validate exact-source CI and coherent controller/JAT pins before release staging."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

CI_REQUIRED_CHECKS = frozenset(
    {
        "test",
        "windows-private-runtime",
        "managed-runtime-consumer (ubuntu-latest)",
        "managed-runtime-consumer (windows-latest)",
    }
)
PLATFORMS = {
    "linux-x64": "linux_amd64",
    "win32-x64": "windows_amd64",
}
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


def validate_green_checks(check_runs: list[dict[str, Any]], *, source_sha: str) -> dict[str, Any]:
    if not SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("release source must be a full lowercase Git SHA")
    if not isinstance(check_runs, list):
        raise TypeError("GitHub check runs must be a list")
    latest_by_name: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        if not isinstance(run, dict):
            raise TypeError("GitHub check run entries must be objects")
        name = run.get("name")
        if name not in CI_REQUIRED_CHECKS:
            continue
        if run.get("head_sha") != source_sha:
            # A stale rerun at an unrelated SHA cannot satisfy this source.
            continue
        previous = latest_by_name.get(name)
        ordering = (str(run.get("started_at", "")), int(run.get("id", 0)))
        previous_ordering = (
            str(previous.get("started_at", "")), int(previous.get("id", 0))
        ) if previous else ("", 0)
        if previous is None or ordering > previous_ordering:
            latest_by_name[name] = run

    missing = CI_REQUIRED_CHECKS - latest_by_name.keys()
    if missing:
        raise ValueError(f"exact-source CI is missing required checks: {', '.join(sorted(missing))}")
    failed = []
    report = {}
    for name in sorted(CI_REQUIRED_CHECKS):
        run = latest_by_name[name]
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            failed.append(name)
        report[name] = {
            "id": run.get("id"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "head_sha": run.get("head_sha"),
        }
    if failed:
        raise ValueError(f"exact-source CI is not fully green: {', '.join(failed)}")
    return {"source_sha": source_sha, "checks": report}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _sha(value: Any, label: str, *, prefixed: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a SHA256 digest")
    if prefixed:
        if not value.startswith("sha256:") or not SHA256.fullmatch(value.removeprefix("sha256:")):
            raise ValueError(f"{label} must be a SHA256 digest")
    elif not SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a SHA256 digest")
    return value


def _check_archive(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    archive = _object(actual.get("archive"), f"{label} archive")
    expected = _object(expected, f"{label} release lock")
    if archive.get("sha256") != expected.get("archive_sha256"):
        raise ValueError(f"{label} archive digest differs from the release lock")
    if archive.get("size") != expected.get("archive_size"):
        raise ValueError(f"{label} archive size differs from the release lock")


def validate_runtime_compatibility(runtime: dict[str, Any], release_lock: dict[str, Any]) -> dict[str, Any]:
    runtime = _object(runtime, "runtime manifest")
    release_lock = _object(release_lock, "release lock")
    controller_rcc = _object(runtime.get("rcc"), "controller RCC pin")
    jat = _object(runtime.get("jat"), "JAT runtime pin")
    jat_rcc = _object(jat.get("rcc"), "JAT RCC pin")
    controller_version = controller_rcc.get("version")
    jat_version = jat_rcc.get("version")
    if controller_version != jat_version:
        raise ValueError("controller and JAT RCC pins are not paired")

    controller_platforms = _object(controller_rcc.get("platforms"), "controller RCC platforms")
    jat_platforms = _object(jat_rcc.get("platforms"), "JAT RCC platforms")
    if set(controller_platforms) != set(PLATFORMS) or jat_platforms != controller_platforms:
        raise ValueError("controller and JAT RCC platform pins are not paired")
    for platform, expected_asset in (("linux-x64", "rcc-linux64"), ("win32-x64", "rcc-windows64.exe")):
        pin = _object(controller_platforms.get(platform), f"controller RCC {platform}")
        _sha(pin.get("sha256"), f"controller RCC {platform} checksum")
        if pin.get("asset") != expected_asset:
            raise ValueError(f"controller RCC {platform} asset is unexpected")
    lock_rcc = _object(release_lock.get("rcc"), "release-lock RCC pin")
    if lock_rcc.get("version") != controller_version or lock_rcc.get("platforms") != controller_platforms:
        raise ValueError("release-lock controller RCC pins differ from the runtime manifest")
    rcc_source_sha = lock_rcc.get("source_sha")
    if not isinstance(rcc_source_sha, str) or not SOURCE_SHA.fullmatch(rcc_source_sha):
        raise ValueError("release-lock RCC pin has no exact source commit")
    lock_jat = _object(release_lock.get("jat"), "release-lock JAT pin")
    lock_jat_rcc = _object(lock_jat.get("rcc"), "release-lock JAT RCC pin")
    if lock_jat_rcc.get("version") != jat_version or lock_jat_rcc.get("platforms") != jat_platforms:
        raise ValueError("release-lock JAT RCC pins differ from the runtime manifest")
    jat_rcc_source_sha = jat_rcc.get("source_sha")
    if not isinstance(jat_rcc_source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", jat_rcc_source_sha):
        raise ValueError("JAT RCC pin has no exact verified RCC source commit")
    if jat_rcc_source_sha != rcc_source_sha or lock_jat_rcc.get("source_sha") != rcc_source_sha:
        raise ValueError("controller and JAT RCC receipts do not identify the same source commit")

    controller = _object(runtime.get("controller"), "controller artifact pins")
    controller_artifacts = _object(controller.get("environment_artifacts"), "controller platform artifacts")
    lock_controller = _object(release_lock.get("controller"), "release-lock controller artifacts")
    lock_controller_platforms = _object(lock_controller.get("platforms"), "release-lock controller platforms")
    controller_source_sha = lock_controller.get("source_sha")
    if not isinstance(controller_source_sha, str) or not SOURCE_SHA.fullmatch(controller_source_sha):
        raise ValueError("release-lock controller artifacts have no exact source commit")
    if lock_controller.get("rcc_version") != controller_version or lock_controller.get("rcc_source_sha") != rcc_source_sha:
        raise ValueError("release-lock controller artifacts differ from the paired RCC runtime")
    if set(controller_artifacts) != set(PLATFORMS) or set(lock_controller_platforms) != set(PLATFORMS):
        raise ValueError("controller artifacts are missing a supported platform")
    for platform in PLATFORMS:
        artifact = _object(controller_artifacts[platform], f"controller artifact {platform}")
        locked = _object(lock_controller_platforms[platform], f"release-lock controller {platform}")
        _sha(artifact.get("digest"), f"controller {platform} artifact digest", prefixed=True)
        _sha(artifact.get("specification_digest"), f"controller {platform} specification digest", prefixed=True)
        if artifact.get("source_sha") != controller_source_sha:
            raise ValueError(f"controller {platform} artifact source differs from the release lock")
        if artifact.get("rcc_version") != controller_version:
            raise ValueError(f"controller {platform} artifact RCC version differs from the paired runtime")
        if artifact.get("rcc_sha256") != controller_platforms[platform].get("sha256"):
            raise ValueError(f"controller {platform} artifact RCC checksum differs from the runtime pin")
        archive = _object(artifact.get("archive"), f"controller {platform} archive")
        expected_asset = "josh-room-controller-linux-amd64.rcca" if platform == "linux-x64" else "josh-room-controller-windows-amd64.rcca"
        expected_url = (
            f"https://github.com/joshyorko/josh-room/releases/download/"
            f"{lock_controller.get('release_tag')}/{expected_asset}"
        )
        if archive.get("asset") != expected_asset or archive.get("url") != expected_url:
            raise ValueError(f"controller {platform} archive URL is not bound to its release tag")
        _check_archive(artifact, locked, f"controller {platform}")
        if (
            artifact.get("digest") != locked.get("artifact_digest")
            or artifact.get("specification_digest") != locked.get("specification_digest")
        ):
            raise ValueError(f"controller {platform} digests differ from the release lock")

    jat_artifacts = _object(jat.get("environment_artifacts"), "JAT platform artifacts")
    lock_jat_artifacts = _object(lock_jat.get("environment_artifacts"), "release-lock JAT platform artifacts")
    if set(jat_artifacts) != set(PLATFORMS) or set(lock_jat_artifacts) != set(PLATFORMS.values()):
        raise ValueError("JAT artifacts are missing a supported platform")
    for platform, lock_platform in PLATFORMS.items():
        artifact = _object(jat_artifacts[platform], f"JAT artifact {platform}")
        locked = _object(lock_jat_artifacts[lock_platform], f"release-lock JAT {platform}")
        _sha(artifact.get("digest"), f"JAT {platform} artifact digest", prefixed=True)
        _sha(artifact.get("specification_digest"), f"JAT {platform} specification digest", prefixed=True)
        archive = _object(artifact.get("archive"), f"JAT {platform} archive")
        if not isinstance(archive.get("url"), str) or not archive["url"].startswith(
            "https://github.com/joshyorko/josh-all-the-things/releases/download/"
        ):
            raise ValueError(f"JAT {platform} archive URL is outside its producer repository")
        if artifact.get("rcc_version") != jat_version or artifact.get("rcc_source_sha") != jat_rcc_source_sha:
            raise ValueError(f"JAT {platform} artifact receipt is not bound to the paired RCC runtime")
        _check_archive(artifact, locked, f"JAT {platform}")
        if (
            artifact.get("digest") != locked.get("rcc_artifact_digest")
            or artifact.get("specification_digest") != locked.get("rcc_specification_digest")
        ):
            raise ValueError(f"JAT {platform} digests differ from the release lock")
        if locked.get("rcc_version") != jat_version or locked.get("rcc_source_sha") != jat_rcc_source_sha:
            raise ValueError(f"release-lock JAT {platform} RCC source differs from its receipt")
    if jat.get("release_tag") != lock_jat.get("release_tag"):
        raise ValueError("JAT runtime release tag differs from the release lock")
    primary_lock = _object(lock_jat.get("environment_artifact"), "release-lock selected JAT artifact")
    linux_artifact = jat_artifacts["linux-x64"]
    if (
        primary_lock.get("release_tag") != jat.get("release_tag")
        or primary_lock.get("rcc_version") != jat_version
        or primary_lock.get("rcc_source_sha") != jat_rcc_source_sha
        or primary_lock.get("archive_url") != linux_artifact["archive"].get("url")
        or primary_lock.get("archive_sha256") != linux_artifact["archive"].get("sha256")
        or primary_lock.get("archive_size") != linux_artifact["archive"].get("size")
        or primary_lock.get("rcc_artifact_digest") != linux_artifact.get("digest")
        or primary_lock.get("rcc_specification_digest") != linux_artifact.get("specification_digest")
    ):
        raise ValueError("release-lock selected JAT artifact differs from the Linux receipt")
    if jat.get("environment_artifact") != jat_artifacts["linux-x64"]:
        raise ValueError("the selected JAT artifact does not match the Linux platform pin")
    if lock_jat.get("git_sha") != jat.get("git_sha"):
        raise ValueError("JAT source SHA differs from the release lock")
    jat_source_sha = jat.get("git_sha")
    if not isinstance(jat_source_sha, str) or not SOURCE_SHA.fullmatch(jat_source_sha):
        raise ValueError("JAT runtime pin has no exact producer source commit")
    source_archive = _object(jat.get("source_archive"), "JAT source archive receipt")
    expected_source_archive = (
        f"https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/{jat_source_sha}"
    )
    if (
        source_archive.get("asset") != f"josh-all-the-things-{jat_source_sha}.tar.gz"
        or source_archive.get("url") != expected_source_archive
    ):
        raise ValueError("JAT source archive is not bound to the pinned producer commit")
    _sha(source_archive.get("sha256"), "JAT source archive checksum")
    if type(source_archive.get("size")) is not int or source_archive["size"] < 1:
        raise ValueError("JAT source archive has no valid size")

    return {
        "rcc_version": controller_version,
        "rcc_source_sha": rcc_source_sha,
        "controller_source_sha": controller_source_sha,
        "jat_source_sha": jat_source_sha,
        "platforms": sorted(PLATFORMS),
    }


def validate_release_assets(
    runtime: dict[str, Any],
    release_lock: dict[str, Any],
    *,
    controller_release: dict[str, Any],
    jat_release: dict[str, Any],
    controller_tag_sha: str,
    jat_tag_sha: str,
) -> dict[str, Any]:
    """Bind public artifact metadata to the exact digests in the release lock."""
    validate_runtime_compatibility(runtime, release_lock)
    releases = {
        "controller": (controller_release, release_lock["controller"].get("release_tag")),
        "jat": (jat_release, release_lock["jat"]["environment_artifact"].get("release_tag")),
    }
    expected_tag_shas = {
        "controller": release_lock["controller"].get("source_sha"),
        "jat": release_lock["jat"].get("git_sha"),
    }
    observed_tag_shas = {"controller": controller_tag_sha, "jat": jat_tag_sha}
    for label, (release_value, expected_tag) in releases.items():
        release = _object(release_value, f"{label} release metadata")
        if release.get("draft") is not False or release.get("prerelease") is not True:
            raise ValueError(f"{label} artifact release is not a published prerelease")
        if release.get("tag_name") != expected_tag:
            raise ValueError(f"{label} artifact release tag differs from the release lock")
        if observed_tag_shas[label] != expected_tag_shas[label]:
            raise ValueError(f"{label} release tag target differs from its locked source commit")
        assets = _asset_index(release, label)
        if label == "controller":
            expected = {
                platform: _object(runtime["controller"]["environment_artifacts"][platform], f"controller {platform}")["archive"]
                for platform in PLATFORMS
            }
        else:
            expected = {
                platform: _object(runtime["jat"]["environment_artifacts"][platform], f"JAT {platform}")["archive"]
                for platform in PLATFORMS
            }
        for platform, archive in expected.items():
            archive = _object(archive, f"{label} {platform} archive")
            asset = assets.get(archive.get("asset"))
            if asset is None:
                raise ValueError(f"{label} release is missing {platform} archive asset")
            if asset.get("browser_download_url") != archive.get("url"):
                raise ValueError(f"{label} release {platform} archive URL differs from the runtime pin")
            if asset.get("digest") != f"sha256:{archive.get('sha256')}" or asset.get("size") != archive.get("size"):
                raise ValueError(f"{label} release {platform} archive differs from the release lock")
    return {"controller_release": releases["controller"][1], "jat_release": releases["jat"][1]}


def _asset_index(release: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise TypeError(f"{label} release assets must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise TypeError(f"{label} release contains malformed asset metadata")
        if asset["name"] in indexed:
            raise ValueError(f"{label} release has a duplicate asset name")
        indexed[asset["name"]] = asset
    return indexed


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_run_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        check_runs = value.get("check_runs")
        if isinstance(check_runs, list):
            return check_runs
    if isinstance(value, list):
        runs = []
        for page in value:
            if isinstance(page, dict) and isinstance(page.get("check_runs"), list):
                runs.extend(page["check_runs"])
            elif isinstance(page, list):
                runs.extend(page)
        return runs
    raise TypeError("GitHub check-run JSON has an unsupported shape")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--check-runs", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--release-lock", type=Path, required=True)
    parser.add_argument("--controller-release", type=Path)
    parser.add_argument("--jat-release", type=Path)
    parser.add_argument("--controller-tag-sha")
    parser.add_argument("--jat-tag-sha")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"status": "BLOCKED", "source_sha": args.source_sha}
    try:
        check_report = validate_green_checks(_check_run_list(_load(args.check_runs)), source_sha=args.source_sha)
        runtime = _load(args.runtime)
        lock = _load(args.release_lock)
        runtime_report = validate_runtime_compatibility(runtime, lock)
        report.update(check_report)
        report.update(runtime_report)
        release_args = (args.controller_release, args.jat_release, args.controller_tag_sha, args.jat_tag_sha)
        if any(release_args) and not all(release_args):
            raise ValueError("controller/JAT release metadata and exact tag commits must be checked together")
        if all(release_args):
            report.update(validate_release_assets(
                runtime,
                lock,
                controller_release=_load(args.controller_release),
                jat_release=_load(args.jat_release),
                controller_tag_sha=args.controller_tag_sha,
                jat_tag_sha=args.jat_tag_sha,
            ))
        report["status"] = "PASS"
    except Exception as error:  # noqa: BLE001 - preserve a reviewable blocked report.
        report["error"] = f"{type(error).__name__}: {error}"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
