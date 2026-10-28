from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.promote_rcc_candidate as promoter
from scripts.promote_rcc_candidate import (
    BOOTSTRAP_FILES,
    CONTROLLER_MANIFESTS,
    FREEZE_DIRS,
    FREEZES,
    LOCKS,
    PACKAGE_MANIFESTS,
    PLATFORM_ASSETS,
    RELEASE_CANDIDATE_LOCKS,
    ROBOT_FILES,
    RUNTIME_MANIFESTS,
    TEMPLATE_MANIFEST,
    _load_receipts,
    promote_candidate,
    verify_downloaded_assets,
    verify_existing_release,
)


def _jat_candidate(candidate, *, source="c" * 40):
    release_tag = "v0.1.12-jat-runtime"
    platforms = {}
    for platform, (release_asset, asset_digest, spec_digest) in {
        "linux-x64": ("jat-runtime-linux-amd64.rcca", "8", "a"),
        "win32-x64": ("jat-runtime-windows-amd64.rcca", "9", "b"),
    }.items():
        platforms[platform] = {
            "digest": "sha256:" + asset_digest * 64,
            "specification_digest": "sha256:" + spec_digest * 64,
            "archive": {
                "asset": release_asset,
                "url": f"https://github.com/joshyorko/josh-all-the-things/releases/download/{release_tag}/{release_asset}",
                "sha256": ("c" if platform == "linux-x64" else "d") * 64,
                "size": 1234 if platform == "linux-x64" else 5678,
            },
        }
    return {
        "status": "QUALIFIED",
        "repository": "https://github.com/joshyorko/josh-all-the-things",
        "release_tag": release_tag,
        "source_sha": source,
        "source_archive": {
            "asset": f"josh-all-the-things-{source}.tar.gz",
            "url": f"https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/{source}",
            "sha256": "7" * 64,
            "size": 4321,
        },
        "rcc_version": candidate["version"],
        "rcc_source_sha": candidate["source_sha"],
        "rcc_platforms": candidate["platforms"],
        "environment_artifact": platforms["linux-x64"],
        "environment_artifacts": platforms,
    }


