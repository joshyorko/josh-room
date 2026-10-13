from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.promote_rcc_candidate as promoter
from scripts.promote_rcc_candidate import (
    CONTROLLER_MANIFESTS,
    FREEZE_DIRS,
    FREEZES,
    LOCKS,
    PLATFORM_ASSETS,
    ROBOT_FILES,
    RUNTIME_MANIFESTS,
    _load_receipts,
    promote_candidate,
    verify_downloaded_assets,
    verify_existing_release,
)


def _write_fixture(root: Path, artifacts: Path, *, source: str = "a" * 40):
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
            path.write_text(json.dumps({"schema_version": 1, "rcc": {"version": "v18.19.5"}, "controller": {}}, indent=2) + "\n")
        else:
            path.write_text(json.dumps({"format_version": 1, "rcc": {"version": "v18.19.5"}, "controller": {"platforms": {}}}, indent=2) + "\n")

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
    return {root / relative for relative in (*CONTROLLER_MANIFESTS, *RUNTIME_MANIFESTS, *LOCKS, *ROBOT_FILES, *(directory / freeze for directory in FREEZE_DIRS for freeze in FREEZES.values()))}


def test_promotion_updates_both_consumers_from_verified_platform_receipts(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)

    report = promote_candidate(root, candidate, artifacts, "a" * 40)

    assert report["status"] == "PASS"
    assert report["no_change"] is False
    assert report["artifact_release_tag"].startswith("rcc-v18.19.6-controller-")
    for relative in RUNTIME_MANIFESTS:
        manifest = json.loads((root / relative).read_text())
        assert manifest["rcc"]["version"] == "v18.19.6"
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
    for relative in ROBOT_FILES:
        assert "environment_windows_amd64_freeze.yaml" in (root / relative).read_text()
    assert (root / FREEZE_DIRS[0] / FREEZES["win32-x64"]).read_bytes() == (artifacts / "rcc-controller-candidate-win32-x64" / FREEZES["win32-x64"]).read_bytes()

    second = promote_candidate(root, candidate, artifacts, "a" * 40)
    assert second["no_change"] is True
    assert second["changed_files"] == []


def test_missing_or_stale_platform_receipt_leaves_every_consumer_file_unchanged(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}

    with pytest.raises(ValueError, match="stale for the requested source commit"):
        promote_candidate(root, candidate, artifacts, "9" * 40)

    assert {path: path.read_bytes() for path in before} == before
    (artifacts / "rcc-controller-candidate-win32-x64" / "josh-room-controller-windows-amd64.json").unlink()
    with pytest.raises(FileNotFoundError):
        promote_candidate(root, candidate, artifacts, "a" * 40)
    assert {path: path.read_bytes() for path in before} == before


def test_existing_artifact_release_is_reused_only_when_every_asset_matches(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    promotion = promote_candidate(root, candidate, artifacts, "a" * 40)
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
        promote_candidate(root, candidate, artifacts, "a" * 40, verify_latest=True)

    assert {path: path.read_bytes() for path in before} == before


def test_plan_only_reports_exact_changes_without_writing_consumer_files(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    before = {path: path.read_bytes() for path in _inputs(root) if path.exists()}

    report = promote_candidate(root, candidate, artifacts, "a" * 40, prepare=False)

    assert report["status"] == "PASS"
    assert report["no_change"] is False
    assert report["changed_files"]
    assert {path: path.read_bytes() for path in before} == before


def test_no_change_detection_repairs_template_drift(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    first = promote_candidate(root, candidate, artifacts, "a" * 40)
    assert first["status"] == "PASS"
    template_lock = root / LOCKS[1]
    template_lock.write_text('{"format_version": 1, "rcc": {}, "controller": {}}\n', encoding="utf-8")

    repair = promote_candidate(root, candidate, artifacts, "a" * 40)

    assert repair["no_change"] is False
    assert str(LOCKS[1]) in repair["changed_files"]
    assert json.loads(template_lock.read_text())["controller"]["source_sha"] == "a" * 40


def test_artifact_release_tag_binds_complete_input_digests(tmp_path: Path):
    root, artifacts = tmp_path / "repo", tmp_path / "artifacts"
    candidate = _write_fixture(root, artifacts)
    first = promote_candidate(root, candidate, artifacts, "a" * 40, prepare=False)
    receipt_path = artifacts / "rcc-controller-candidate-linux-x64" / "josh-room-controller-linux-amd64.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["input_digest"] = receipt["input_digest"][:-1] + "4"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    second = promote_candidate(root, candidate, artifacts, "a" * 40, prepare=False)

    assert first["artifact_release_tag"] != second["artifact_release_tag"]
