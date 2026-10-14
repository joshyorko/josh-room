from __future__ import annotations

import hashlib
import json

import pytest

from scripts.jat_runtime_candidate import select_latest_release, verify_release

JAT_SHA = "a" * 40
RCC_SHA = "b" * 40
RCC_VERSION = "v18.19.6"


def _fixture():
    tag = "v0.1.12-jat-runtime"
    assets = {}
    payloads = {}
    receipts = {}
    platforms = {
        "linux-x64": ("linux_amd64", "jat-runtime-linux-amd64.rcca", "jat-runtime-linux-amd64.json", "rcc-linux64"),
        "win32-x64": ("windows_amd64", "jat-runtime-windows-amd64.rcca", "jat-runtime-windows-amd64.json", "rcc-windows64.exe"),
    }
    rcc_platforms = {
        "linux-x64": {"asset": "rcc-linux64", "url": f"https://github.com/joshyorko/rcc/releases/download/{RCC_VERSION}/rcc-linux64", "sha256": "1" * 64, "size": 100},
        "win32-x64": {"asset": "rcc-windows64.exe", "url": f"https://github.com/joshyorko/rcc/releases/download/{RCC_VERSION}/rcc-windows64.exe", "sha256": "2" * 64, "size": 200},
    }
    for platform, (receipt_platform, archive_name, receipt_name, rcc_asset) in platforms.items():
        archive_body = f"synthetic immutable {platform} JAT archive".encode()
        receipt = {
            "format_version": 3,
            "operation": "build",
            "success": True,
            "jat_git_sha": JAT_SHA,
            "rcc_version": RCC_VERSION,
            "rcc_source": {
                "repository": "https://github.com/joshyorko/rcc",
                "tag": RCC_VERSION,
                "commit": RCC_SHA,
                "asset": rcc_asset,
                "sha256": rcc_platforms[platform]["sha256"],
            },
            "platform": receipt_platform,
            "artifact_digest": "sha256:" + ("3" if platform == "linux-x64" else "4") * 64,
            "specification_digest": "sha256:" + ("5" if platform == "linux-x64" else "6") * 64,
            "archive": {"filename": archive_name, "sha256": hashlib.sha256(archive_body).hexdigest(), "size": len(archive_body)},
            "verified_acquire": {"fresh_home": True, "no_build": True},
            "verified_no_build": {"fresh_home": True, "no_build": True},
            "verified_exec": {"fresh_home": True},
            "verified_warm_reuse": {"fresh_home": True, "no_build": True, "provider_unavailable": True},
            "verified_hauler": {"fresh_home": True, "resolved_under_conda_prefix": True, "exit_code": 0},
        }
        receipt_body = json.dumps(receipt).encode()
        payloads[receipt_name] = receipt_body
        receipts[receipt_name] = receipt
        assets[archive_name] = {"name": archive_name, "digest": "sha256:" + hashlib.sha256(archive_body).hexdigest(), "size": len(archive_body)}
        assets[receipt_name] = {"name": receipt_name, "digest": "sha256:" + hashlib.sha256(receipt_body).hexdigest(), "size": len(receipt_body)}
    provenance = {
        "format_version": 1,
        "release_tag": tag,
        "jat_git_sha": JAT_SHA,
        "rcc_version": RCC_VERSION,
        "workflow_run": {"id": 12345, "url": "https://github.com/joshyorko/josh-all-the-things/actions/runs/12345"},
        "platforms": {
            "linux_amd64": {
                "receipt": "jat-runtime-linux-amd64.json",
                "archive": {"asset": "jat-runtime-linux-amd64.rcca", "sha256": assets["jat-runtime-linux-amd64.rcca"]["digest"].removeprefix("sha256:"), "size": assets["jat-runtime-linux-amd64.rcca"]["size"]},
                "artifact_digest": receipts["jat-runtime-linux-amd64.json"]["artifact_digest"],
                "specification_digest": receipts["jat-runtime-linux-amd64.json"]["specification_digest"],
            },
            "windows_amd64": {
                "receipt": "jat-runtime-windows-amd64.json",
                "archive": {"asset": "jat-runtime-windows-amd64.rcca", "sha256": assets["jat-runtime-windows-amd64.rcca"]["digest"].removeprefix("sha256:"), "size": assets["jat-runtime-windows-amd64.rcca"]["size"]},
                "artifact_digest": receipts["jat-runtime-windows-amd64.json"]["artifact_digest"],
                "specification_digest": receipts["jat-runtime-windows-amd64.json"]["specification_digest"],
            },
        },
    }
    provenance_body = json.dumps(provenance).encode()
    payloads["jat-runtime-provenance.json"] = provenance_body
    assets["jat-runtime-provenance.json"] = {"name": "jat-runtime-provenance.json", "digest": "sha256:" + hashlib.sha256(provenance_body).hexdigest(), "size": len(provenance_body)}
    release = {"tag_name": tag, "draft": False, "prerelease": True, "target_commitish": JAT_SHA, "assets": list(assets.values())}
    expected_rcc = {"version": RCC_VERSION, "source_sha": RCC_SHA, "platforms": rcc_platforms}
    return release, expected_rcc, payloads


def test_select_latest_jat_release_does_not_fall_back_to_an_older_compatible_build():
    old = {"tag_name": "v0.1.11-jat-runtime", "draft": False, "prerelease": True}
    newest = {"tag_name": "v0.1.12-jat-runtime", "draft": False, "prerelease": True}
    ignored = {"tag_name": "v0.1.13-jat-runtime", "draft": True, "prerelease": True}

    assert select_latest_release([old, newest, ignored]) is newest


def test_jat_release_requires_matching_rcc_and_two_verified_platform_receipts():
    release, expected_rcc, payloads = _fixture()

    candidate = verify_release(release, release_commit_sha=JAT_SHA, expected_rcc=expected_rcc, asset_payloads=payloads)

    assert candidate["release_tag"] == "v0.1.12-jat-runtime"
    assert candidate["source_sha"] == JAT_SHA
    assert candidate["rcc_version"] == RCC_VERSION
    assert candidate["environment_artifacts"]["linux-x64"]["digest"].startswith("sha256:")
    assert candidate["environment_artifacts"]["win32-x64"]["archive"]["sha256"] == release["assets"][2]["digest"].removeprefix("sha256:")


def test_jat_receipt_from_controller_incompatible_rcc_is_rejected():
    release, expected_rcc, payloads = _fixture()
    receipt = json.loads(payloads["jat-runtime-linux-amd64.json"])
    receipt["rcc_version"] = "v18.19.5"
    payload = json.dumps(receipt).encode()
    payloads["jat-runtime-linux-amd64.json"] = payload
    asset = next(asset for asset in release["assets"] if asset["name"] == "jat-runtime-linux-amd64.json")
    asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    asset["size"] = len(payload)

    with pytest.raises(ValueError, match="RCC version differs"):
        verify_release(release, release_commit_sha=JAT_SHA, expected_rcc=expected_rcc, asset_payloads=payloads)


def test_jat_release_archive_digest_must_match_github_release_asset_metadata():
    release, expected_rcc, payloads = _fixture()
    release["assets"][0]["digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="archive asset metadata differs"):
        verify_release(release, release_commit_sha=JAT_SHA, expected_rcc=expected_rcc, asset_payloads=payloads)


def test_jat_candidate_source_must_match_the_release_tag_target():
    release, expected_rcc, payloads = _fixture()

    with pytest.raises(ValueError, match="does not resolve to the recorded JAT source"):
        verify_release(release, release_commit_sha="c" * 40, expected_rcc=expected_rcc, asset_payloads=payloads)