def test_promotion_cli_imports_when_run_by_workflow():
    result = subprocess.run(
        [sys.executable, str(Path(promoter.__file__).with_name("promote_rcc_candidate.py")), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Prepare a complete, receipt-bound controller and JAT runtime consumer update" in result.stdout


def _write_fixture(root: Path, artifacts: Path, *, source: str = "a" * 40):
    for relative in BOOTSTRAP_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("code --install-extension joshyorko.josh-room-0.1.32\n", encoding="utf-8")
    for relative in PACKAGE_MANIFESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": "josh-room", "version": "0.1.32"}, indent=2) + "\n", encoding="utf-8")
    for relative in (*CONTROLLER_MANIFESTS, *RUNTIME_MANIFESTS, *LOCKS, *ROBOT_FILES):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".yaml":
            path.write_text(
                'tasks:\n  "Josh Room":\n    shell: python -m josh_room --help\n\n'
                'environmentConfigs:\n  - environment_linux_amd64_freeze.yaml\n  - conda.yaml\n',
                encoding="utf-8",
            )
        elif "controller-artifact-manifest" in path.name:
            path.write_text(json.dumps({"schema_version": 1, "rcc": {"version": "v18.19.5"}, "controller": {"robot": "controller/robot.yaml"}}, indent=2) + "\n")
        elif path in [root / relative for relative in RUNTIME_MANIFESTS]:
            old_jat = {
                "git_sha": "c" * 40,
                "release_tag": "v0.1.11-jat-runtime",
                "source_archive": {"asset": "jat-source.tar.gz", "url": "https://example.test/jat-source.tar.gz", "sha256": "7" * 64, "size": 100},
                "rcc": {"version": "v18.19.5", "platforms": {}},
                "environment_artifact": {"digest": "sha256:" + "e" * 64, "specification_digest": "sha256:" + "f" * 64, "archive": {"asset": "old-jat.rcca", "url": "https://example.test/old-jat.rcca", "sha256": "6" * 64, "size": 50}},
                "environment_artifacts": {},
            }
            path.write_text(json.dumps({"schema_version": 1, "extension_version": "0.1.32", "rcc": {"version": "v18.19.5"}, "controller": {}, "jat": old_jat}, indent=2) + "\n")
        else:
            old_jat = {
                "repository": "https://github.com/joshyorko/josh-all-the-things",
                "version": "git-cccccccc",
                "git_sha": "c" * 40,
                "release_tag": "v0.1.11-jat-runtime",
                "source_archive": {"asset": "jat-source.tar.gz", "url": "https://example.test/jat-source.tar.gz", "sha256": "7" * 64, "size": 100},
                "environment_artifact": {"release_tag": "v0.1.11-jat-runtime", "archive_url": "https://example.test/old-jat.rcca", "archive_sha256": "6" * 64, "archive_size": 50, "rcc_artifact_digest": "sha256:" + "e" * 64, "rcc_specification_digest": "sha256:" + "f" * 64, "rcc_version": "v18.19.5", "platform": "linux_amd64"},
                "environment_artifacts": {},
                "rcc": {"version": "v18.19.5", "platforms": {}},
            }
            path.write_text(json.dumps({"format_version": 1, "candidate_version": "0.1.32", "josh_room": {"version": "0.1.32", "git_sha": "a" * 40}, "template": {"oci": "ghcr.io/example/room", "version": "0.1.2"}, "rcc": {"version": "v18.19.5"}, "controller": {"platforms": {}}, "jat": old_jat}, indent=2) + "\n")

    template_path = root / TEMPLATE_MANIFEST
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(json.dumps({"id": "room", "version": "0.1.2"}, indent=2) + "\n")

    candidate = {
        "version": "v18.19.6",
        "source_sha": "b" * 40,
        "platforms": {
            "linux-x64": {"asset": "rcc-linux64", "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.6/rcc-linux64", "sha256": "c" * 64, "size": 11},
            "win32-x64": {"asset": "rcc-windows64.exe", "url": "https://github.com/joshyorko/rcc/releases/download/v18.19.6/rcc-windows64.exe", "sha256": "d" * 64, "size": 12},
        },
    }
    for platform, archive_name in PLATFORM_ASSETS.items():
        directory = artifacts / f"rcc-controller-candidate-{platform}"
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / archive_name
        archive_bytes = f"verified {platform} archive".encode()
        archive.write_bytes(archive_bytes)
        freeze_name = FREEZES[platform]
        freeze = directory / freeze_name
        freeze_bytes = f"fresh {platform} freeze".encode()
        freeze.write_bytes(freeze_bytes)
        receipt = {
            "artifact_digest": "sha256:" + ("e" if platform == "linux-x64" else "f") * 64,
            "specification_digest": "sha256:" + "1" * 64,
            "legacy_blueprint_key": f"{platform}-blueprint",
            "archive": {"sha256": hashlib.sha256(archive_bytes).hexdigest(), "size": len(archive_bytes)},
            "rcc_version": candidate["version"],
            "rcc_sha256": candidate["platforms"][platform]["sha256"],
            "input_digest": "sha256:" + ("2" if platform == "linux-x64" else "3") * 64,
            "freeze": {"asset": freeze_name, "sha256": hashlib.sha256(freeze_bytes).hexdigest(), "size": len(freeze_bytes)},
            "source": source,
            "platform": platform,
            "verified_acquire": True,
            "verified_no_build": True,
            "verified_exec": True,
            "verified_crypto": True,
        }
        (directory / f"{Path(archive_name).stem}.json").write_text(json.dumps(receipt), encoding="utf-8")
    return candidate


def _inputs(root: Path) -> set[Path]:
    return {root / relative for relative in (*CONTROLLER_MANIFESTS, *RUNTIME_MANIFESTS, *LOCKS, *PACKAGE_MANIFESTS, TEMPLATE_MANIFEST, *RELEASE_CANDIDATE_LOCKS, *BOOTSTRAP_FILES, *ROBOT_FILES, *(directory / freeze for directory in FREEZE_DIRS for freeze in FREEZES.values()))}


def test_promotion_updates_both_consumers_from_verified_platform_receipts(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)

    report = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)

    assert report["status"] == "PASS"
    assert report["no_change"] is False
    assert report["artifact_release_tag"].startswith(f"rcc-v18.19.6-controller-{'a' * 40}-")
    assert report["release_version"] == "0.1.33"
    assert report["jat_source_handoff"] == {
        "from": "c" * 40,
        "to": "c" * 40,
        "release_tag": "v0.1.12-jat-runtime",
        "requires_consumer_review": False,
        "state": "same_pinned_source",
    }
    for relative in RUNTIME_MANIFESTS:
        manifest = json.loads((root / relative).read_text())
        assert manifest["rcc"]["version"] == "v18.19.6"
        assert manifest["jat"]["rcc"]["version"] == "v18.19.6"
        assert manifest["jat"]["environment_artifact"]["archive"]["url"].startswith(
            "https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.12-jat-runtime/"
        )
        assert manifest["jat"]["environment_artifacts"]["win32-x64"]["digest"].startswith("sha256:")
        assert set(manifest["controller"]["environment_artifacts"]) == {"linux-x64", "win32-x64"}
        assert manifest["controller"]["environment_artifacts"]["linux-x64"]["source_sha"] == "a" * 40
        assert manifest["controller"]["environment_artifacts"]["win32-x64"]["archive"]["url"].endswith("josh-room-controller-windows-amd64.rcca")
    for relative in CONTROLLER_MANIFESTS:
        assert json.loads((root / relative).read_text())["rcc"]["version"] == "v18.19.6"
    for relative in LOCKS:
        lock = json.loads((root / relative).read_text())
        assert lock["rcc"]["source_sha"] == "b" * 40
        assert lock["controller"]["source_sha"] == "a" * 40
        assert lock["controller"]["platforms"]["win32-x64"]["input_digest"] == "sha256:" + "3" * 64
    for relative in PACKAGE_MANIFESTS:
        assert json.loads((root / relative).read_text())["version"] == "0.1.33"
    assert json.loads((root / TEMPLATE_MANIFEST).read_text())["version"] == "0.1.3"
    for relative in RUNTIME_MANIFESTS:
        manifest = json.loads((root / relative).read_text())
        assert manifest["extension_version"] == "0.1.33"
        assert manifest["jat"]["source_archive"]["url"].endswith("/tarball/" + "c" * 40)
    for relative in RELEASE_CANDIDATE_LOCKS:
        lock = json.loads((root / relative).read_text())
        assert lock["candidate_version"] == "0.1.33"
        assert lock["template"]["version"] == "0.1.3"
    assert report["jat_platforms"]["linux-x64"]["digest"] == "sha256:" + "8" * 64
    assert report["jat_platforms"]["win32-x64"]["digest"] == "sha256:" + "9" * 64
    assert report["jat_platforms"]["linux-x64"]["archive_sha256"] == "c" * 64
    for relative in BOOTSTRAP_FILES:
        assert "joshyorko.josh-room-0.1.33" in (root / relative).read_text()
    for relative in ROBOT_FILES:
        assert "environment_windows_amd64_freeze.yaml" in (root / relative).read_text()
    assert (root / FREEZE_DIRS[0] / FREEZES["win32-x64"]).read_bytes() == (artifacts / "rcc-controller-candidate-win32-x64" / FREEZES["win32-x64"]).read_bytes()

    second = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)
    assert second["no_change"] is True
    assert second["changed_files"] == []


