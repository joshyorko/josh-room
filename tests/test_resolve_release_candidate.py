from __future__ import annotations

import hashlib
import json
import shutil
import stat
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.resolve_release_candidate import (
    RELEASE_WORKFLOW_PATH,
    REPOSITORY,
    _download_artifact,
    _extract_flat_artifact,
    _gh_json,
    _retain_verifier_diagnostic,
    _write_readiness_inputs,
    main,
    select_stage_artifact,
    select_stage_run,
    verify_stage_artifact,
)
from scripts.verify_release_readiness import CI_REQUIRED_CHECKS, CI_WORKFLOW_PATH

SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
STAGE_WORKFLOW_ID = 88
CI_WORKFLOW_ID = 77
STAGE_RUN_ID = 201
CI_RUN_ID = 301
VERSION = "0.1.43"


def stage_run(**overrides):
    return {
        "id": STAGE_RUN_ID,
        "workflow_id": STAGE_WORKFLOW_ID,
        "path": RELEASE_WORKFLOW_PATH,
        "event": "workflow_run",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "main",
        "head_sha": SOURCE_SHA,
        "repository": {"full_name": REPOSITORY},
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{STAGE_RUN_ID}",
        **overrides,
    }


def ci_checks():
    return {
        "source_sha": SOURCE_SHA,
        "workflow": {
            "id": CI_WORKFLOW_ID,
            "path": CI_WORKFLOW_PATH,
            "run_id": CI_RUN_ID,
            "run_attempt": 1,
            "event": "push",
            "branch": "main",
            "repository": REPOSITORY,
            "head_sha": SOURCE_SHA,
        },
        "checks": {
            name: {
                "id": 1000 + index,
                "status": "completed",
                "conclusion": "success",
                "head_sha": SOURCE_SHA,
                "app_id": 15368,
                "run_id": CI_RUN_ID,
            }
            for index, name in enumerate(sorted(CI_REQUIRED_CHECKS))
        },
    }


def source_tree(tmp_path):
    for relative in (
        "vscode-extension/package.json",
        "templates/room/vscode-extension/package.json",
        "vscode-extension/runtime/manifest.json",
        "templates/room/vscode-extension/runtime/manifest.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(__file__).parents[1] / relative).read_bytes())


def artifact_directory(
    tmp_path, *, source_sha=SOURCE_SHA, stage_id=STAGE_RUN_ID, **receipt_changes
):
    source_tree(tmp_path / "checkout")
    root = tmp_path / "checkout"
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "SHA256SUMS").write_text("synthetic staged bytes\n")
    asset = directory / "SHA256SUMS"
    package = json.loads((root / "vscode-extension/package.json").read_text())
    json.loads((root / "vscode-extension/runtime/manifest.json").read_text())
    version = package["version"]
    stage_metadata = stage_run(id=stage_id)
    normalized_stage = {
        "id": stage_id,
        "workflow_id": STAGE_WORKFLOW_ID,
        "path": RELEASE_WORKFLOW_PATH,
        "event": "workflow_run",
        "repository": REPOSITORY,
        "source_sha": source_sha,
    }
    checks = ci_checks()
    checks["source_sha"] = source_sha
    checks["workflow"]["head_sha"] = source_sha
    for check in checks["checks"].values():
        check["head_sha"] = source_sha
    receipt = {
        "schema_version": 2,
        "source_sha": source_sha,
        "release_tag": f"v{version}-standalone-vsix",
        "version": version,
        "checks": checks,
        "stage_run": normalized_stage,
        "runtime": {},
        "node_runtime": {},
        "actions_runtime": {},
        "assets": {
            asset.name: {
                "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                "size": asset.stat().st_size,
            }
        },
    }
    receipt.update(receipt_changes)
    (directory / "release-candidate.json").write_text(json.dumps(receipt))
    return root, directory, stage_metadata, version


def test_selects_newest_trusted_successful_stage_run_from_paginated_results():
    older = stage_run(id=200)
    newer = stage_run(id=201)
    result = select_stage_run(
        [
            {"total_count": 2, "workflow_runs": [older]},
            {"total_count": 2, "workflow_runs": [newer]},
        ],
        STAGE_WORKFLOW_ID,
        source_sha=SOURCE_SHA,
    )
    assert result == newer


