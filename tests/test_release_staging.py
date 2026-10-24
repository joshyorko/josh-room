from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.release_staging import (
    ACTIONS_RUNTIME_FILE,
    MINIMUM_NODE_VERSION,
    actions_runtime_identity,
    build_receipt,
    node_runtime_identity,
    reconcile_release_assets,
    runtime_identity,
    validate_stage_run,
    validate_staged_files,
)
from scripts.verify_release_readiness import CI_REQUIRED_CHECKS

SOURCE_SHA = "a" * 40
ACTION_COMMIT_SHA = "949feb2413d6458794dcd2491c4babbbce0c15c1"
ACTIONS_RUNTIME = {
    "schema_version": 1,
    "action": "actions/setup-node",
    "ref": "v7",
    "commit_sha": ACTION_COMMIT_SHA,
    "declared_runtime": "node24",
    "runtime_source": f"https://raw.githubusercontent.com/actions/setup-node/{ACTION_COMMIT_SHA}/action.yml",
}
NODE_RUNTIME = {
    "schema_version": 1,
    "node_version": MINIMUM_NODE_VERSION,
    "npm_version": "10.9.4",
    "minimum": MINIMUM_NODE_VERSION,
    "satisfied": True,
    "source_sha": SOURCE_SHA,
    "actions": {
        "github_actions": True,
        "runner_os": "Linux",
        "runner_arch": "X64",
        "image_os": "ubuntu24",
        "image_version": "20261020.1.0",
    },
}
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
CHECKS = {
    "source_sha": SOURCE_SHA,
    "workflow": {
        "id": 77,
        "path": ".github/workflows/ci.yml",
        "run_id": 101,
        "run_attempt": 1,
        "event": "push",
        "branch": "main",
        "repository": "joshyorko/josh-room",
        "head_sha": SOURCE_SHA,
    },
    "checks": {
        name: {"id": 101 + index, "status": "completed", "conclusion": "success", "head_sha": SOURCE_SHA, "run_id": 101, "app_id": 15368}
        for index, name in enumerate(sorted(CI_REQUIRED_CHECKS))
    },
}
STAGE_RUN = {
    "id": 202,
    "workflow_id": 88,
    "path": ".github/workflows/release.yml",
    "event": "workflow_run",
    "repository": "joshyorko/josh-room",
    "source_sha": SOURCE_SHA,
}
STAGE_RUN_API = {
    **STAGE_RUN,
    "status": "completed",
    "conclusion": "success",
    "head_branch": "main",
    "repository": {"full_name": "joshyorko/josh-room"},
}


def _files(directory):
    directory.mkdir(parents=True)
    (directory / "josh-room-0.1.33.vsix").write_bytes(b"synthetic vsix")
    (directory / "josh_room-0.1.33-py3-none-any.whl").write_bytes(b"synthetic wheel")
    (directory / "josh_room-0.1.33.tar.gz").write_bytes(b"synthetic sdist")
    (directory / "SHA256SUMS").write_text("synthetic checksums\n")
    (directory / "node-runtime.json").write_text(json.dumps(NODE_RUNTIME))
    (directory / ACTIONS_RUNTIME_FILE).write_text(json.dumps(ACTIONS_RUNTIME))


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
    node_identity = node_runtime_identity(staged, source_sha=SOURCE_SHA)
    actions_identity = actions_runtime_identity(staged)
    receipt = build_receipt(
        staged,
        source_sha=SOURCE_SHA,
        release_tag="v0.1.33-standalone-vsix",
        version="0.1.33",
        checks=CHECKS,
        runtime=identity,
        node_runtime=node_identity,
        actions_runtime=actions_identity,
        stage_run=STAGE_RUN,
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
        node_runtime=node_identity,
        actions_runtime=actions_identity,
        stage_run=STAGE_RUN,
    )

    assert receipt["schema_version"] == 2
    assert receipt["node_runtime"] == node_identity
    assert receipt["node_runtime"]["node_version"] == MINIMUM_NODE_VERSION
    assert receipt["node_runtime"]["source_sha"] == SOURCE_SHA
    assert receipt["actions_runtime"] == actions_identity
    assert receipt["actions_runtime"]["declared_runtime"] == "node24"
    assert receipt["actions_runtime"]["commit_sha"] == ACTION_COMMIT_SHA
    assert set(validated) == set(receipt["assets"])
    assert "node-runtime.json" in receipt["assets"]
    assert "actions-runtime.json" in receipt["assets"]
    assert receipt["source_sha"] == SOURCE_SHA
    assert receipt["assets"]["josh-room-0.1.33.vsix"]["sha256"] == hashlib.sha256(b"synthetic vsix").hexdigest()