def test_first_promotion_bootstraps_missing_template_lock_from_root_lock(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    template_lock = root / LOCKS[1]
    template_lock.unlink()

    jat_candidate = _jat_candidate(candidate)
    report = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)

    assert report["status"] == "PASS"
    assert str(LOCKS[1]) in report["changed_files"]
    assert json.loads(template_lock.read_text()) == json.loads((root / LOCKS[0]).read_text())


def test_missing_or_stale_platform_receipt_leaves_every_consumer_file_unchanged(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}

    with pytest.raises(ValueError, match="stale for the requested source commit"):
        promote_candidate(root, candidate, artifacts, "9" * 40, jat_candidate=_jat_candidate(candidate))

    assert {path: path.read_bytes() for path in before} == before


def test_reviewable_consumer_pr_hands_off_to_a_verified_new_jat_source(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)

    report = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate, source="d" * 40))

    assert report["status"] == "PASS"
    assert report["jat_source_handoff"] == {
        "from": "c" * 40,
        "to": "d" * 40,
        "release_tag": "v0.1.12-jat-runtime",
        "requires_consumer_review": True,
        "state": "awaiting_consumer_pr_review",
    }
    manifest = json.loads((root / RUNTIME_MANIFESTS[0]).read_text())
    assert manifest["jat"]["git_sha"] == "d" * 40
    assert manifest["jat"]["source_archive"]["sha256"] == "7" * 64


