from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request

import pytest

from scripts import rcc_maintenance as maintenance


def _release_fixture():
    tag = "v18.19.6"
    assets = {}
    bodies = {}
    index = {"tested": [{
        "version": tag,
        "linux": f"https://github.com/joshyorko/rcc/releases/download/{tag}/rcc-linux64",
        "windows": f"https://github.com/joshyorko/rcc/releases/download/{tag}/rcc-windows64.exe",
    }]}
    bodies["index.json"] = json.dumps(index).encode()
    bodies["rcc-linux64"] = b"synthetic linux RCC asset"
    bodies["rcc-windows64.exe"] = b"synthetic windows RCC asset"
    for name, body in bodies.items():
        assets[name] = {
            "name": name,
            "browser_download_url": f"https://github.com/joshyorko/rcc/releases/download/{tag}/{name}",
            "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            "size": len(body),
        }
    release = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-12T16:17:45Z",
        "target_commitish": "main",
        "assets": list(assets.values()),
    }
    return release, bodies


def test_release_candidate_selection_is_stable_and_allowlisted():
    releases = [
        {"tag_name": "v18.19.6", "draft": False, "prerelease": False},
        {"tag_name": "v18.19.7-rc.1", "draft": False, "prerelease": True},
        {"tag_name": "v18.20.0", "draft": False, "prerelease": False},
        {"tag_name": "v17.19.99", "draft": False, "prerelease": False},
        {"tag_name": "v18.19.8", "draft": True, "prerelease": False},
    ]
    candidates = maintenance._release_candidates(releases)
    assert candidates == [(6, releases[0])]


def test_github_api_request_uses_workflow_token_without_printing_it(monkeypatch):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok": true}'

    def open_request(request, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setenv("GH_TOKEN", "fixture-token-do-not-print")
    monkeypatch.setattr(maintenance.urllib.request, "urlopen", open_request)

    assert maintenance._request_json("https://api.github.com/repos/joshyorko/rcc/releases") == {"ok": True}
    assert isinstance(observed["request"], Request)
    assert observed["request"].get_header("Authorization") == "Bearer fixture-token-do-not-print"
    assert observed["timeout"] == 30


def test_release_verification_binds_tag_index_assets_and_exact_checksums(monkeypatch):
    release, bodies = _release_fixture()
    monkeypatch.setattr(maintenance, "resolve_tag_commit", lambda _tag: "a" * 40)

    def download(url, *, keep_bytes=False):
        name = url.rsplit("/", 1)[-1]
        body = bodies[name]
        return hashlib.sha256(body).hexdigest(), len(body), body if keep_bytes else None

    monkeypatch.setattr(maintenance, "_download", download)
    candidate = maintenance.verify_release(release)

    assert candidate["version"] == "v18.19.6"
    assert candidate["source_sha"] == "a" * 40
    assert candidate["platforms"]["linux-x64"]["sha256"] == hashlib.sha256(bodies["rcc-linux64"]).hexdigest()
    assert candidate["platforms"]["win32-x64"]["size"] == len(bodies["rcc-windows64.exe"])
    assert candidate["evidence"]["linux-x64"]["status"] == "PASS"


def test_release_verification_rejects_metadata_checksum_mismatch(monkeypatch):
    release, bodies = _release_fixture()
    release["assets"][1]["digest"] = "sha256:" + "0" * 64
    monkeypatch.setattr(maintenance, "resolve_tag_commit", lambda _tag: "a" * 40)
    monkeypatch.setattr(
        maintenance,
        "_download",
        lambda url, *, keep_bytes=False: (
            hashlib.sha256(bodies[url.rsplit("/", 1)[-1]]).hexdigest(),
            len(bodies[url.rsplit("/", 1)[-1]]),
            bodies[url.rsplit("/", 1)[-1]] if keep_bytes else None,
        ),
    )
    with pytest.raises(ValueError, match="differs from GitHub release metadata"):
        maintenance.verify_release(release)


def test_candidate_manifest_is_written_separately_from_consumer_pins(tmp_path: Path):
    source = tmp_path / "vscode-extension/runtime/controller-artifact-manifest.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"schema_version": 1, "rcc": {"version": "v18.19.5"}, "controller": {"robot": "controller/robot.yaml"}}))
    candidate = {
        "version": "v18.19.6",
        "platforms": {
            "linux-x64": {"asset": "rcc-linux64", "url": "https://example.test/linux", "sha256": "1" * 64, "size": 11},
            "win32-x64": {"asset": "rcc-windows64.exe", "url": "https://example.test/windows", "sha256": "2" * 64, "size": 12},
        },
    }
    destination = tmp_path / "candidate" / "manifest.json"

    maintenance.write_candidate_manifest(tmp_path, candidate, destination)

    written = json.loads(destination.read_text())
    assert written["rcc"]["version"] == "v18.19.6"
    assert written["rcc"]["platforms"]["linux-x64"]["sha256"] == "1" * 64
    assert json.loads(source.read_text())["rcc"]["version"] == "v18.19.5"