@pytest.mark.parametrize(
    "run",
    [
        stage_run(workflow_id=999),
        stage_run(path=".github/workflows/other.yml"),
        stage_run(event="workflow_dispatch"),
        stage_run(head_branch="feature/example"),
        stage_run(repository={"full_name": "other/repository"}),
        stage_run(status="in_progress", conclusion=None),
        stage_run(conclusion="failure"),
    ],
)
def test_rejects_untrusted_wrong_source_mode_or_unfinished_runs(run):
    with pytest.raises(ValueError):
        select_stage_run([run], STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA)


def test_newer_failed_run_does_not_hide_newest_successful_exact_source_run():
    successful = stage_run(id=200)
    failed = stage_run(id=201, conclusion="failure")
    assert (
        select_stage_run([successful, failed], STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA)
        == successful
    )


def test_wrong_source_run_is_excluded_even_when_newer():
    selected = stage_run(id=200)
    wrong_source = stage_run(id=201, head_sha=OTHER_SHA)
    assert (
        select_stage_run(
            [selected, wrong_source], STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA
        )
        == selected
    )


def test_exact_stage_override_must_name_the_latest_or_explicit_matching_run():
    runs = [stage_run(id=200), stage_run(id=201)]
    assert (
        select_stage_run(
            runs, STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA, requested_run_id=200
        )["id"]
        == 200
    )
    with pytest.raises(ValueError, match="requested stage"):
        select_stage_run(
            runs, STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA, requested_run_id=199
        )


def test_rejects_duplicate_run_ids_in_paginated_api_results():
    run = stage_run()
    with pytest.raises(ValueError, match="duplicate"):
        select_stage_run([run, run], STAGE_WORKFLOW_ID, source_sha=SOURCE_SHA)


def test_selects_unique_unexpired_stage_artifact_from_paginated_results():
    result = select_stage_artifact(
        [
            {
                "total_count": 2,
                "artifacts": [
                    {"id": 30, "name": "release-stage-files", "expired": False}
                ],
            },
            {
                "total_count": 2,
                "artifacts": [{"id": 31, "name": "other", "expired": False}],
            },
        ],
        STAGE_RUN_ID,
    )
    assert result["id"] == 30


@pytest.mark.parametrize(
    "artifacts, message",
    [
        ({"artifacts": []}, "missing"),
        (
            {"artifacts": [{"id": 30, "name": "release-stage-files", "expired": True}]},
            "expired",
        ),
        (
            {
                "artifacts": [
                    {"id": 30, "name": "release-stage-files", "expired": False},
                    {"id": 31, "name": "release-stage-files", "expired": False},
                ]
            },
            "duplicate",
        ),
    ],
)
def test_rejects_missing_expired_and_duplicate_stage_artifacts(artifacts, message):
    with pytest.raises(ValueError, match=message):
        select_stage_artifact(artifacts, STAGE_RUN_ID)


def test_missing_artifact_on_newest_successful_run_does_not_fall_back_to_older_stage():
    older = stage_run(id=200)
    newest = stage_run(id=201)
    selected = select_stage_run(
        [{"workflow_runs": [older, newest]}],
        STAGE_WORKFLOW_ID,
        source_sha=SOURCE_SHA,
    )
    with pytest.raises(ValueError, match="different workflow run"):
        select_stage_artifact(
            {
                "artifacts": [
                    {
                        "id": 30,
                        "name": "release-stage-files",
                        "expired": False,
                        "workflow_run": {"id": older["id"]},
                    }
                ]
            },
            selected["id"],
        )


def test_verifies_stage_artifact_source_version_ci_receipt_and_asset_hashes(tmp_path):
    root, directory, run, version = artifact_directory(tmp_path)
    result = verify_stage_artifact(
        root,
        directory,
        run,
        source_sha=SOURCE_SHA,
        stage_run_id=STAGE_RUN_ID,
        stage_workflow_id=STAGE_WORKFLOW_ID,
        ci_workflow_id=CI_WORKFLOW_ID,
        version=version,
    )
    assert result["source_sha"] == SOURCE_SHA
    assert result["stage_run_id"] == STAGE_RUN_ID
    assert result["ci_run_id"] == CI_RUN_ID


