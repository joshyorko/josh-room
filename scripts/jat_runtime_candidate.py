#!/usr/bin/env python3
"""Verify the latest immutable JAT runtime release against an RCC candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

JAT_OWNER = "joshyorko"
JAT_REPO = "josh-all-the-things"
JAT_API = f"https://api.github.com/repos/{JAT_OWNER}/{JAT_REPO}"
JAT_RELEASE_LINE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)-jat-runtime$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CONTENT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PLATFORMS = {
    "linux-x64": {
        "receipt_platform": "linux_amd64",
        "release_archive": "jat-runtime-linux-amd64.rcca",
        "receipt": "jat-runtime-linux-amd64.json",
        "rcc_asset": "rcc-linux64",
    },
    "win32-x64": {
        "receipt_platform": "windows_amd64",
        "release_archive": "jat-runtime-windows-amd64.rcca",
        "receipt": "jat-runtime-windows-amd64.json",
        "rcc_asset": "rcc-windows64.exe",
    },
}
REQUIRED_RECEIPT_GATES = {
    "verified_acquire": {"fresh_home": True, "no_build": True},
    "verified_no_build": {"fresh_home": True, "no_build": True},
    "verified_exec": {"fresh_home": True},
    "verified_warm_reuse": {"fresh_home": True, "no_build": True, "provider_unavailable": True},
    "verified_hauler": {"fresh_home": True, "resolved_under_conda_prefix": True, "exit_code": 0},
}


def _request_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "josh-room-rcc-maintenance",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "josh-room-rcc-maintenance"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _release_candidates(releases: list[dict[str, Any]]) -> list[tuple[tuple[int, int, int], dict[str, Any]]]:
    candidates = []
    for release in releases:
        match = JAT_RELEASE_LINE.fullmatch(str(release.get("tag_name", "")))
        if match and release.get("draft") is False and release.get("prerelease") is True:
            candidates.append((tuple(map(int, match.groups())), release))
    return candidates


def select_latest_release(releases: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = _release_candidates(releases)
    if not candidates:
        raise RuntimeError("no eligible published JAT runtime release found")
    highest = max(version for version, _release in candidates)
    winners = [release for version, release in candidates if version == highest]
    if len(winners) != 1:
        raise ValueError("latest JAT runtime release version is ambiguous")
    return winners[0]


def _parse_tag_version(tag: str) -> tuple[int, int, int]:
    match = JAT_RELEASE_LINE.fullmatch(tag)
    if not match:
        raise ValueError("JAT runtime release tag is outside the approved tag format")
    return tuple(map(int, match.groups()))


def _resolve_tag_commit(tag: str) -> str:
    reference = _request_json(f"{JAT_API}/git/ref/tags/{tag}")
    target = reference.get("object", {})
    if target.get("type") == "tag":
        target = _request_json(f"{JAT_API}/git/tags/{target.get('sha')}").get("object", {})
    commit = target.get("sha")
    if target.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise ValueError("JAT runtime release tag does not resolve to a commit")
    return commit


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _asset_index(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise TypeError("JAT release assets must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise TypeError("JAT release contains malformed asset metadata")
        name = asset["name"]
        if name in indexed:
            raise ValueError(f"JAT release has duplicate asset name: {name}")
        indexed[name] = asset
    return indexed


def _verify_payload(asset: dict[str, Any], payload: bytes, *, label: str) -> None:
    expected = asset.get("digest")
    if not isinstance(expected, str) or not CONTENT_DIGEST.fullmatch(expected):
        raise ValueError(f"{label} has no trusted GitHub SHA256 asset digest")
    size = asset.get("size")
    if type(size) is not int or size < 1:
        raise ValueError(f"{label} has no valid GitHub asset size")
    if hashlib.sha256(payload).hexdigest() != expected.removeprefix("sha256:") or len(payload) != size:
        raise ValueError(f"{label} differs from GitHub release metadata")


def _decode_asset(name: str, assets: dict[str, dict[str, Any]], payloads: dict[str, bytes]) -> dict[str, Any]:
    asset = assets.get(name)
    if asset is None:
        raise ValueError(f"required JAT release asset is missing: {name}")
    if name not in payloads:
        raise ValueError(f"required JAT receipt payload was not downloaded: {name}")
    payload = payloads[name]
    _verify_payload(asset, payload, label=name)
    value = json.loads(payload)
    return _object(value, name)


def _validate_digest(value: Any, label: str, *, prefixed: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a SHA256 digest")
    pattern = CONTENT_DIGEST if prefixed else SHA256
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} must be a SHA256 digest")
    return value


def _validate_receipt_gates(receipt: dict[str, Any], label: str) -> None:
    for gate, expected in REQUIRED_RECEIPT_GATES.items():
        observed = receipt.get(gate)
        if not isinstance(observed, dict) or any(observed.get(field) is not value for field, value in expected.items()):
            raise ValueError(f"{label} is missing a passing {gate} qualification")
    tasks = receipt.get("verified_tasks")
    if tasks is not None and (not isinstance(tasks, dict) or any(value is not True for value in tasks.values())):
        raise ValueError(f"{label} contains a failing JAT task qualification")


def verify_release(
    release: dict[str, Any],
    *,
    release_commit_sha: str,
    expected_rcc: dict[str, Any],
    asset_payloads: dict[str, bytes],
    source_archive_payload: bytes,
) -> dict[str, Any]:
    release = _object(release, "JAT runtime release")
    tag = str(release.get("tag_name", ""))
    _parse_tag_version(tag)
    if release.get("draft") is not False or release.get("prerelease") is not True:
        raise ValueError("latest JAT runtime release is not a published prerelease")
    if not re.fullmatch(r"[0-9a-f]{40}", release_commit_sha):
        raise ValueError("JAT release tag does not resolve to the recorded JAT source")
    source_archive = {
        "asset": f"josh-all-the-things-{release_commit_sha}.tar.gz",
        "url": f"https://api.github.com/repos/{JAT_OWNER}/{JAT_REPO}/tarball/{release_commit_sha}",
        "sha256": hashlib.sha256(source_archive_payload).hexdigest(),
        "size": len(source_archive_payload),
    }

    expected_rcc = _object(expected_rcc, "RCC candidate")
    rcc_version = expected_rcc.get("version")
    rcc_source_sha = expected_rcc.get("source_sha")
    rcc_platforms = _object(expected_rcc.get("platforms"), "RCC platform pins")
    if not isinstance(rcc_version, str) or not re.fullmatch(r"v18\.19\.\d+", rcc_version):
        raise ValueError("RCC candidate version is outside the approved v18.19.x line")
    if not isinstance(rcc_source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", rcc_source_sha):
        raise ValueError("RCC candidate has no exact source commit")
    if set(rcc_platforms) != set(PLATFORMS):
        raise ValueError("RCC candidate does not contain both supported platforms")

    assets = _asset_index(release)
    provenance = _decode_asset("jat-runtime-provenance.json", assets, asset_payloads)
    if provenance.get("format_version") != 1 or provenance.get("release_tag") != tag:
        raise ValueError("JAT runtime provenance does not match the selected release tag")
    if provenance.get("jat_git_sha") != release_commit_sha:
        raise ValueError("JAT release tag does not resolve to the recorded JAT source")
    if provenance.get("rcc_version") != rcc_version:
        raise ValueError(f"JAT runtime RCC version differs from the controller candidate {rcc_version}")
    if not isinstance(provenance.get("workflow_run"), dict) or type(provenance["workflow_run"].get("id")) is not int:
        raise ValueError("JAT runtime provenance has no producer workflow run")

    provenance_platforms = _object(provenance.get("platforms"), "JAT provenance platforms")
    environment_artifacts: dict[str, dict[str, Any]] = {}
    for platform, details in PLATFORMS.items():
        rcc_pin = _object(rcc_platforms.get(platform), f"RCC {platform} pin")
        rcc_sha256 = _validate_digest(rcc_pin.get("sha256"), f"RCC {platform} SHA256")
        if rcc_pin.get("asset") != details["rcc_asset"]:
            raise ValueError(f"RCC candidate has the wrong {platform} asset")

        receipt = _decode_asset(details["receipt"], assets, asset_payloads)
        if receipt.get("format_version") != 3 or receipt.get("operation") != "build" or receipt.get("success") is not True:
            raise ValueError(f"{platform} JAT receipt is not a successful runtime build")
        if receipt.get("platform") != details["receipt_platform"]:
            raise ValueError(f"JAT receipt platform does not match {platform}")
        if receipt.get("jat_git_sha") != release_commit_sha:
            raise ValueError(f"{platform} JAT receipt is stale for the selected release tag")
        if receipt.get("rcc_version") != rcc_version:
            raise ValueError(f"{platform} JAT receipt RCC version differs from the controller candidate {rcc_version}")

        rcc_source = _object(receipt.get("rcc_source"), f"{platform} RCC provenance")
        if (
            rcc_source.get("repository") != "https://github.com/joshyorko/rcc"
            or rcc_source.get("tag") != rcc_version
            or rcc_source.get("commit") != rcc_source_sha
            or rcc_source.get("asset") != details["rcc_asset"]
            or rcc_source.get("sha256") != rcc_sha256
        ):
            raise ValueError(f"{platform} JAT receipt RCC provenance differs from the verified RCC candidate")

        _validate_receipt_gates(receipt, platform)
        artifact_digest = _validate_digest(receipt.get("artifact_digest"), f"{platform} artifact digest", prefixed=True)
        specification_digest = _validate_digest(receipt.get("specification_digest"), f"{platform} specification digest", prefixed=True)
        receipt_archive = _object(receipt.get("archive"), f"{platform} archive receipt")
        archive_sha = _validate_digest(receipt_archive.get("sha256"), f"{platform} archive SHA256")
        archive_size = receipt_archive.get("size")
        if type(archive_size) is not int or archive_size < 1:
            raise ValueError(f"{platform} archive receipt has an invalid size")
        archive_asset = assets.get(details["release_archive"])
        if archive_asset is None:
            raise ValueError(f"required JAT release archive is missing: {details['release_archive']}")
        if archive_asset.get("digest") != f"sha256:{archive_sha}" or archive_asset.get("size") != archive_size:
            raise ValueError(f"{platform} archive asset metadata differs from its immutable receipt")

        provenance_platform = _object(provenance_platforms.get(details["receipt_platform"]), f"{platform} provenance")
        provenance_archive = _object(provenance_platform.get("archive"), f"{platform} provenance archive")
        if (
            provenance_platform.get("receipt") != details["receipt"]
            or provenance_archive.get("asset") != details["release_archive"]
            or provenance_archive.get("sha256") != archive_sha
            or provenance_archive.get("size") != archive_size
            or provenance_platform.get("artifact_digest") != artifact_digest
            or provenance_platform.get("specification_digest") != specification_digest
        ):
            raise ValueError(f"{platform} JAT provenance does not match its receipt")

        environment_artifacts[platform] = {
            "digest": artifact_digest,
            "specification_digest": specification_digest,
            "archive": {
                "asset": details["release_archive"],
                "url": f"https://github.com/{JAT_OWNER}/{JAT_REPO}/releases/download/{tag}/{details['release_archive']}",
                "sha256": archive_sha,
                "size": archive_size,
            },
        }

    return {
        "repository": f"https://github.com/{JAT_OWNER}/{JAT_REPO}",
        "release_tag": tag,
        "source_sha": release_commit_sha,
        "source_archive": source_archive,
        "rcc_version": rcc_version,
        "rcc_source_sha": rcc_source_sha,
        "rcc_platforms": rcc_platforms,
        "environment_artifact": environment_artifacts["linux-x64"],
        "environment_artifacts": environment_artifacts,
        "workflow_run": provenance["workflow_run"],
    }


def validate_candidate(candidate: dict[str, Any], *, expected_rcc: dict[str, Any]) -> dict[str, Any]:
    candidate = _object(candidate, "JAT runtime candidate")
    if candidate.get("status") != "QUALIFIED":
        raise ValueError("JAT runtime candidate is not marked QUALIFIED")
    if candidate.get("repository") != f"https://github.com/{JAT_OWNER}/{JAT_REPO}":
        raise ValueError("JAT runtime candidate belongs to an unexpected producer repository")
    tag = candidate.get("release_tag")
    _parse_tag_version(str(tag))
    if not isinstance(candidate.get("source_sha"), str) or not re.fullmatch(r"[0-9a-f]{40}", candidate["source_sha"]):
        raise ValueError("JAT runtime candidate has no exact source commit")
    source_sha = candidate["source_sha"]
    source_archive = _object(candidate.get("source_archive"), "JAT source archive receipt")
    expected_source_archive = {
        "asset": f"josh-all-the-things-{source_sha}.tar.gz",
        "url": f"https://api.github.com/repos/{JAT_OWNER}/{JAT_REPO}/tarball/{source_sha}",
    }
    if any(source_archive.get(key) != value for key, value in expected_source_archive.items()):
        raise ValueError("JAT source archive is not bound to the verified producer commit")
    _validate_digest(source_archive.get("sha256"), "JAT source archive SHA256")
    if type(source_archive.get("size")) is not int or source_archive["size"] < 1:
        raise ValueError("JAT source archive has an invalid size")
    if candidate.get("rcc_version") != expected_rcc.get("version"):
        raise ValueError("JAT runtime candidate RCC version differs from the controller candidate")
    if candidate.get("rcc_source_sha") != expected_rcc.get("source_sha"):
        raise ValueError("JAT runtime candidate RCC source differs from the verified controller candidate")
    if candidate.get("rcc_platforms") != expected_rcc.get("platforms"):
        raise ValueError("JAT runtime candidate RCC platform pins differ from the verified controller candidate")
    artifacts = _object(candidate.get("environment_artifacts"), "JAT platform artifacts")
    if set(artifacts) != set(PLATFORMS):
        raise ValueError("JAT runtime candidate is missing a supported platform")
    for platform, details in PLATFORMS.items():
        artifact = _object(artifacts[platform], f"JAT {platform} artifact")
        _validate_digest(artifact.get("digest"), f"JAT {platform} artifact digest", prefixed=True)
        _validate_digest(artifact.get("specification_digest"), f"JAT {platform} specification digest", prefixed=True)
        archive = _object(artifact.get("archive"), f"JAT {platform} archive")
        if archive.get("asset") != details["release_archive"]:
            raise ValueError(f"JAT {platform} archive asset is unexpected")
        expected_url = f"https://github.com/{JAT_OWNER}/{JAT_REPO}/releases/download/{tag}/{details['release_archive']}"
        if archive.get("url") != expected_url:
            raise ValueError(f"JAT {platform} archive URL is not bound to the verified release tag")
        _validate_digest(archive.get("sha256"), f"JAT {platform} archive SHA256")
        if type(archive.get("size")) is not int or archive["size"] < 1:
            raise ValueError(f"JAT {platform} archive size is invalid")
    if candidate.get("environment_artifact") != artifacts["linux-x64"]:
        raise ValueError("selected JAT artifact does not match the Linux platform receipt")
    return candidate


def fetch_latest_candidate(expected_rcc: dict[str, Any]) -> dict[str, Any]:
    releases: list[dict[str, Any]] = []
    for page in range(1, 101):
        page_value = _request_json(f"{JAT_API}/releases?per_page=100&page={page}")
        if not isinstance(page_value, list):
            raise TypeError("JAT releases API did not return a list")
        releases.extend(page_value)
        if len(page_value) < 100:
            break
    else:
        raise RuntimeError("JAT release discovery exceeded the pagination safety limit")
    selected = select_latest_release(releases)
    tag = selected["tag_name"]
    payloads = {}
    assets = _asset_index(selected)
    for name in (
        "jat-runtime-provenance.json",
        "jat-runtime-linux-amd64.json",
        "jat-runtime-windows-amd64.json",
    ):
        asset = assets.get(name)
        if asset is None or not isinstance(asset.get("browser_download_url"), str):
            raise ValueError(f"required JAT release asset is missing: {name}")
        payloads[name] = _download(asset["browser_download_url"])
    release_commit_sha = _resolve_tag_commit(tag)
    source_archive_url = f"https://api.github.com/repos/{JAT_OWNER}/{JAT_REPO}/tarball/{release_commit_sha}"
    return verify_release(
        selected,
        release_commit_sha=release_commit_sha,
        expected_rcc=expected_rcc,
        asset_payloads=payloads,
        source_archive_payload=_download(source_archive_url),
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rcc-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"repository": f"{JAT_OWNER}/{JAT_REPO}", "status": "BLOCKED"}
    try:
        rcc_report = json.loads(args.rcc_report.read_text(encoding="utf-8"))
        if not isinstance(rcc_report, dict) or rcc_report.get("status") != "RELEASE_VERIFIED":
            raise ValueError("the controller RCC candidate is not fully verified")
        expected_rcc = {
            key: rcc_report[key]
            for key in ("version", "source_sha", "platforms")
        }
        candidate = fetch_latest_candidate(expected_rcc)
        report.update(candidate)
        report["status"] = "QUALIFIED"
    except Exception as error:  # noqa: BLE001 - preserve the blocked report for every failure.
        report["error"] = f"{type(error).__name__}: {error}"
    _atomic_write_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "QUALIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