def test_superseded_jat_release_blocks_promotion_before_any_write(tmp_path: Path, monkeypatch):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}
    monkeypatch.setattr(promoter, "fetch_release", lambda: {"tag_name": "v18.19.6"})
    monkeypatch.setattr(promoter, "verify_release", lambda _release: candidate)
    latest_jat = _jat_candidate(candidate)
    latest_jat["release_tag"] = "v0.1.13-jat-runtime"
    monkeypatch.setattr(promoter, "fetch_latest_jat_candidate", lambda _rcc: latest_jat)

    with pytest.raises(ValueError, match="JAT runtime candidate was superseded before promotion"):
        promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, verify_latest=True)

    assert {path: path.read_bytes() for path in before} == before
    (artifacts / "rcc-controller-candidate-win32-x64" / "josh-room-controller-windows-amd64.json").unlink()
    with pytest.raises(FileNotFoundError):
        promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate))
    assert {path: path.read_bytes() for path in before} == before


def test_existing_artifact_release_is_reused_only_when_every_asset_matches(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    promotion = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate))
    assets = []
    for directory in artifacts.iterdir():
        for path in directory.iterdir():
            if path.suffix in {".rcca", ".json"}:
                assets.append({"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size})
    release = {"tag_name": promotion["artifact_release_tag"], "target_commitish": "a" * 40, "draft": False, "prerelease": True, "assets": assets}

    receipts = _load_receipts(artifacts, candidate, "a" * 40)
    assert verify_existing_release(release, tag=promotion["artifact_release_tag"], source_sha="a" * 40, tag_target_sha="a" * 40, receipts=receipts) == []
    partial = {**release, "assets": assets[:-1]}
    assert verify_existing_release(partial, tag=promotion["artifact_release_tag"], source_sha="a" * 40, tag_target_sha="a" * 40, receipts=receipts)
    conflict = {**release, "assets": [{**assets[0], "digest": "sha256:" + "0" * 64}, *assets[1:]]}
    with pytest.raises(ValueError, match="conflicts with candidate"):
        verify_existing_release(conflict, tag=promotion["artifact_release_tag"], source_sha="a" * 40, tag_target_sha="a" * 40, receipts=receipts)
    with pytest.raises(ValueError, match="Git tag does not resolve"):
        verify_existing_release(release, tag=promotion["artifact_release_tag"], source_sha="a" * 40, tag_target_sha="b" * 40, receipts=receipts)

    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    for asset in assets:
        source = next(path / asset["name"] for path in artifacts.iterdir() if (path / asset["name"]).is_file())
        (downloaded / asset["name"]).write_bytes(source.read_bytes())
    verify_downloaded_assets(downloaded, receipts)


def test_superseded_rcc_candidate_blocks_promotion_before_any_write(tmp_path: Path, monkeypatch):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}
    monkeypatch.setattr(promoter, "fetch_release", lambda: {"tag_name": "v18.19.7"})
    monkeypatch.setattr(promoter, "verify_release", lambda _release: {**candidate, "version": "v18.19.7"})

    with pytest.raises(ValueError, match="superseded before promotion"):
        promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate), verify_latest=True)

    assert {path: path.read_bytes() for path in before} == before


def test_plan_only_reports_exact_changes_without_writing_consumer_files(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}

    report = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate), prepare=False)

    assert report["status"] == "PASS"
    assert report["no_change"] is False
    assert report["changed_files"]
    assert {path: path.read_bytes() for path in before} == before


def test_no_change_detection_repairs_template_drift(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    first = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)
    assert first["status"] == "PASS"
    template_lock = root / LOCKS[1]
    template_lock.write_text('{"format_version": 1, "rcc": {}, "controller": {}}\n', encoding="utf-8")

    repair = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)

    assert repair["no_change"] is False
    assert str(LOCKS[1]) in repair["changed_files"]
    assert json.loads(template_lock.read_text())["controller"]["source_sha"] == "a" * 40