@pytest.mark.parametrize(
    "receipt_changes, source_sha, message",
    [
        ({}, "b" * 40, "source SHA"),
        ({"stage_run": {"id": 999}}, SOURCE_SHA, "different stage"),
        (
            {"checks": {"source_sha": SOURCE_SHA, "workflow": {}, "checks": {}}},
            SOURCE_SHA,
            "CI receipt",
        ),
    ],
)
def test_rejects_stale_or_incomplete_stage_receipts(
    tmp_path, receipt_changes, source_sha, message
):
    root, directory, run, version = artifact_directory(
        tmp_path, source_sha=SOURCE_SHA, **receipt_changes
    )
    with pytest.raises(ValueError, match=message):
        verify_stage_artifact(
            root,
            directory,
            run,
            source_sha=source_sha,
            stage_run_id=STAGE_RUN_ID,
            stage_workflow_id=STAGE_WORKFLOW_ID,
            ci_workflow_id=CI_WORKFLOW_ID,
            version=version,
        )


def test_rejects_artifact_digest_mismatch_without_echoing_untrusted_data(tmp_path):
    root, directory, run, version = artifact_directory(tmp_path)
    (directory / "SHA256SUMS").write_text("tampered\n")
    with pytest.raises(ValueError, match="asset digests differ") as error:
        verify_stage_artifact(
            root,
            directory,
            run,
            source_sha=SOURCE_SHA,
            stage_run_id=STAGE_RUN_ID,
            stage_workflow_id=STAGE_WORKFLOW_ID,
            ci_workflow_id=CI_WORKFLOW_ID,
            version=version,
        )
    assert "token" not in str(error.value).lower()