def test_staged_receipt_rejects_changed_source_checks_runtime_or_asset_bytes(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    identity = runtime_identity(RUNTIME, LOCK)
    node_identity = node_runtime_identity(staged, source_sha=SOURCE_SHA)
    actions_identity = actions_runtime_identity(staged)
    receipt = build_receipt(staged, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity, node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN)

    with pytest.raises(ValueError, match="source SHA"):
        validate_staged_files(staged, receipt, source_sha="d" * 40, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity, node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN)
    with pytest.raises(ValueError, match="check receipts"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks={"source_sha": "a" * 40, "checks": {"test": {"id": 102}}}, runtime=identity, node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN)
    changed = json.loads(json.dumps(identity))
    changed["jat"]["platforms"]["linux-x64"]["archive_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="runtime receipts"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=changed, node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN)
    changed_node = json.loads(json.dumps(node_identity))
    changed_node["node_version"] = "22.23.5"
    with pytest.raises(ValueError, match="Node runtime evidence"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity, node_runtime=changed_node, actions_runtime=actions_identity, stage_run=STAGE_RUN)
    changed_action = json.loads(json.dumps(actions_identity))
    changed_action["declared_runtime"] = "node25"
    with pytest.raises(ValueError, match="action runtime evidence"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity, node_runtime=node_identity, actions_runtime=changed_action, stage_run=STAGE_RUN)
    (staged / "josh-room-0.1.33.vsix").write_bytes(b"changed")
    with pytest.raises(ValueError, match="asset differs"):
        validate_staged_files(staged, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix", version="0.1.33", checks=CHECKS, runtime=identity, node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN)


def test_node_runtime_evidence_is_required_patched_and_source_bound(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    node_identity = node_runtime_identity(staged, source_sha=SOURCE_SHA)
    assert node_identity["minimum"] == MINIMUM_NODE_VERSION
    assert node_identity["actions"]["github_actions"] is True

    (staged / "node-runtime.json").unlink()
    with pytest.raises(FileNotFoundError, match="patched-Node runtime evidence"):
        node_runtime_identity(staged, source_sha=SOURCE_SHA)

    below = {**NODE_RUNTIME, "node_version": "22.23.3"}
    (staged / "node-runtime.json").write_text(json.dumps(below))
    with pytest.raises(ValueError, match="unpatched Node version"):
        node_runtime_identity(staged, source_sha=SOURCE_SHA)

    wrong_minimum = {**NODE_RUNTIME, "minimum": "22.0.0"}
    (staged / "node-runtime.json").write_text(json.dumps(wrong_minimum))
    with pytest.raises(ValueError, match="minimum must be 22.23.4"):
        node_runtime_identity(staged, source_sha=SOURCE_SHA)

    mismatched = {**NODE_RUNTIME, "source_sha": "e" * 40}
    (staged / "node-runtime.json").write_text(json.dumps(mismatched))
    with pytest.raises(ValueError, match="source SHA differs"):
        node_runtime_identity(staged, source_sha=SOURCE_SHA)

    unsatisfied = {**NODE_RUNTIME, "satisfied": False}
    (staged / "node-runtime.json").write_text(json.dumps(unsatisfied))
    with pytest.raises(ValueError, match="satisfied minimum"):
        node_runtime_identity(staged, source_sha=SOURCE_SHA)

    (staged / "node-runtime.json").write_text(json.dumps(NODE_RUNTIME))
    assert node_runtime_identity(staged, source_sha=SOURCE_SHA) == node_identity


def test_actions_runtime_evidence_is_required_and_commit_bound(tmp_path):
    staged = tmp_path / "staged"
    _files(staged)
    identity = actions_runtime_identity(staged)
    assert identity["action"] == "actions/setup-node"
    assert identity["commit_sha"] == ACTION_COMMIT_SHA
    assert identity["declared_runtime"] == "node24"

    path = staged / ACTIONS_RUNTIME_FILE
    path.unlink()
    with pytest.raises(FileNotFoundError, match="JavaScript action runtime evidence"):
        actions_runtime_identity(staged)

    bad_commit = {**ACTIONS_RUNTIME, "commit_sha": "not-a-sha"}
    path.write_text(json.dumps(bad_commit))
    with pytest.raises(ValueError, match="action commit SHA"):
        actions_runtime_identity(staged)

    bad_runtime = {**ACTIONS_RUNTIME, "declared_runtime": "node"}
    path.write_text(json.dumps(bad_runtime))
    with pytest.raises(ValueError, match="node<N> declared runtime"):
        actions_runtime_identity(staged)

    unbound_source = {**ACTIONS_RUNTIME, "runtime_source": "https://raw.githubusercontent.com/actions/setup-node/main/action.yml"}
    path.write_text(json.dumps(unbound_source))
    with pytest.raises(ValueError, match="not bound to its exact commit SHA"):
        actions_runtime_identity(staged)

    wrong_action = {**ACTIONS_RUNTIME, "action": "actions/checkout"}
    path.write_text(json.dumps(wrong_action))
    with pytest.raises(ValueError, match="must name actions/setup-node"):
        actions_runtime_identity(staged)

    path.write_text(json.dumps(ACTIONS_RUNTIME))
    assert actions_runtime_identity(staged) == identity


def test_stage_run_provenance_is_exact_and_rejects_other_workflow_runs():
    assert validate_stage_run(STAGE_RUN_API, expected_run_id=202, expected_workflow_id=88, source_sha=SOURCE_SHA) == STAGE_RUN

    spoofed = {**STAGE_RUN_API, "workflow_id": 999}
    with pytest.raises(ValueError, match="trusted release workflow"):
        validate_stage_run(spoofed, expected_run_id=202, expected_workflow_id=88, source_sha=SOURCE_SHA)

    stale = {**STAGE_RUN_API, "conclusion": "failure"}
    with pytest.raises(ValueError, match="trusted release workflow"):
        validate_stage_run(stale, expected_run_id=202, expected_workflow_id=88, source_sha=SOURCE_SHA)


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


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("sha256sum"), reason="Bash and sha256sum are required for the staging shell contract")
def test_workflow_stage_receipt_survives_default_artifact_upload(tmp_path):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["stage-candidate"]["steps"]
    stage = next(step for step in steps if step.get("name") == "Create exact release stage files and provenance receipt")
    upload = next(step for step in steps if step.get("with", {}).get("name") == "release-stage-files")
    assert upload["with"].get("include-hidden-files", False) is False

    candidate = tmp_path / "ci-release-candidate"
    candidate.mkdir()
    (candidate / "josh-room-0.1.33.vsix").write_bytes(b"synthetic vsix")
    (candidate / "candidate.json").write_text("{}")
    (candidate / "node-runtime.json").write_text(json.dumps(NODE_RUNTIME))
    (candidate / ACTIONS_RUNTIME_FILE).write_text(json.dumps(ACTIONS_RUNTIME))
    (tmp_path / "release-readiness.json").write_text(json.dumps(CHECKS))
    manifest = tmp_path / "vscode-extension/runtime/manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(RUNTIME))
    (tmp_path / "release-lock.json").write_text(json.dumps(LOCK))
    commands = tmp_path / "commands"
    commands.mkdir()
    uv = commands / "uv"
    # Model uv's output boundary, including its default hidden metadata file.
    uv.write_text(f"#!{sys.executable}\n" + '''import pathlib, sys
assert sys.argv[1] == "build"
dist = pathlib.Path("dist")
if "--no-create-gitignore" not in sys.argv[2:]:
    (dist / ".gitignore").write_text("*")
(dist / "synthetic.whl").write_bytes(b"synthetic wheel")
(dist / "synthetic.tar.gz").write_bytes(b"synthetic sdist")
''')
    uv.chmod(0o755)
    env = os.environ | {
        "PATH": str(commands) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(root), "RUNNER_TEMP": str(tmp_path),
        "SOURCE_SHA": SOURCE_SHA, "VERSION": "0.1.33", "CI_RUN_ID": "101",
        "GITHUB_RUN_ID": str(STAGE_RUN["id"]), "STAGE_WORKFLOW_ID": str(STAGE_RUN["workflow_id"]),
    }
    result = subprocess.run(["bash", "-c", stage["run"]], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    for path in (tmp_path / "dist").iterdir():
        if not path.name.startswith("."):
            shutil.copyfile(path, downloaded / path.name)
    receipt = json.loads((downloaded / "release-candidate.json").read_text())
    node_identity = node_runtime_identity(downloaded, source_sha=SOURCE_SHA)
    actions_identity = actions_runtime_identity(downloaded)
    validated = validate_staged_files(
        downloaded, receipt, source_sha=SOURCE_SHA, release_tag="v0.1.33-standalone-vsix",
        version="0.1.33", checks=CHECKS, runtime=runtime_identity(RUNTIME, LOCK),
        node_runtime=node_identity, actions_runtime=actions_identity, stage_run=STAGE_RUN,
    )
    assert receipt["node_runtime"] == node_identity
    assert receipt["actions_runtime"] == actions_identity
    assert "node-runtime.json" in receipt["assets"]
    assert "actions-runtime.json" in receipt["assets"]
    assert set(validated) == set(receipt["assets"])
