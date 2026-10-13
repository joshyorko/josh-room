#!/usr/bin/env python3
"""Discover a verified allowlisted RCC release and prepare consumer pins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

OWNER = "joshyorko"
REPO = "rcc"
API_ROOT = f"https://api.github.com/repos/{OWNER}/{REPO}"
RELEASE_LINE = re.compile(r"^v18\.19\.(\d+)$")
SHA256 = re.compile(r"^sha256:([0-9a-f]{64})$")
PLATFORMS = {
    "linux-x64": {"asset": "rcc-linux64", "index_field": "linux"},
    "win32-x64": {"asset": "rcc-windows64.exe", "index_field": "windows"},
}
LOCKS = (
    Path("release-lock.json"),
    Path("templates/room/vscode-extension/runtime/release-lock.json"),
)


def _request_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "josh-room-rcc-maintenance",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _release_candidates(releases: list[dict]) -> list[tuple[int, dict]]:
    candidates = []
    for release in releases:
        match = RELEASE_LINE.fullmatch(str(release.get("tag_name", "")))
        if match and not release.get("draft") and not release.get("prerelease"):
            candidates.append((int(match.group(1)), release))
    return candidates


def fetch_release() -> dict:
    releases = _request_json(f"{API_ROOT}/releases?per_page=100")
    candidates = _release_candidates(releases)
    if not candidates:
        raise RuntimeError("no eligible stable joshyorko/rcc v18.19.x release found")
    return max(candidates, key=lambda item: item[0])[1]


def resolve_tag_commit(tag: str) -> str:
    ref = _request_json(f"{API_ROOT}/git/ref/tags/{tag}")
    target = ref.get("object", {})
    if target.get("type") == "tag":
        target = _request_json(f"{API_ROOT}/git/tags/{target.get('sha')}").get("object", {})
    commit = target.get("sha")
    if target.get("type") != "commit" or not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
        raise ValueError("RCC release tag does not resolve to a commit")
    return commit


def _download(url: str, *, keep_bytes: bool = False) -> tuple[str, int, bytes | None]:
    request = urllib.request.Request(url, headers={"User-Agent": "josh-room-rcc-maintenance"})
    digest = hashlib.sha256()
    size = 0
    body = bytearray() if keep_bytes else None
    with urllib.request.urlopen(request, timeout=120) as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if body is not None:
                body.extend(chunk)
    return digest.hexdigest(), size, bytes(body) if body is not None else None


def _expected_asset_digest(asset: dict, *, label: str) -> str:
    match = SHA256.fullmatch(str(asset.get("digest", "")))
    if not match:
        raise ValueError(f"{label} has no trusted GitHub SHA256 asset digest")
    if type(asset.get("size")) is not int or asset["size"] < 1:
        raise ValueError(f"{label} has no valid GitHub asset size")
    return match.group(1)


def _verify_asset(asset: dict, *, tag: str, name: str, keep_bytes: bool = False) -> tuple[dict, bytes | None]:
    expected_url = f"https://github.com/{OWNER}/{REPO}/releases/download/{tag}/{name}"
    url = asset.get("browser_download_url")
    if url != expected_url:
        raise ValueError(f"RCC release asset URL does not match the tagged asset {name}")
    expected_sha = _expected_asset_digest(asset, label=name)
    observed_sha, observed_size, body = _download(url, keep_bytes=keep_bytes)
    if observed_sha != expected_sha:
        raise ValueError(f"RCC release asset checksum differs from GitHub release metadata: {name}")
    if observed_size != asset["size"]:
        raise ValueError(f"RCC release asset size differs from GitHub release metadata: {name}")
    return {
        "asset": name,
        "url": url,
        "sha256": observed_sha,
        "size": observed_size,
        "release_metadata_sha256": expected_sha,
        "status": "PASS",
    }, body


def verify_release(release: dict) -> dict:
    tag = str(release.get("tag_name", ""))
    if not RELEASE_LINE.fullmatch(tag) or release.get("draft") or release.get("prerelease"):
        raise ValueError("RCC release is outside the stable v18.19.x allowlist")
    commit = resolve_tag_commit(tag)
    assets = {asset.get("name"): asset for asset in release.get("assets", [])}
    index_asset = assets.get("index.json")
    if not index_asset:
        raise RuntimeError("required RCC release provenance asset index.json is missing")
    index_receipt, index_bytes = _verify_asset(index_asset, tag=tag, name="index.json", keep_bytes=True)
    assert index_bytes is not None
    index = json.loads(index_bytes)
    indexed_versions = [item for item in index.get("tested", []) if item.get("version") == tag]
    if len(indexed_versions) != 1:
        raise ValueError("RCC release index does not identify the selected stable tag exactly once")
    indexed = indexed_versions[0]

    platform_pins: dict[str, dict] = {}
    platform_evidence: dict[str, dict] = {}
    for platform, details in PLATFORMS.items():
        name = details["asset"]
        asset = assets.get(name)
        if not asset:
            raise RuntimeError(f"required {platform} RCC asset {name} is missing")
        evidence, _unused = _verify_asset(asset, tag=tag, name=name)
        expected_index_url = f"https://github.com/{OWNER}/{REPO}/releases/download/{tag}/{name}"
        if indexed.get(details["index_field"]) != expected_index_url:
            raise ValueError(f"RCC release index does not bind {platform} to the selected release asset")
        platform_pins[platform] = {
            "asset": name,
            "url": expected_index_url,
            "sha256": evidence["sha256"],
            "size": evidence["size"],
        }
        platform_evidence[platform] = {**evidence, "version": tag}

    return {
        "version": tag,
        "source_sha": commit,
        "published_at": release.get("published_at"),
        "target_commitish": release.get("target_commitish"),
        "index": index_receipt,
        "platforms": platform_pins,
        "evidence": platform_evidence,
    }


def _atomic_write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_candidate_manifest(root: Path, candidate: dict, destination: Path) -> None:
    source = root / "vscode-extension/runtime/controller-artifact-manifest.json"
    value = json.loads(source.read_text(encoding="utf-8"))
    value.setdefault("rcc", {})["version"] = candidate["version"]
    value["rcc"]["platforms"] = candidate["platforms"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(destination, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, default=Path("artifacts/rcc-maintenance-report.json"))
    parser.add_argument("--candidate-manifest", type=Path, help="write verified RCC pins into an isolated controller manifest")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report_path = args.report if args.report.is_absolute() else root / args.report
    report: dict[str, Any] = {
        "repository": f"{OWNER}/{REPO}",
        "status": "BLOCKED",
        "platforms": {platform: {"status": "BLOCKED"} for platform in PLATFORMS},
    }
    try:
        release = fetch_release()
        candidate = verify_release(release)
        report.update(candidate)
        report["status"] = "RELEASE_VERIFIED"
        if args.candidate_manifest:
            candidate_manifest = args.candidate_manifest
            if not candidate_manifest.is_absolute():
                candidate_manifest = root / candidate_manifest
            write_candidate_manifest(root, candidate, candidate_manifest)
            report["candidate_manifest"] = str(candidate_manifest)
    except Exception as error:  # noqa: BLE001 - preserve the report on every failure.
        report["error"] = f"{type(error).__name__}: {error}"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "RELEASE_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
