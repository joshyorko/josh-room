from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def test_release_is_staged_first_and_only_owner_can_publish_after_explicit_approval():
    workflow = _workflow("release.yml")
    dispatch = workflow["on"]["workflow_dispatch"]
    inputs = dispatch["inputs"]
    assert inputs["operation"]["options"] == ["stage", "publish"]
    assert inputs["source_sha"]["required"] == "true"
    assert inputs["release_tag"]["required"] == "true"
    assert inputs["owner_approval"]["default"] == "REJECT"
    assert "push" not in workflow["on"]

    jobs = workflow["jobs"]
    assert "release-readiness" in jobs
    assert "stage" in jobs and "publish" in jobs
    assert jobs["stage"]["needs"] == ["release-readiness"]
    assert jobs["publish"]["needs"] == ["release-readiness"]
    assert "github.actor == 'joshyorko'" in jobs["stage"]["if"]
    publish = jobs["publish"]
    condition = publish["if"]
    assert "github.actor == 'joshyorko'" in condition
    assert "github.triggering_actor == 'joshyorko'" in condition
    assert "inputs.operation == 'publish'" in condition
    assert "inputs.owner_approval == 'APPROVE'" in condition
    assert "refs/heads/main" in condition
    assert publish["permissions"]["contents"] == "write"
    assert publish["permissions"]["packages"] == "write"
    assert any("release_staging.py verify" in str(step.get("run", "")) for step in jobs["release-readiness"]["steps"])
    assert any("release_staging.py reconcile" in str(step.get("run", "")) for step in jobs["publish"]["steps"])
    assert "draft=false" in str(jobs["publish"]).lower()
    readiness = jobs["release-readiness"]
    assert any("--report \"$RUNNER_TEMP/release-readiness.json\"" in str(step.get("run", "")) for step in readiness["steps"])
    stage_build = next(step for step in readiness["steps"] if step.get("name") == "Build and receipt the exact release assets for staging")
    assert stage_build["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert '--checks "$RUNNER_TEMP/release-readiness.json"' in stage_build["run"]
    assert "verify_release_candidate.py" in stage_build["run"]
    assert "verify_release_readiness.py" not in stage_build["run"]
    assert "release-checks.json" not in str(jobs)
    assert 'refs/tags/$RELEASE_TAG' in str(jobs["stage"])
    assert "test \"$tag_sha\" = \"$RELEASE_SHA\"" in str(jobs["stage"])
    readiness_run = str([step.get("run", "") for step in readiness["steps"]])
    assert "--controller-tag-sha" in readiness_run and "--jat-tag-sha" in readiness_run


def test_template_workflow_validates_only_and_has_no_publication_permission():
    workflow = _workflow("publish-template.yml")

    assert workflow["permissions"].get("packages") != "write"
    assert "workflow_dispatch" not in workflow["on"]
    assert "packages" not in workflow["permissions"]
    assert "Publish template" not in str(workflow["jobs"])
    assert "templates publish" not in str(workflow["jobs"])


def test_rcc_maintenance_pairs_jat_candidate_and_only_opens_a_draft_consumer_pr():
    workflow = _workflow("rcc-candidate-qualification.yml")
    jobs = workflow["jobs"]
    assert "jat-runtime-candidate" in jobs
    assert "jat_runtime_candidate.py" in str(jobs["jat-runtime-candidate"])
    promote = jobs["promote"]
    assert "needs.jat-runtime-candidate.outputs.qualified == 'true'" in promote["if"]
    assert "--jat-candidate" in str(promote)
    assert "--draft" in str(promote)
    assert "maintenance/rcc-runtimes" in str(promote)


def test_manual_controller_builder_uses_manifest_pins_without_old_version_label():
    workflow = (WORKFLOWS / "controller-artifact.yml").read_text()
    assert "v18.19.5" not in workflow
    assert "inputs.rcc_sha256" not in workflow
    assert "jq -er '.rcc.version'" in workflow
    assert "$manifest.rcc.version" in workflow