def test_actual_script_entrypoint_rejects_malformed_sha_without_network(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/resolve_release_candidate.py"),
            "--root",
            str(tmp_path),
            "--source-sha",
            "not-a-sha",
            "--directory",
            str(tmp_path / "artifact"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "source SHA must be a full lowercase Git SHA" in result.stderr


def test_cli_downloads_and_validates_the_selected_artifact_id(
    tmp_path, monkeypatch, capsys
):
    root, source_artifact, run, version = artifact_directory(tmp_path)
    target = tmp_path / "downloaded"
    output = tmp_path / "report.json"
    artifact = {"id": 30, "name": "release-stage-files", "expired": False}
    calls = []

    def api(endpoint, *, paginate=False):
        calls.append(endpoint)
        if endpoint.endswith("/workflows/release.yml"):
            return {"id": STAGE_WORKFLOW_ID}
        if endpoint.endswith("/workflows/ci.yml"):
            return {"id": CI_WORKFLOW_ID}
        if "/actions/workflows/88/runs?" in endpoint:
            return [{"workflow_runs": [run]}]
        if endpoint.endswith("/actions/runs/201/artifacts?per_page=100"):
            return [{"artifacts": [artifact]}]
        raise AssertionError(f"unexpected GitHub endpoint: {endpoint}")

    def download(artifact_id, directory):
        assert artifact_id == artifact["id"]
        shutil.copytree(source_artifact, directory)

    monkeypatch.setattr(
        "scripts.resolve_release_candidate._current_main_sha", lambda _root: SOURCE_SHA
    )
    monkeypatch.setattr("scripts.resolve_release_candidate._gh_json", api)
    monkeypatch.setattr(
        "scripts.resolve_release_candidate._download_artifact", download
    )
    monkeypatch.setattr(
        "scripts.resolve_release_candidate._run_verifiers",
        lambda *_args: (
            "https://github.com/example/stage",
            "https://github.com/example/ci",
        ),
    )

    assert (
        main(
            [
                "--root",
                str(root),
                "--source-sha",
                SOURCE_SHA,
                "--directory",
                str(target),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    assert report["artifact_id"] == 30
    assert report["stage_run_id"] == STAGE_RUN_ID
    assert report["version"] == version
    assert report["source_sha"] == SOURCE_SHA
    assert report["ci_run_id"] == CI_RUN_ID
    assert calls[2].endswith(f"head_sha={SOURCE_SHA}&per_page=100")
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_paginated_api_error_is_generic_and_does_not_echo_token(monkeypatch, capsys):
    token = "synthetic-token-do-not-print"

    def failed(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f"failed with {token}"
        )

    monkeypatch.setattr("scripts.resolve_release_candidate.subprocess.run", failed)
    with pytest.raises(ValueError, match="GitHub read request failed") as error:
        _gh_json("repos/example/repository/actions/runs", paginate=True)
    assert token not in str(error.value)
    assert token not in capsys.readouterr().out


def test_verifier_failure_retains_safe_report_and_stderr_beside_artifact(tmp_path):
    artifact_directory = tmp_path / "stage"
    artifact_directory.mkdir()
    report = tmp_path / "readiness.json"
    report.write_text(
        json.dumps({"status": "BLOCKED", "error": "synthetic check failed"})
    )
    result = subprocess.CompletedProcess(
        ["python", "verifier"],
        1,
        stdout="ignored stdout",
        stderr="safe synthetic stderr",
    )

    _retain_verifier_diagnostic(
        artifact_directory,
        "release-readiness",
        result,
        report_path=report,
    )

    diagnostic = json.loads((tmp_path / "release-readiness-failure.json").read_text())
    assert diagnostic == {
        "verifier": "release-readiness",
        "exit_code": 1,
        "report": {"status": "BLOCKED", "error": "synthetic check failed"},
        "stderr": "safe synthetic stderr",
    }
    assert "ignored stdout" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "entry", ["../escape", "/absolute", "folder/file", r"folder\escape"]
)
def test_artifact_zip_rejects_paths_outside_flat_destination(tmp_path, entry):
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(entry, "unsafe")
    with pytest.raises(ValueError, match="unsafe file entry"):
        _extract_flat_artifact(archive_path, tmp_path / "unpacked")


def test_artifact_zip_rejects_symlinks_and_duplicate_entries(tmp_path):
    symlink_archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink_archive, "w") as archive:
        archive.writestr(link, "target")
    with pytest.raises(ValueError, match="unsafe file entry"):
        _extract_flat_artifact(symlink_archive, tmp_path / "symlink-out")

    duplicate_archive = tmp_path / "duplicate.zip"

    def write_duplicate_entries():
        with zipfile.ZipFile(duplicate_archive, "w") as archive:
            archive.writestr("one.txt", "first")
            archive.writestr("one.txt", "second")

    with pytest.warns(UserWarning, match="Duplicate name"):
        write_duplicate_entries()
    with pytest.raises(ValueError, match="duplicate"):
        _extract_flat_artifact(duplicate_archive, tmp_path / "duplicate-out")


def test_download_uses_selected_artifact_id_endpoint(monkeypatch, tmp_path):
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("release-candidate.json", "{}")
    zip_bytes = zip_buffer.getvalue()
    calls = []

    def fake_run(command, *, stdout, stderr, text, check):
        calls.append(command)
        stdout.write(zip_bytes)
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    monkeypatch.setattr("scripts.resolve_release_candidate.subprocess.run", fake_run)
    target = tmp_path / "artifact"
    _download_artifact(30, target)

    assert calls == [["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/30/zip"]]
    assert (target / "release-candidate.json").read_text() == "{}"


def test_readiness_cli_accepts_paginated_job_and_check_shapes(tmp_path):
    root = Path(__file__).parents[1]
    workflow_id = CI_WORKFLOW_ID
    run = {
        "id": CI_RUN_ID,
        "workflow_id": workflow_id,
        "path": CI_WORKFLOW_PATH,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "head_sha": SOURCE_SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
    }
    jobs = []
    checks = []
    for index, name in enumerate(sorted(CI_REQUIRED_CHECKS), start=1):
        check_id = 1000 + index
        url = f"https://github.com/{REPOSITORY}/actions/runs/{CI_RUN_ID}/job/{check_id}"
        jobs.append(
            {
                "id": check_id,
                "name": name,
                "run_id": CI_RUN_ID,
                "head_sha": SOURCE_SHA,
                "status": "completed",
                "conclusion": "success",
                "html_url": url,
            }
        )
        checks.append(
            {
                "id": check_id,
                "name": name,
                "head_sha": SOURCE_SHA,
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 15368, "slug": "github-actions"},
                "details_url": url,
            }
        )
    paths = _write_readiness_inputs(
        tmp_path,
        workflow=run,
        jobs=[{"jobs": jobs}],
        check_runs=[{"check_runs": checks}],
        controller_release={},
        jat_release={},
        stage_run={},
    )
    runtime = tmp_path / "runtime.json"
    release_lock = tmp_path / "release-lock.json"
    runtime.write_text("{}")
    release_lock.write_text("{}")
    report = tmp_path / "readiness.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/verify_release_readiness.py"),
            "--source-sha",
            SOURCE_SHA,
            "--workflow-id",
            str(workflow_id),
            "--workflow-run",
            str(paths["ci-run.json"]),
            "--workflow-jobs",
            str(paths["ci-jobs.json"]),
            "--check-runs",
            str(paths["check-runs.json"]),
            "--runtime",
            str(runtime),
            "--release-lock",
            str(release_lock),
            "--report",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert (
        json.loads(report.read_text())["error"]
        == "TypeError: controller RCC pin must be a JSON object"
    )
    assert "unsupported shape" not in result.stdout
