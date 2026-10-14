from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def _steps(job):
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def test_ci_workflow_can_be_dispatched_for_automation_created_prs_at_an_exact_sha():
    workflow = _workflow("ci.yml")

    assert workflow["on"]["workflow_dispatch"]["inputs"]["source_sha"]["required"] == "true"
    checkout = workflow["jobs"]["test"]["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert "EXPECTED_SHA" in _steps(workflow["jobs"]["test"])
    assert "test \"$GITHUB_SHA\" = \"$EXPECTED_SHA\"" in _steps(workflow["jobs"]["test"])
    promotion = _workflow("rcc-candidate-qualification.yml")["jobs"]["promote"]
    assert promotion["permissions"]["actions"] == "write"
    assert "gh workflow run ci.yml" in _steps(promotion)
    assert '--field source_sha="$promotion_sha"' in _steps(promotion)
    promotion_steps = _steps(promotion)
    assert '.jat_platforms["linux-x64"].digest' in promotion_steps
    assert '.jat_platforms["win32-x64"].digest' in promotion_steps
    jat_verification = _workflow("rcc-candidate-qualification.yml")["jobs"]["jat-runtime-candidate"]
    fetch_step = next(step for step in jat_verification["steps"] if step.get("id") == "verify")
    assert fetch_step["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_main_ci_builds_a_versioned_candidate_only_after_every_required_check_passes():
    workflow = _workflow("ci.yml")
    candidate = workflow["jobs"]["release-candidate"]

    assert candidate["needs"] == ["test", "windows-private-runtime", "managed-runtime-consumer"]
    steps = _steps(candidate)
    assert "npm run package" in steps
    assert "--version \"$VERSION\"" in steps
    assert "--source-sha \"$SOURCE_SHA\"" in steps
    assert "--ci-run-id \"$CI_RUN_ID\"" in steps
    assert "release-candidate-${{ steps.version.outputs.version }}-${{ github.sha }}" in str(candidate)
    upload = next(step for step in candidate["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["retention-days"] == "90"


def test_release_stages_from_trusted_green_ci_without_preexisting_release_tag_or_vsix():
    workflow = _workflow("release.yml")
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]
    trigger = workflow["on"]["workflow_run"]
    stage = workflow["jobs"]["stage-candidate"]

    assert trigger["workflows"] == ["CI"]
    assert trigger["types"] == ["completed"]
    assert trigger["branches"] == ["main"]
    assert set(dispatch) == {"source_sha", "stage_run_id", "owner_approval"}
    assert dispatch["owner_approval"]["default"] == "REJECT"
    assert "gh release create" not in _steps(stage)
    assert "gh release upload" not in _steps(stage)
    assert "check-runs?per_page=100" in _steps(stage)
    assert "--slurp" not in _steps(stage)
    assert "release-candidate-${{ steps.source.outputs.version }}-${{ github.event.workflow_run.head_sha }}" in str(stage)
    assert "python -m scripts.release_staging create" in _steps(stage)
    assert "python scripts/release_staging.py" not in _steps(stage)
    download = next(step for step in stage["steps"] if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
    assert "verify_release_readiness.py" in _steps(stage)
    assert "verify_release_candidate.py" in _steps(stage)
    upload = next(step for step in stage["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "release-stage-files"
    assert upload["with"]["retention-days"] == "90"
    assert stage["permissions"]["contents"] == "read"


def test_only_explicit_owner_dispatch_can_create_tag_publish_release_and_template():
    workflow = _workflow("release.yml")
    publish = workflow["jobs"]["publish"]
    condition = publish["if"]
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.actor == 'joshyorko'" in condition
    assert "github.triggering_actor == 'joshyorko'" in condition
    assert "inputs.owner_approval == 'APPROVE'" in condition
    assert "refs/heads/main" in condition
    assert publish["permissions"]["contents"] == "write"
    assert publish["permissions"]["packages"] == "write"
    steps = _steps(publish)
    assert "actions/download-artifact@v8" in str(publish)
    assert "python -m scripts.release_staging verify" in steps
    assert "python -m scripts.release_staging reconcile" in steps
    assert "python scripts/release_staging.py" not in steps
    assert "verify_release_candidate.py" in steps
    assert "gh release create" in steps
    assert "gh release upload" in steps
    assert "--target \"$SOURCE_SHA\" --draft" in steps
    assert "devcontainer templates publish" in steps
    step_names = [step.get("name") for step in publish["steps"]]
    preflight_index = step_names.index("Install pinned Dev Container CLI and preflight template publication")
    create_index = step_names.index("Create or safely resume the owner-approved draft release")
    publish_index = step_names.index("Publish the verified release and approved template")
    assert preflight_index < create_index < publish_index
    preflight = _steps({"steps": [publish["steps"][preflight_index]]})
    assert "npm install --global @devcontainers/cli@0.89.0" in preflight
    assert 'test "$(devcontainer --version)" = "0.89.0"' in preflight
    assert "devcontainer templates publish --help" in preflight
    assert "grep -Fq -- '--registry'" in preflight
    assert "grep -Fq -- '--namespace'" in preflight
    assert "jq -e '.id == \"room\"" in preflight
    assert "jq empty templates/room/.devcontainer/devcontainer.json" in preflight
    assert "bash -n templates/room/.devcontainer/bootstrap.sh" in preflight
    publish_run = publish["steps"][publish_index]["run"]
    assert "gh release edit \"$RELEASE_TAG\" --repo \"$GITHUB_REPOSITORY\" --draft=false" in publish_run
    assert "gh release edit \"$RELEASE_TAG\" --repo \"$GITHUB_REPOSITORY\" --draft=false" in steps
    assert '--namespace "$GITHUB_REPOSITORY/templates"' in publish_run
    assert "--namespace ghcr.io/" not in publish_run


def test_release_staging_module_entrypoint_imports_with_clean_python_environment():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-m", "scripts.release_staging", "--help"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "{create,verify,reconcile}" in result.stdout


def test_readiness_binds_workflow_id_path_jobs_and_actions_app():
    source = (ROOT / "scripts/verify_release_readiness.py").read_text()
    assert 'CI_WORKFLOW_PATH = ".github/workflows/ci.yml"' in source
    assert "GITHUB_ACTIONS_APP_ID = 15368" in source
    assert "workflow_run.get(\"workflow_id\") != workflow_id" in source
    assert "check[\"app\"].get(\"id\") != GITHUB_ACTIONS_APP_ID" in source
    assert "job.get(\"run_id\") != run_id" in source
    assert "job.get(\"head_sha\") != source_sha" in source


def test_template_workflow_validates_only_and_has_no_publication_permission():
    workflow = _workflow("publish-template.yml")

    assert workflow["permissions"].get("packages") != "write"
    assert "workflow_dispatch" not in workflow["on"]
    assert "packages" not in workflow["permissions"]
    assert "Publish template" not in str(workflow["jobs"])
    assert "templates publish" not in str(workflow["jobs"])


def test_rcc_maintenance_pairs_jat_candidate_and_opens_only_a_draft_consumer_pr():
    workflow = _workflow("rcc-candidate-qualification.yml")
    jobs = workflow["jobs"]
    assert "jat-runtime-candidate" in jobs
    assert "jat_runtime_candidate.py" in str(jobs["jat-runtime-candidate"])
    promote = jobs["promote"]
    assert "needs.jat-runtime-candidate.outputs.qualified == 'true'" in promote["if"]
    assert "--jat-candidate" in str(promote)
    assert "--draft" in str(promote)
    assert "maintenance/rcc-runtimes" in str(promote)
    assert "gh workflow run ci.yml" in _steps(promote)
    assert "JAT source handoff" in _steps(promote)


def test_manual_controller_builder_uses_manifest_pins_without_old_version_label():
    workflow = (WORKFLOWS / "controller-artifact.yml").read_text()
    assert "v18.19.5" not in workflow
    assert "inputs.rcc_sha256" not in workflow
    assert "jq -er '.rcc.version'" in workflow
    assert "$manifest.rcc.version" in workflow
