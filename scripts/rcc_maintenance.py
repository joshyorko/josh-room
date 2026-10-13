#!/usr/bin/env python3
"""Discover and qualify allowlisted RCC releases for controller maintenance."""
from __future__ import annotations
import argparse, hashlib, json, re, urllib.request
from pathlib import Path
OWNER, REPO = "joshyorko", "rcc"
ALLOWLIST = re.compile(r"^v(18)\.(19)\.(\d+)$")
PLATFORMS = {"linux-x64": "rcc-linux64", "win32-x64": "rcc-windows64"}
MANIFESTS = (Path("vscode-extension/runtime/controller-artifact-manifest.json"), Path("vscode-extension/runtime/manifest.json"))
def fetch_release():
    req = urllib.request.Request("https://api.github.com/repos/joshyorko/rcc/releases", headers={"Accept": "application/vnd.github+json", "User-Agent": "josh-room-rcc-maintenance"})
    with urllib.request.urlopen(req, timeout=30) as response: releases = json.load(response)
    candidates = [(int(m.group(3)), r) for r in releases if (m := ALLOWLIST.fullmatch(r.get("tag_name", ""))) and not r.get("draft") and not r.get("prerelease")]
    if not candidates: raise RuntimeError("no eligible stable joshyorko/rcc release found")
    return max(candidates, key=lambda item: item[0])[1]
def digest(asset):
    url = asset.get("browser_download_url", "")
    if not url.startswith("https://github.com/joshyorko/rcc/releases/download/"): raise ValueError(f"asset URL is outside RCC lineage: {url}")
    with urllib.request.urlopen(url, timeout=120) as source:
        h = hashlib.sha256()
        while chunk := source.read(1024 * 1024): h.update(chunk)
    return h.hexdigest()
def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--report", type=Path, default=Path("artifacts/rcc-maintenance-report.json")); parser.add_argument("--apply", action="store_true"); args = parser.parse_args()
    report = {"repository": f"{OWNER}/{REPO}", "platforms": {}, "status": "FAIL"}
    try:
        release = fetch_release(); version = release["tag_name"]; assets = {a["name"]: a for a in release.get("assets", [])}; pins = {}
        for platform, name in PLATFORMS.items():
            asset = assets.get(name)
            if not asset: raise RuntimeError(f"required {platform} asset {name} is missing")
            sha = digest(asset); pins[platform] = {"asset": name, "url": asset["browser_download_url"], "sha256": sha, "size": asset["size"]}; report["platforms"][platform] = {"version": version, "asset": name, "sha256": sha, "size": asset["size"], "status": "PASS"}
        report.update(version=version, status="PASS")
        if args.apply:
            for path in MANIFESTS:
                document = json.loads(path.read_text()); document.setdefault("rcc", {})["version"] = version; document["rcc"]["platforms"] = pins; path.write_text(json.dumps(document, indent=2) + "\n")
            report["applied"] = [str(path) for path in MANIFESTS]
    except Exception as error:
        report["error"] = str(error)
        for platform in PLATFORMS: report["platforms"].setdefault(platform, {"status": "BLOCKED"})
    args.report.parent.mkdir(parents=True, exist_ok=True); args.report.write_text(json.dumps(report, indent=2) + "\n"); print(json.dumps(report, indent=2)); return 0 if report["status"] == "PASS" else 1
if __name__ == "__main__": raise SystemExit(main())
