from __future__ import annotations

import hashlib
import json

import pytest

from scripts.release_staging import (
    build_receipt,
    reconcile_release_assets,
    runtime_identity,
    validate_staged_files,
)

SOURCE_SHA = "a" * 40
RUNTIME = {
    "rcc": {"version": "v18.19.6"},
    "controller": {"environment_artifacts": {
        "linux-x64": {"digest": "sha256:" + "1" * 64, "archive": {"sha256": "2" * 64, "size": 12}},
        "win32-x64": {"digest": "sha256:" + "3" * 64, "archive": {"sha256": "4" * 64, "size": 34}},
    }},
    "jat": {"git_sha": "b" * 40, "release_tag": "v0.1.12-jat-runtime", "rcc": {"version": "v18.19.6"}, "environment_artifacts": {
        "linux-x64": {"digest": "sha256:" + "5" * 64, "archive": {"sha256": "6" * 64, "size": 56}},
        "win32-x64": {"digest": "sha256:" + "7" * 64, "archive": {"sha256": "8" * 64, "size": 78}},
    }},
}
LOCK = {
    "rcc": {"source_sha": "c" * 40},
    "controller": {"release_tag": "controller-candidate"},
    "jat": {"rcc": {"source_sha": "c" * 40}},
}
CHECKS = {"source_sha": SOURCE_SHA, "checks": {"test": {"id": 101, "status": "completed", "conclusion": "success", "head_sha": SOURCE_SHA}}}


def _files(directory):
    directory.mkdir(parents=True)
    (directory / "josh-room-0.1.33.vsix").write_bytes(b"synthetic vsix")
    (directory / "josh_room-0.1.33-py3-none-any.whl").write_bytes(b"synthetic wheel")
    (directory / "josh_room-0.1.33.tar.gz").write_bytes(b"synthetic sdist")
    (directory / "SHA256SUMS").write_text("synthetic checksums\n")


def _draft(tag, sha, assets):
    return {"tag_name": tag, "target_commitish": sha, "draft": True, "prerelease": False, "assets": assets}


def test_runtime_identity_keeps_both_platform_artifact_digests():
    value = runtime_identity(RUNTIME, LOCK)

    assert value["controller"]["rcc_version"] == "v18.19.6"
    assert value["jat"]["rcc_version"] == "v18.19.6"
    assert value["jat"]["release_tag"] == "v0.1.12-jat-runtime"
    assert set(value["controller"]["platforms"]) == {"linux-x64", "win32-x64"}
    assert set(value["jat"]["platforms"]) == {"linux-x64", "win32-x64"}


def test_candidate_receipt_binds_source_sha_checks_and_exact_assets(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    identity = runtime_identity(RUNTIME, LOCK)
    receipt = build_receipt(
        staged,
        source_sha=SOURCE_SHA,
        release_tag="v0.1.33-standalone-vsix",
        version="0.1.33",
        checks=CHECKS,
        runtime=identity,
    )
    (staged / "release-candidate.json").write_text(json.dumps(receipt))

    validated = validate_staged_files(
        staged,
        receipt,
        source_sha=SOURCE_SHA,
        release_tag="v0.1.33-standalone-vsix",
        version="0.1.33",
        checks=CHECKS,
        runtime=identity,
    )

    assert set(validated) == set(receipt["assets"])
    assert receipt["source_sha"] == SOURCE_SHA
    assert receipt["assets"]["josh-room-0.1.33.vsix"]["sha256"] == hashlib.sha256(b"synthetic vsix").hexdigest()


def test_staged_receipt_rejects_changed_source_checks_runtime_or_asset_bytes(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    identity = runtime_identity(RUNTIME, LOCK)
    receipt = build_receipt(staged, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity)

    with pytest.raises(ValueError, match="source SHA"):
        validate_staged_files(staged, receipt, source_sha="d" * 40, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity)
    with pytest.raises(ValueError, match="check receipts"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks={"source_sha": "a" * 40, "checks": {"test": {"id": 102}}}, runtime=identity)
    changed = json.loads(json.dumps(identity))
    changed["jat"]["platforms"]["linux-x64"]["archive_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="runtime receipts"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=changed)
    (staged / "josh-room-0.1.33.vsix").write_bytes(b"changed")
    with pytest.raises(ValueError, match="asset differs"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity)


def test_release_asset_reconciliation_is_idempotent_and_rejects_conflicts(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    tag = "v0.1.33-standalone-vsix"
    files = {path.name: path for path in staged.iterdir()}
    assets = [{"name": name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size} for name, path in files.items()]
    release = _draft(tag, SOURCE_SHA, assets)
    # GitHub's target_commitish is not authoritative for an existing release;
    # the Git tag object SHA is checked separately.
    release["target_commitish"] = "main"

    assert reconcile_release_assets(release, staged, expected_tag=tag, source_sha=SOURCE_SHA, tag_target_sha=SOURCE_SHA) == []
    missing_asset = assets[-1]["name"]
    assert reconcile_release_assets({**release, "assets": assets[:-1]}, staged, expected_tag=tag, source_sha=SOURCE_SHA, tag_target_sha=SOURCE_SHA) == [missing_asset]
    conflict = {**release, "assets": [{**assets[0], "digest": "sha256:" + "0" * 64}, *assets[1:]]}
    with pytest.raises(ValueError, match="conflicts with staged release"):
        reconcile_release_assets(conflict, staged, expected_tag=tag, source_sha=SOURCE_SHA, tag_target_sha=SOURCE_SHA)
    with pytest.raises(ValueError, match="tag target SHA"):
        reconcile_release_assets(release, staged, expected_tag=tag, source_sha=SOURCE_SHA, tag_target_sha="b" * 40)


def test_published_release_retry_verifies_exact_assets_without_reupload(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    assets = [{"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size} for path in staged.iterdir()]
    release = _draft("v0.1.33-standalone-vsix", SOURCE_SHA, assets)
    release["draft"] = False
    missing = reconcile_release_assets(release, staged, expected_tag=release["tag_name"], source_sha=SOURCE_SHA, tag_target_sha=SOURCE_SHA)

    assert missing == []
    with pytest.raises(ValueError, match="public release is missing staged assets"):
        reconcile_release_assets({**release, "assets": []}, staged, expected_tag=release["tag_name"], source_sha=SOURCE_SHA, tag_target_sha=SOURCE_SHA)
