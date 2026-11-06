#!/usr/bin/env python3
"""Resolve and verify the current main branch's immutable release stage."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

# Direct script invocation puts scripts/ rather than the repository root on
# sys.path. Anchor imports to this trusted checkout before importing modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.release_staging import (
    RELEASE_WORKFLOW_PATH,
    REPOSITORY,
    _file_records,
    validate_stage_run,
)
from scripts.verify_release_readiness import CI_REQUIRED_CHECKS, CI_WORKFLOW_PATH

SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
STAGE_ARTIFACT = "release-stage-files"


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _page_items(
    value: Any, key: str, extra_pages: list[Any] | None = None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in [value, *(extra_pages or [])]:
        if isinstance(page, dict) and isinstance(page.get(key), list):
            page_items = page[key]
        elif (
            isinstance(page, list)
            and page
            and all(
                isinstance(item, dict) and isinstance(item.get(key), list)
                for item in page
            )
        ):
            page_items = [item for nested in page for item in nested[key]]
        elif isinstance(page, list):
            page_items = page
        else:
            raise ValueError(f"GitHub response is missing its {key} list")
        if any(not isinstance(item, dict) for item in page_items):
            raise ValueError(f"GitHub {key} response contains a malformed item")
        items.extend(page_items)
    return items


def _full_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("full_name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def select_stage_run(
    runs: Any,
    workflow_id: int,
    *,
    source_sha: str,
    pages: list[Any] | None = None,
    requested_run_id: int | None = None,
    repository: str = REPOSITORY,
) -> dict[str, Any]:
    candidates = []
    seen: dict[int, dict[str, Any]] = {}
    for run in _page_items(runs, "workflow_runs", pages):
        run_id = run.get("id")
        if type(run_id) is int:
            if run_id in seen:
                raise ValueError("duplicate release workflow run in paginated results")
            seen[run_id] = run
        if (
            run.get("workflow_id") == workflow_id
            and run.get("path") == RELEASE_WORKFLOW_PATH
            and run.get("event") == "workflow_run"
            and run.get("head_branch") == "main"
            and run.get("head_sha") == source_sha
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and _full_name(run.get("repository")) == repository
        ):
            candidates.append(run)
    if requested_run_id is not None:
        matches = [run for run in candidates if run.get("id") == requested_run_id]
        if len(matches) != 1:
            raise ValueError(
                "requested stage run is not a unique trusted release workflow run"
            )
        selected = matches[0]
    else:
        if not candidates:
            raise ValueError(
                "no trusted release stage workflow run is available for current main"
            )
        selected = max(candidates, key=lambda run: run.get("id", 0))
    if type(selected.get("id")) is not int or selected["id"] < 1:
        raise ValueError("release stage workflow run has no valid run ID")
    return selected


def select_stage_artifact(
    artifacts: Any,
    stage_run_id: int,
    *,
    pages: list[Any] | None = None,
) -> dict[str, Any]:
    matches = [
        artifact
        for artifact in _page_items(artifacts, "artifacts", pages)
        if artifact.get("name") == STAGE_ARTIFACT
    ]
    if not matches:
        raise ValueError(
            "newest matching stage run is missing its release-stage-files artifact"
        )
    if len(matches) != 1:
        raise ValueError("stage run contains duplicate release-stage-files artifacts")
    artifact = matches[0]
    workflow_run = artifact.get("workflow_run")
    if isinstance(workflow_run, dict) and workflow_run.get("id") != stage_run_id:
        raise ValueError("release stage artifact belongs to a different workflow run")
    if artifact.get("expired") is not False:
        raise ValueError(
            "release-stage-files artifact is expired or has unknown expiry"
        )
    if type(artifact.get("id")) is not int or artifact["id"] < 1:
        raise ValueError("release-stage-files artifact has no valid artifact ID")
    return artifact


def _source_version(root: Path) -> str:
    package_paths = (
        root / "vscode-extension/package.json",
        root / "templates/room/vscode-extension/package.json",
    )
    runtime_paths = (
        root / "vscode-extension/runtime/manifest.json",
        root / "templates/room/vscode-extension/runtime/manifest.json",
    )
    versions = [
        json.loads(path.read_text(encoding="utf-8")).get("version")
        for path in package_paths
    ]
    runtime_versions = [
        json.loads(path.read_text(encoding="utf-8")).get("extension_version")
        for path in runtime_paths
    ]
    if len(set(versions + runtime_versions)) != 1:
        raise ValueError(
            "current main package and runtime versions are not synchronized"
        )
    version = versions[0]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError("current main has an invalid stable extension version")
    return version


def verify_stage_artifact(
    root: Path,
    directory: Path,
    stage_run: dict[str, Any],
    *,
    source_sha: str,
    stage_run_id: int,
    stage_workflow_id: int,
    ci_workflow_id: int,
    version: str,
) -> dict[str, Any]:
    if _source_version(root) != version:
        raise ValueError("selected candidate version differs from current main")
    normalized_stage = validate_stage_run(
        stage_run,
        expected_run_id=stage_run_id,
        expected_workflow_id=stage_workflow_id,
        source_sha=source_sha,
    )
    receipt = _object(
        json.loads((directory / "release-candidate.json").read_text(encoding="utf-8")),
        "staged release receipt",
    )
    if (
        receipt.get("schema_version") != 2
        or receipt.get("source_sha") != source_sha
        or receipt.get("version") != version
        or receipt.get("release_tag") != f"v{version}-standalone-vsix"
    ):
        raise ValueError(
            "staged release receipt source SHA or version differs from current main"
        )
    if receipt.get("stage_run") != normalized_stage:
        raise ValueError(
            "staged release receipt belongs to a different stage workflow run"
        )
    checks = _object(receipt.get("checks"), "staged CI check receipt")
    workflow = _object(checks.get("workflow"), "staged CI workflow receipt")
    ci_run_id = workflow.get("run_id")
    check_receipts = _object(checks.get("checks"), "staged required CI checks")
    if (
        checks.get("source_sha") != source_sha
        or workflow.get("id") != ci_workflow_id
        or workflow.get("path") != CI_WORKFLOW_PATH
        or workflow.get("repository") != REPOSITORY
        or workflow.get("head_sha") != source_sha
        or type(ci_run_id) is not int
        or ci_run_id < 1
        or set(check_receipts) != CI_REQUIRED_CHECKS
    ):
        raise ValueError(
            "staged CI receipt is incomplete or belongs to a different source"
        )
    for name, check in check_receipts.items():
        if (
            not isinstance(check, dict)
            or check.get("head_sha") != source_sha
            or check.get("status") != "completed"
            or check.get("conclusion") != "success"
            or check.get("run_id") != ci_run_id
            or check.get("app_id") != 15368
        ):
            raise ValueError(
                f"staged CI receipt is not successful for required check {name}"
            )
    assets = _object(receipt.get("assets"), "staged release assets")
    if _file_records(directory) != assets:
        raise ValueError(
            "staged asset digests differ from their immutable release receipt"
        )
    return {
        "source_sha": source_sha,
        "stage_run_id": stage_run_id,
        "artifact_id": None,
        "version": version,
        "ci_run_id": ci_run_id,
        "checks": checks,
    }


def _gh_json(endpoint: str, *, paginate: bool = False) -> Any:
    command = ["gh", "api"]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    command.append(endpoint)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("GitHub read request failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub returned malformed JSON") from exc


def _current_main_sha(root: Path) -> str:
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    remote = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "origin", "refs/heads/main"],
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode or remote.returncode:
        raise ValueError("could not verify checked out source against current main")
    head_sha = head.stdout.strip()
    remote_sha = remote.stdout.split()[0] if remote.stdout.split() else ""
    if not SHA.fullmatch(head_sha) or head_sha != remote_sha:
        raise ValueError(
            "checked out source is stale; release approval requires current main"
        )
    return head_sha


def _download_artifact(artifact_id: int, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("release artifact destination must be empty")
    archive_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="release-stage-", suffix=".zip", dir=directory.parent, delete=False
        ) as archive:
            archive_path = Path(archive.name)
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip",
                ],
                check=False,
                stdout=archive,
                stderr=subprocess.PIPE,
                text=False,
            )
            if result.returncode:
                raise ValueError(
                    "GitHub could not download the selected immutable stage artifact"
                )
        _extract_flat_artifact(archive_path, directory)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(
            "GitHub could not download the selected immutable stage artifact"
        ) from exc
    finally:
        if archive_path is not None:
            archive_path.unlink(missing_ok=True)


def _extract_flat_artifact(archive_path: Path, directory: Path) -> None:
    names: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            relative = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                relative.is_absolute()
                or len(relative.parts) != 1
                or "\\" in item.filename
                or any(part in {"", ".", ".."} for part in relative.parts)
                or item.is_dir()
                or stat.S_ISLNK(mode)
                or item.flag_bits & 1
            ):
                raise ValueError("release stage artifact contains an unsafe file entry")
            if item.filename in names:
                raise ValueError(
                    "release stage artifact contains duplicate file entries"
                )
            names.add(item.filename)
        if not names:
            raise ValueError("release stage artifact contains no files")
        for item in archive.infolist():
            target = directory / item.filename
            with archive.open(item) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)


def _run_verifiers(
    root: Path,
    directory: Path,
    verified: dict[str, Any],
    stage_workflow_id: int,
    ci_workflow_id: int,
) -> tuple[str, str]:
    repo_api = f"repos/{REPOSITORY}"
    ci_run_id = verified["ci_run_id"]
    workflow = _gh_json(f"{repo_api}/actions/runs/{ci_run_id}")
    jobs = _gh_json(
        f"{repo_api}/actions/runs/{ci_run_id}/jobs?per_page=100", paginate=True
    )
    check_runs = _gh_json(
        f"{repo_api}/commits/{verified['source_sha']}/check-runs?per_page=100",
        paginate=True,
    )
    controller_tag = json.loads(
        (root / "release-lock.json").read_text(encoding="utf-8")
    )["controller"]["release_tag"]
    jat_tag = json.loads((root / "release-lock.json").read_text(encoding="utf-8"))[
        "jat"
    ]["environment_artifact"]["release_tag"]
    controller_release = _gh_json(f"{repo_api}/releases/tags/{controller_tag}")
    jat_release = _gh_json(
        f"repos/joshyorko/josh-all-the-things/releases/tags/{jat_tag}"
    )
    stage_run_api = _gh_json(f"{repo_api}/actions/runs/{verified['stage_run_id']}")
    with tempfile.TemporaryDirectory(
        prefix="josh-room-release-resolver-", dir=directory.parent
    ) as temporary:
        temp = Path(temporary)
        paths = _write_readiness_inputs(
            temp,
            workflow=workflow,
            jobs=jobs,
            check_runs=check_runs,
            controller_release=controller_release,
            jat_release=jat_release,
            stage_run=stage_run_api,
        )
        controller_sha = _tag_sha(
            f"https://github.com/{REPOSITORY}.git", controller_tag
        )
        jat_sha = _tag_sha(
            "https://github.com/joshyorko/josh-all-the-things.git", jat_tag
        )
        readiness = temp / "readiness.json"
        command = [
            sys.executable,
            str(root / "scripts/verify_release_readiness.py"),
            "--source-sha",
            verified["source_sha"],
            "--workflow-id",
            str(ci_workflow_id),
            "--workflow-run",
            str(paths["ci-run.json"]),
            "--workflow-jobs",
            str(paths["ci-jobs.json"]),
            "--check-runs",
            str(paths["check-runs.json"]),
            "--runtime",
            str(root / "vscode-extension/runtime/manifest.json"),
            "--release-lock",
            str(root / "release-lock.json"),
            "--controller-release",
            str(paths["controller-release.json"]),
            "--jat-release",
            str(paths["jat-release.json"]),
            "--controller-tag-sha",
            controller_sha,
            "--jat-tag-sha",
            jat_sha,
            "--report",
            str(readiness),
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode:
            _retain_verifier_diagnostic(
                directory,
                "release-readiness",
                result,
                report_path=readiness,
            )
            raise ValueError(
                "exact-source CI or runtime release readiness verification failed"
            )
        verify_command = [
            sys.executable,
            "-m",
            "scripts.release_staging",
            "verify",
            "--directory",
            str(directory),
            "--source-sha",
            verified["source_sha"],
            "--release-tag",
            f"v{verified['version']}-standalone-vsix",
            "--version",
            verified["version"],
            "--checks",
            str(readiness),
            "--runtime",
            str(root / "vscode-extension/runtime/manifest.json"),
            "--release-lock",
            str(root / "release-lock.json"),
            "--stage-run-json",
            str(paths["stage-run.json"]),
            "--stage-run-id",
            str(verified["stage_run_id"]),
            "--stage-workflow-id",
            str(stage_workflow_id),
        ]
        result = subprocess.run(
            verify_command, cwd=root, check=False, capture_output=True, text=True
        )
        if result.returncode:
            _retain_verifier_diagnostic(
                directory,
                "release-staging",
                result,
            )
            raise ValueError(
                "staged receipt, source, runtime pins, or asset digests failed verification"
            )
        ci_url = workflow.get("html_url")
        stage_url = stage_run_api.get("html_url")
        if not isinstance(ci_url, str) or not isinstance(stage_url, str):
            raise TypeError("GitHub omitted workflow run links from release evidence")
        return stage_url, ci_url


def _write_readiness_inputs(
    directory: Path,
    *,
    workflow: dict[str, Any],
    jobs: Any,
    check_runs: Any,
    controller_release: dict[str, Any],
    jat_release: dict[str, Any],
    stage_run: dict[str, Any],
) -> dict[str, Path]:
    values = {
        "ci-run.json": workflow,
        "ci-jobs.json": {"jobs": _page_items(jobs, "jobs")},
        "check-runs.json": {"check_runs": _page_items(check_runs, "check_runs")},
        "controller-release.json": controller_release,
        "jat-release.json": jat_release,
        "stage-run.json": stage_run,
    }
    paths = {}
    for name, value in values.items():
        paths[name] = directory / name
        paths[name].write_text(json.dumps(value), encoding="utf-8")
    return paths


def _retain_verifier_diagnostic(
    directory: Path,
    name: str,
    result: subprocess.CompletedProcess[str],
    *,
    report_path: Path | None = None,
) -> None:
    report = None
    if report_path is not None and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {"status": "unreadable"}
    diagnostic = {
        "verifier": name,
        "exit_code": result.returncode,
        "report": report,
        "stderr": result.stderr[-4096:],
    }
    path = directory.parent / f"{name}-failure.json"
    if path.exists() or path.is_symlink():
        raise ValueError(
            "verification diagnostics already exist beside the staged artifact"
        )
    path.write_text(
        json.dumps(diagnostic, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _tag_sha(repository_url: str, tag: str) -> str:
    for ref in (f"refs/tags/{tag}^{{}}", f"refs/tags/{tag}"):
        result = subprocess.run(
            ["git", "ls-remote", repository_url, ref],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise ValueError("could not verify an immutable runtime release tag")
        if result.stdout.split():
            value = result.stdout.split()[0]
            if SHA.fullmatch(value):
                return value
    raise ValueError("runtime release tag has no valid Git target")


def resolve_candidate(
    *,
    root: Path,
    source_sha: str,
    directory: Path,
    output: Path,
    stage_run_id: int | None = None,
) -> dict[str, Any]:
    if not SHA.fullmatch(source_sha):
        raise ValueError("source SHA must be a full lowercase Git SHA")
    if stage_run_id is not None and (type(stage_run_id) is not int or stage_run_id < 1):
        raise ValueError("stage run ID must be a positive integer")
    root = root.resolve()
    if _current_main_sha(root) != source_sha:
        raise ValueError("requested source SHA is not current main")
    version = _source_version(root)
    stage_workflow = _gh_json(f"repos/{REPOSITORY}/actions/workflows/release.yml")
    stage_workflow_id = stage_workflow.get("id")
    ci_workflow = _gh_json(f"repos/{REPOSITORY}/actions/workflows/ci.yml")
    ci_workflow_id = ci_workflow.get("id")
    if type(stage_workflow_id) is not int or type(ci_workflow_id) is not int:
        raise ValueError("GitHub did not return trusted workflow IDs")
    run_pages = _gh_json(
        f"repos/{REPOSITORY}/actions/workflows/{stage_workflow_id}/runs?branch=main&head_sha={source_sha}&per_page=100",
        paginate=True,
    )
    selected_run = select_stage_run(
        run_pages,
        stage_workflow_id,
        source_sha=source_sha,
        requested_run_id=stage_run_id,
    )
    selected_stage_id = selected_run["id"]
    artifact_pages = _gh_json(
        f"repos/{REPOSITORY}/actions/runs/{selected_stage_id}/artifacts?per_page=100",
        paginate=True,
    )
    artifact = select_stage_artifact(artifact_pages, selected_stage_id)
    _download_artifact(artifact["id"], directory)
    verified = verify_stage_artifact(
        root,
        directory,
        selected_run,
        source_sha=source_sha,
        stage_run_id=selected_stage_id,
        stage_workflow_id=stage_workflow_id,
        ci_workflow_id=ci_workflow_id,
        version=version,
    )
    verified["artifact_id"] = artifact["id"]
    stage_url, ci_url = _run_verifiers(
        root,
        directory,
        verified,
        stage_workflow_id,
        ci_workflow_id,
    )
    report = {
        **verified,
        "stage_url": stage_url,
        "ci_url": ci_url,
        "status": "verified",
    }
    if output.exists() or output.is_symlink():
        raise ValueError("release candidate report destination already exists")
    if _current_main_sha(root) != source_sha:
        raise ValueError("main advanced during release candidate verification")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--stage-run-id", type=int)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = resolve_candidate(
            root=args.root,
            source_sha=args.source_sha,
            stage_run_id=args.stage_run_id,
            directory=args.directory,
            output=args.output,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except (
        ValueError,
        TypeError,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        print(f"release-candidate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
