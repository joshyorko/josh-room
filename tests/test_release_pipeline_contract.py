from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.release_staging import MINIMUM_NODE_VERSION

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github/workflows"
MINIMUM_NODE = "22.23.4"


def _workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def _steps(job):
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def test_every_node_workflow_pins_and_verifies_the_patched_node_22_runtime():
    assert MINIMUM_NODE_VERSION == MINIMUM_NODE
    js = (ROOT / "scripts/verify_node_version.js").read_text()
    assert re.search(r"const DEFAULT_MINIMUM = '22\.23\.4';", js)
    assert "module.exports" in js
    assert "require.main === module" in js

    node_workflows = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        source = path.read_text()
        if "actions/setup-node@" not in source:
            continue
        node_workflows.add(path.name)
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        assert workflow["env"]["NODE_VERSION"] == MINIMUM_NODE, path.name
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            setup_indexes = [index for index, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/setup-node@")]
            for index in setup_indexes:
                setup = steps[index]
                assert setup["with"]["node-version"] == "${{ env.NODE_VERSION }}", (path.name, job_name)
                later = steps[index + 1:]
                verifiers = [
                    str(step.get("run", ""))
                    for step in later
                    if "scripts/verify_node_version.js" in str(step.get("run", ""))
                ]
                assert verifiers, (path.name, job_name)
                assert "--source-sha" in verifiers[0]
                assert "--report" in verifiers[0]
                assert "--minimum" in verifiers[0]
                assert "node-version: \"22\"" not in source
                assert "node-version: '22'" not in source
    assert node_workflows == {"ci.yml", "capture-candidate.yml", "capture-windows-ui.yml", "issue-165-windows-ui-capture.yml", "release.yml"}


def test_ci_retains_exact_setup_node_action_runtime_evidence():
    workflow = _workflow("ci.yml")
    steps = workflow["jobs"]["release-candidate"]["steps"]
    action_step = next(step for step in steps if step.get("name") == "Record the setup-node action runtime")
    run = action_step["run"]

    assert action_step["shell"] == "bash"
    assert action_step["env"]["ACTION_REF"] == "v7"
    assert "refs/tags/${ACTION_REF}^{}" in run
    assert "refs/tags/${ACTION_REF}" in run
    assert "^[0-9a-f]{40}$" in run
    assert 'https://raw.githubusercontent.com/actions/setup-node/$action_sha/action.yml' in run
    assert "curl --fail --location --proto '=https' --tlsv1.2" in run
    assert "/^runs:[[:space:]]*$/" in run
    assert "/^[[:space:]]+using:/" in run
    assert "^node[0-9]+$" in run
    assert '"declared_runtime"' in run or "--arg declared_runtime" in run
    assert "actions-runtime.json" in run

    release = yaml.load((WORKFLOWS / "release.yml").read_text(), Loader=yaml.BaseLoader)
    stage = next(
        step for step in release["jobs"]["stage-candidate"]["steps"]
        if step.get("name") == "Create exact release stage files and provenance receipt"
    )
    assert 'cp "$RUNNER_TEMP/ci-release-candidate/actions-runtime.json" dist/actions-runtime.json' in stage["run"]
    assert "ci-release-candidate/actions-runtime.json" in stage["run"]

    staging = (ROOT / "scripts/release_staging.py").read_text()
    assert 'ACTIONS_RUNTIME_FILE = "actions-runtime.json"' in staging
    assert "def actions_runtime_identity(directory: Path)" in staging
    assert '"action": "actions/setup-node"' in staging
    assert 'ACTIONS_RUNTIME_PATTERN = re.compile(r"^node[0-9]+$")' in staging
    assert "runtime_source" in staging
    assert "actions_runtime" in staging


def test_ci_runs_the_patched_node_version_verifier_tests():
    workflow = _workflow("ci.yml")

    assert (
        "node --test vscode-extension/*.test.js scripts/verify_node_version.test.js"
        in _steps(workflow["jobs"]["test"])
    )
    release_candidate = workflow["jobs"]["release-candidate"]
    upload = next(step for step in release_candidate["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["with"]["path"] == "dist/release-candidate/"
    assert "dist/release-candidate/node-runtime.json" in _steps(release_candidate)


def test_release_staging_fails_without_a_patched_node_runtime_receipt():
    source = (ROOT / ".github/workflows/release.yml").read_text()
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    stage = next(
        step for step in workflow["jobs"]["stage-candidate"]["steps"]
        if step.get("name") == "Create exact release stage files and provenance receipt"
    )

    assert 'cp "$RUNNER_TEMP/ci-release-candidate/node-runtime.json" dist/node-runtime.json' in stage["run"]
    assert "ci-release-candidate/node-runtime.json" in stage["run"]
    staging = (ROOT / "scripts/release_staging.py").read_text()
    assert 'MINIMUM_NODE_VERSION = "22.23.4"' in staging
    assert 'receipt.get("schema_version") != 2' in staging
    assert "node_runtime_identity" in staging


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
    assert '--candidate "$candidate"' in steps
    assert "--source-sha \"$SOURCE_SHA\"" in steps
    assert "--ci-run-id \"$CI_RUN_ID\"" in steps
    assert 'cmp "$candidate" "dist/release-candidate/josh-room-$VERSION.vsix"' in steps
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
    assert set(dispatch) == {"source_sha", "stage_run_id", "owner_approval", "resume_empty_draft_id", "expected_draft_source_sha"}
    assert dispatch["resume_empty_draft_id"]["required"] == "false"
    assert dispatch["expected_draft_source_sha"]["required"] == "false"
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
    assert 'gh api --method POST "repos/$GITHUB_REPOSITORY/releases"' in steps
    assert 'uploads.github.com/repos/$GITHUB_REPOSITORY/releases/$release_id/assets?name=$encoded_name' in steps
    assert '-f target_commitish="$SOURCE_SHA"' in steps
    assert "-F draft=true -F prerelease=false" in steps
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
    assert 'gh api --method PATCH "repos/$GITHUB_REPOSITORY/releases/$RELEASE_ID" -f tag_name="$RELEASE_TAG" -F draft=false -f make_latest=true' in publish_run
    assert publish["steps"][publish_index]["env"]["RELEASE_ID"] == "${{ steps.draft.outputs.release_id }}"
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
