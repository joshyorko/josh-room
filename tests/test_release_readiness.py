from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.verify_release_readiness import (
    CI_REQUIRED_CHECKS,
    validate_green_checks,
    validate_release_assets,
    validate_runtime_compatibility,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40


def _green_checks():
    return [
        {"name": name, "head_sha": SOURCE_SHA, "status": "completed", "conclusion": "success"}
        for name in CI_REQUIRED_CHECKS
    ]


def test_release_readiness_requires_the_complete_ci_matrix_on_exact_source_sha():
    report = validate_green_checks(_green_checks(), source_sha=SOURCE_SHA)

    assert report["source_sha"] == SOURCE_SHA
    assert set(report["checks"]) == set(CI_REQUIRED_CHECKS)


@pytest.mark.parametrize("change", ["missing", "failed", "in_progress", "stale_sha"])
def test_release_readiness_rejects_incomplete_failed_or_stale_ci(change):
    checks = _green_checks()
    if change == "missing":
        checks = checks[:-1]
    elif change == "failed":
        checks[0]["conclusion"] = "failure"
    elif change == "in_progress":
        checks[0]["status"] = "in_progress"
    else:
        checks[0]["head_sha"] = "b" * 40

    with pytest.raises(ValueError):
        validate_green_checks(checks, source_sha=SOURCE_SHA)


def test_release_readiness_rejects_current_controller_jat_rcc_split():
    runtime = json.loads((ROOT / "vscode-extension/runtime/manifest.json").read_text())
    lock = json.loads((ROOT / "release-lock.json").read_text())

    with pytest.raises(ValueError, match="controller and JAT RCC pins are not paired"):
        validate_runtime_compatibility(runtime, lock)


def test_release_readiness_accepts_coherent_current_runtime_receipts():
    runtime, lock = _coherent_release_fixture()

    report = validate_runtime_compatibility(runtime, lock)

    assert report["rcc_version"] == runtime["rcc"]["version"]
    assert report["platforms"] == ["linux-x64", "win32-x64"]


def _coherent_release_fixture():
    runtime = json.loads((ROOT / "vscode-extension/runtime/manifest.json").read_text())
    lock = json.loads((ROOT / "release-lock.json").read_text())
    runtime = copy.deepcopy(runtime)
    lock = copy.deepcopy(lock)
    runtime["jat"]["rcc"] = copy.deepcopy(runtime["rcc"])
    runtime["jat"]["rcc"]["source_sha"] = lock["rcc"]["source_sha"]
    runtime["jat"]["release_tag"] = "v0.1.12-jat-runtime"
    lock["jat"]["rcc"] = copy.deepcopy(lock["rcc"])
    lock["jat"]["rcc"]["source_sha"] = lock["rcc"]["source_sha"]
    lock["jat"]["release_tag"] = runtime["jat"]["release_tag"]
    lock["jat"]["environment_artifact"]["release_tag"] = runtime["jat"]["release_tag"]
    source_sha = lock["rcc"]["source_sha"]
    for index, platform in enumerate(("linux-x64", "win32-x64"), start=1):
        artifact = runtime["jat"]["environment_artifacts"][platform]
        lock_platform = "linux_amd64" if platform == "linux-x64" else "windows_amd64"
        lock_artifact = lock["jat"]["environment_artifacts"][lock_platform]
        artifact["digest"] = "sha256:" + str(index + 1) * 64
        artifact["specification_digest"] = "sha256:" + str(index + 3) * 64
        artifact["rcc_version"] = runtime["rcc"]["version"]
        artifact["rcc_source_sha"] = source_sha
        archive = artifact["archive"]
        archive["sha256"] = str(index + 5) * 64
        archive["url"] = archive["url"].replace("v0.1.11-jat-runtime", runtime["jat"]["release_tag"])
        lock_artifact.update({
            "rcc_artifact_digest": artifact["digest"],
            "rcc_specification_digest": artifact["specification_digest"],
            "archive_sha256": archive["sha256"],
            "rcc_version": artifact["rcc_version"],
            "rcc_source_sha": source_sha,
        })
        lock_artifact["archive_size"] = archive["size"]
    runtime["jat"]["environment_artifact"] = runtime["jat"]["environment_artifacts"]["linux-x64"]
    linux = runtime["jat"]["environment_artifact"]
    lock["jat"]["environment_artifact"].update({
        "archive_url": linux["archive"]["url"],
        "archive_sha256": linux["archive"]["sha256"],
        "archive_size": linux["archive"]["size"],
        "rcc_artifact_digest": linux["digest"],
        "rcc_specification_digest": linux["specification_digest"],
        "rcc_version": linux["rcc_version"],
        "rcc_source_sha": source_sha,
    })
    return runtime, lock


def test_published_controller_and_jat_tags_are_bound_to_locked_source_commits():
    runtime, lock = _coherent_release_fixture()

    def release(tag, artifacts):
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": True,
            "assets": [
                {
                    "name": artifact["archive"]["asset"],
                    "digest": "sha256:" + artifact["archive"]["sha256"],
                    "size": artifact["archive"]["size"],
                    "browser_download_url": artifact["archive"]["url"],
                }
                for artifact in artifacts.values()
            ],
        }

    controller = release(lock["controller"]["release_tag"], runtime["controller"]["environment_artifacts"])
    jat = release(lock["jat"]["environment_artifact"]["release_tag"], runtime["jat"]["environment_artifacts"])
    report = validate_release_assets(
        runtime,
        lock,
        controller_release=controller,
        jat_release=jat,
        controller_tag_sha=lock["controller"]["source_sha"],
        jat_tag_sha=lock["jat"]["git_sha"],
    )

    assert report["controller_release"] == lock["controller"]["release_tag"]
    assert report["jat_release"] == lock["jat"]["environment_artifact"]["release_tag"]
    with pytest.raises(ValueError, match="tag target"):
        validate_release_assets(
            runtime,
            lock,
            controller_release=controller,
            jat_release=jat,
            controller_tag_sha="d" * 40,
            jat_tag_sha=lock["jat"]["git_sha"],
        )


def test_controller_artifact_receipt_cannot_claim_an_unpinned_rcc_binary():
    runtime, lock = _coherent_release_fixture()
    runtime["controller"]["environment_artifacts"]["linux-x64"]["rcc_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="RCC checksum"):
        validate_runtime_compatibility(runtime, lock)


def test_jat_source_archive_must_be_bound_to_its_locked_source_commit():
    runtime, lock = _coherent_release_fixture()
    runtime["jat"]["source_archive"]["url"] = "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "d" * 40

    with pytest.raises(ValueError, match="JAT source archive is not bound"):
        validate_runtime_compatibility(runtime, lock)
