#!/usr/bin/env python3
"""Build and verify exact, retry-safe standalone release staging receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scripts.verify_release_readiness import CI_REQUIRED_CHECKS

SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE_WORKFLOW_PATH = ".github/workflows/release.yml"
REPOSITORY = "joshyorko/josh-room"


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _file_records(directory: Path, *, include_receipt: bool = False) -> dict[str, dict[str, Any]]:
    if not directory.is_dir():
        raise FileNotFoundError(f"release staging directory does not exist: {directory}")
    records = {}
    for path in directory.iterdir():
        if path.name == "release-candidate.json" and not include_receipt:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("release staging directory contains a non-file asset")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records[path.name] = {"sha256": digest, "size": path.stat().st_size}
    if not records:
        raise ValueError("release staging directory contains no assets")
    return dict(sorted(records.items()))


def runtime_identity(runtime: dict[str, Any], release_lock: dict[str, Any]) -> dict[str, Any]:
    runtime = _object(runtime, "runtime manifest")
    release_lock = _object(release_lock, "release lock")
    controller = _object(runtime.get("controller"), "controller runtime")
    jat = _object(runtime.get("jat"), "JAT runtime")
    rcc = _object(runtime.get("rcc"), "controller RCC")
    jat_rcc = _object(jat.get("rcc"), "JAT RCC")
    if rcc.get("version") != jat_rcc.get("version"):
        raise ValueError("controller and JAT RCC pins are not paired")
    controller_artifacts = _object(controller.get("environment_artifacts"), "controller artifacts")
    jat_artifacts = _object(jat.get("environment_artifacts"), "JAT artifacts")
    lock_controller = _object(release_lock.get("controller"), "release-lock controller")
    lock_jat = _object(release_lock.get("jat"), "release-lock JAT")
    controller_platforms = {}
    jat_platforms = {}
    for platform in ("linux-x64", "win32-x64"):
        artifact = _object(controller_artifacts.get(platform), f"controller {platform}")
        archive = _object(artifact.get("archive"), f"controller {platform} archive")
        controller_platforms[platform] = {
            "artifact_digest": artifact.get("digest"),
            "specification_digest": artifact.get("specification_digest"),
            "source_sha": artifact.get("source_sha"),
            "rcc_version": artifact.get("rcc_version"),
            "archive_sha256": archive.get("sha256"),
            "archive_size": archive.get("size"),
            "rcc_sha256": artifact.get("rcc_sha256"),
            "input_digest": artifact.get("input_digest"),
        }
        jat_artifact = _object(jat_artifacts.get(platform), f"JAT {platform}")
        jat_archive = _object(jat_artifact.get("archive"), f"JAT {platform} archive")
        jat_platforms[platform] = {
            "artifact_digest": jat_artifact.get("digest"),
            "specification_digest": jat_artifact.get("specification_digest"),
            "archive_sha256": jat_archive.get("sha256"),
            "archive_size": jat_archive.get("size"),
            "rcc_version": jat_artifact.get("rcc_version"),
            "rcc_source_sha": jat_artifact.get("rcc_source_sha"),
        }
    return {
        "controller": {
            "rcc_version": rcc.get("version"),
            "rcc_source_sha": _object(release_lock.get("rcc"), "release-lock RCC").get("source_sha"),
            "source_sha": lock_controller.get("source_sha"),
            "release_tag": lock_controller.get("release_tag"),
            "platforms": controller_platforms,
        },
        "jat": {
            "rcc_version": jat_rcc.get("version"),
            "rcc_source_sha": _object(lock_jat.get("rcc"), "release-lock JAT RCC").get("source_sha"),
            "release_tag": jat.get("release_tag"),
            "source_sha": jat.get("git_sha"),
            "source_archive": jat.get("source_archive"),
            "producer_workflow_run": jat.get("producer_workflow_run"),
            "platforms": jat_platforms,
        },
    }


def build_receipt(
    directory: Path,
    *,
    source_sha: str,
    release_tag: str,
    version: str,
    checks: dict[str, Any],
    runtime: dict[str, Any],
    stage_run: dict[str, Any],
) -> dict[str, Any]:
    if not SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("release source must be a full lowercase Git SHA")
    if not VERSION.fullmatch(version) or release_tag != f"v{version}-standalone-vsix":
        raise ValueError("release tag does not match the exact package version")
    checks = _object(checks, "exact-source CI check report")
    if checks.get("source_sha") != source_sha or not isinstance(checks.get("checks"), dict) or not checks["checks"]:
        raise ValueError("CI check receipts are missing or belong to another source SHA")
    workflow = _object(checks.get("workflow"), "trusted CI workflow receipt")
    if (
        workflow.get("path") != ".github/workflows/ci.yml"
        or workflow.get("head_sha") != source_sha
        or workflow.get("repository") != REPOSITORY
        or type(workflow.get("id")) is not int
        or type(workflow.get("run_id")) is not int
    ):
        raise ValueError("CI receipt is not bound to the trusted exact-source ci.yml run")
    if set(checks["checks"]) != CI_REQUIRED_CHECKS:
        raise ValueError("CI receipt does not contain the complete trusted release check set")
    for name, check in checks["checks"].items():
        check = _object(check, f"CI check {name}")
        if (
            check.get("head_sha") != source_sha
            or check.get("status") != "completed"
            or check.get("conclusion") != "success"
            or check.get("run_id") != workflow["run_id"]
            or check.get("app_id") != 15368
        ):
            raise ValueError(f"CI check receipt is not green for the exact source SHA: {name}")
    runtime = _object(runtime, "runtime receipt")
    if _object(runtime.get("controller"), "controller runtime receipt").get("rcc_version") != _object(runtime.get("jat"), "JAT runtime receipt").get("rcc_version"):
        raise ValueError("controller and JAT runtime receipts are not paired")
    stage_run = _object(stage_run, "candidate stage workflow run")
    if (
        type(stage_run.get("id")) is not int
        or type(stage_run.get("workflow_id")) is not int
        or stage_run.get("path") != RELEASE_WORKFLOW_PATH
        or stage_run.get("event") != "workflow_run"
        or stage_run.get("repository") != REPOSITORY
        or stage_run.get("source_sha") != source_sha
    ):
        raise ValueError("candidate stage receipt is not bound to its exact release workflow run")
    return {
        "schema_version": 1,
        "release_tag": release_tag,
        "source_sha": source_sha,
        "version": version,
        "checks": checks,
        "stage_run": stage_run,
        "runtime": runtime,
        "assets": _file_records(directory),
    }


def validate_staged_files(
    directory: Path,
    receipt: dict[str, Any],
    *,
    source_sha: str,
    release_tag: str,
    version: str,
    checks: dict[str, Any],
    runtime: dict[str, Any],
    stage_run: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    receipt = _object(receipt, "staged release receipt")
    if receipt.get("schema_version") != 1:
        raise ValueError("staged release receipt version is unsupported")
    if receipt.get("source_sha") != source_sha:
        raise ValueError("staged release receipt source SHA differs from the requested source SHA")
    if receipt.get("release_tag") != release_tag or receipt.get("version") != version:
        raise ValueError("staged release receipt tag or version differs from the candidate")
    if receipt.get("checks") != checks:
        raise ValueError("staged release receipt check receipts differ from the current exact-source CI")
    if receipt.get("stage_run") != stage_run:
        raise ValueError("staged release receipt belongs to a different stage workflow run")
    if receipt.get("runtime") != runtime:
        raise ValueError("staged release receipt runtime receipts differ from the current candidate")
    expected = _object(receipt.get("assets"), "staged release assets")
    observed = _file_records(directory)
    if observed != expected:
        raise ValueError("a staged release asset differs from its exact receipt")
    return observed


def validate_stage_run(
    stage_run: dict[str, Any],
    *,
    expected_run_id: int,
    expected_workflow_id: int,
    source_sha: str,
    repository: str = REPOSITORY,
) -> dict[str, Any]:
    stage_run = _object(stage_run, "candidate stage workflow run")
    stage_repository = stage_run.get("repository")
    stage_repository = stage_repository.get("full_name") if isinstance(stage_repository, dict) else None
    if (
        stage_run.get("id") != expected_run_id
        or stage_run.get("workflow_id") != expected_workflow_id
        or stage_run.get("path") != RELEASE_WORKFLOW_PATH
        or stage_run.get("event") != "workflow_run"
        or stage_run.get("status") != "completed"
        or stage_run.get("conclusion") != "success"
        or stage_run.get("head_branch") != "main"
        or stage_repository != repository
    ):
        raise ValueError("staged candidate was not produced by a successful trusted release workflow run")
    if not SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("staged candidate source must be a full lowercase Git SHA")
    return {
        "id": expected_run_id,
        "workflow_id": expected_workflow_id,
        "path": RELEASE_WORKFLOW_PATH,
        "event": "workflow_run",
        "repository": repository,
        "source_sha": source_sha,
    }


def reconcile_release_assets(
    release: dict[str, Any],
    staging_directory: Path,
    *,
    expected_tag: str,
    source_sha: str,
    tag_target_sha: str,
) -> list[str]:
    release = _object(release, "GitHub release")
    if release.get("tag_name") != expected_tag:
        raise ValueError("staged release tag does not match this candidate")
    if not tag_target_sha and release.get("draft") is True:
        if not SOURCE_SHA.fullmatch(source_sha) or release.get("target_commitish") != source_sha:
            raise ValueError("untagged draft release target differs from the exact source SHA")
    elif tag_target_sha != source_sha:
        raise ValueError("staged release Git tag target SHA differs from the exact source SHA")
    if release.get("prerelease") is not False:
        raise ValueError("standalone release candidate is incorrectly marked as a prerelease")
    expected = _file_records(staging_directory, include_receipt=True)
    release_assets = release.get("assets")
    if not isinstance(release_assets, list):
        raise TypeError("GitHub release assets must be a list")
    observed = {}
    for asset in release_assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise TypeError("GitHub release contains malformed asset metadata")
        if asset["name"] in observed:
            raise ValueError("GitHub release contains duplicate asset names")
        observed[asset["name"]] = asset
    unexpected = set(observed) - set(expected)
    if unexpected:
        raise ValueError(f"staged release contains unexpected assets: {', '.join(sorted(unexpected))}")
    missing = []
    for name, record in expected.items():
        asset = observed.get(name)
        if asset is None:
            missing.append(name)
            continue
        if asset.get("digest") != f"sha256:{record['sha256']}" or asset.get("size") != record["size"]:
            raise ValueError(f"GitHub release asset conflicts with staged release receipt: {name}")
    if release.get("draft") is False and missing:
        raise ValueError("public release is missing staged assets; publication cannot be repaired in place")
    return sorted(missing)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create a receipt for local staged release assets")
    create.add_argument("--directory", type=Path, required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--release-tag", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--checks", type=Path, required=True)
    create.add_argument("--runtime", type=Path, required=True)
    create.add_argument("--release-lock", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--stage-run-id", type=int, required=True)
    create.add_argument("--stage-workflow-id", type=int, required=True)

    verify = commands.add_parser("verify", help="verify downloaded staged assets against the exact candidate")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--release-tag", required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--checks", type=Path, required=True)
    verify.add_argument("--runtime", type=Path, required=True)
    verify.add_argument("--release-lock", type=Path, required=True)
    verify.add_argument("--stage-run-json", type=Path, required=True)
    verify.add_argument("--stage-run-id", type=int, required=True)
    verify.add_argument("--stage-workflow-id", type=int, required=True)

    reconcile = commands.add_parser("reconcile", help="compare staged bytes with existing GitHub release assets")
    reconcile.add_argument("--directory", type=Path, required=True)
    reconcile.add_argument("--release-json", type=Path, required=True)
    reconcile.add_argument("--release-tag", required=True)
    reconcile.add_argument("--source-sha", required=True)
    reconcile.add_argument("--tag-target-sha", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            receipt = build_receipt(
                args.directory,
                source_sha=args.source_sha,
                release_tag=args.release_tag,
                version=args.version,
                checks=_load(args.checks),
                runtime=runtime_identity(_load(args.runtime), _load(args.release_lock)),
                stage_run={
                    "id": args.stage_run_id,
                    "workflow_id": args.stage_workflow_id,
                    "path": RELEASE_WORKFLOW_PATH,
                    "event": "workflow_run",
                    "repository": REPOSITORY,
                    "source_sha": args.source_sha,
                },
            )
            _write(args.output, receipt)
            result = {"status": "PASS", "receipt": str(args.output), "asset_count": len(receipt["assets"])}
        elif args.command == "verify":
            receipt = _load(args.directory / "release-candidate.json")
            stage_run = validate_stage_run(
                _load(args.stage_run_json),
                expected_run_id=args.stage_run_id,
                expected_workflow_id=args.stage_workflow_id,
                source_sha=args.source_sha,
            )
            files = validate_staged_files(
                args.directory,
                receipt,
                source_sha=args.source_sha,
                release_tag=args.release_tag,
                version=args.version,
                checks=_load(args.checks),
                runtime=runtime_identity(_load(args.runtime), _load(args.release_lock)),
                stage_run=stage_run,
            )
            result = {"status": "PASS", "verified_asset_count": len(files)}
        else:
            missing = reconcile_release_assets(
                _load(args.release_json),
                args.directory,
                expected_tag=args.release_tag,
                source_sha=args.source_sha,
                tag_target_sha=args.tag_target_sha,
            )
            result = {"status": "PASS", "missing_assets": missing}
    except Exception as error:  # noqa: BLE001 - report blocked staging/publish attempts.
        result = {"status": "BLOCKED", "error": f"{type(error).__name__}: {error}"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