def test_promotion_repairs_stale_template_lock_versions(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    lock = json.loads((root / RELEASE_CANDIDATE_LOCKS[1]).read_text())
    lock["template"]["version"] = "0.1.1"
    (root / RELEASE_CANDIDATE_LOCKS[1]).write_text(json.dumps(lock), encoding="utf-8")

    report = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=_jat_candidate(candidate))

    assert report["status"] == "PASS"
    assert json.loads((root / TEMPLATE_MANIFEST).read_text())["version"] == "0.1.3"
    for relative in RELEASE_CANDIDATE_LOCKS:
        assert json.loads((root / relative).read_text())["template"]["version"] == "0.1.3"


def test_artifact_release_tag_binds_complete_input_digests(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    first = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)
    receipt_path = artifacts / "rcc-controller-candidate-linux-x64" / "josh-room-controller-linux-amd64.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["input_digest"] = receipt["input_digest"][:-1] + "4"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    second = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)

    assert first["artifact_release_tag"] != second["artifact_release_tag"]


@pytest.mark.parametrize("platform", PLATFORM_ASSETS)
def test_artifact_release_tag_changes_when_rebuild_bytes_change(tmp_path: Path, platform: str):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    first = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)
    archive = artifacts / f"rcc-controller-candidate-{platform}" / PLATFORM_ASSETS[platform]
    archive.write_bytes(archive.read_bytes() + b"fresh qualified build")
    receipt_path = archive.with_suffix(".json")
    receipt = json.loads(receipt_path.read_text())
    receipt["archive"] = {"sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    second = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)

    assert first["artifact_release_tag"] != second["artifact_release_tag"]


@pytest.mark.parametrize("platform", PLATFORM_ASSETS)
def test_artifact_release_tag_binds_exact_receipt_bytes(tmp_path: Path, platform: str):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    first = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)
    receipt_path = (artifacts / f"rcc-controller-candidate-{platform}" / PLATFORM_ASSETS[platform]).with_suffix(".json")
    receipt_path.write_text(receipt_path.read_text() + "\n", encoding="utf-8")
    second = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)

    assert first["artifact_release_tag"] != second["artifact_release_tag"]


def test_identical_qualified_outputs_reuse_a_bounded_release_tag(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    options = {"jat_candidate": _jat_candidate(candidate), "prepare": False}
    first = promote_candidate(root, candidate, artifacts, "a" * 40, **options)
    second = promote_candidate(root, candidate, artifacts, "a" * 40, **options)

    assert first["artifact_release_tag"] == second["artifact_release_tag"]
    assert len(first["artifact_release_tag"].encode("utf-8")) <= 255


def test_artifact_release_tag_binds_source_sha_even_if_inputs_are_unchanged(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    original = promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate, prepare=False)
    for platform in PLATFORM_ASSETS:
        receipt_path = artifacts / f"rcc-controller-candidate-{platform}" / f"{Path(PLATFORM_ASSETS[platform]).stem}.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["source"] = "9" * 40
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    updated = promote_candidate(root, candidate, artifacts, "9" * 40, jat_candidate=jat_candidate, prepare=False)

    assert original["artifact_release_tag"] != updated["artifact_release_tag"]
    assert original["artifact_release_tag"].startswith(f"rcc-v18.19.6-controller-{'a' * 40}-")
    assert updated["artifact_release_tag"].startswith(f"rcc-v18.19.6-controller-{'9' * 40}-")


@pytest.mark.parametrize("rcc_field,stale", [("rcc_version", "v18.19.5"), ("rcc_source_sha", "0" * 40)])
def test_stale_jat_rcc_combination_cannot_overwrite_current_consumers(tmp_path, rcc_field, stale):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    jat_candidate = _jat_candidate(candidate)
    jat_candidate[rcc_field] = stale
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}
    with pytest.raises(ValueError):
        promote_candidate(root, candidate, artifacts, "a" * 40, jat_candidate=jat_candidate)
    assert {path: path.read_bytes() for path in before} == before
