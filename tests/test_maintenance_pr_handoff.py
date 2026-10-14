"""Execute the real promotion shell against local command-boundary fixtures."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "a" * 40
BRANCH_SHA = "b" * 40


def _handoff(tmp_path, *, existing_pr=False, ahead=True, rebased=False):
    workflow = yaml.safe_load((ROOT / ".github/workflows/rcc-candidate-qualification.yml").read_text())
    step = next(item for item in workflow["jobs"]["promote"]["steps"] if item.get("name") == "Open or update coherent consumer promotion PR")
    script = step["run"].replace("${{ github.sha }}", SOURCE_SHA)
    commands = tmp_path / "commands"
    commands.mkdir()
    recorder = '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALL_LOG"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "git":
    if args[0] == "rev-parse":
        print(os.environ["SOURCE_SHA"] if args[1] == "origin/main" else os.environ["BRANCH_TIP"] if args[1].startswith("origin/") else os.environ["BRANCH_SHA"])
    elif args[0] == "rev-list":
        print(os.environ["AHEAD"])
    elif args[:2] == ["diff", "--cached"]:
        sys.exit(0)
    elif args[:2] == ["diff", "--quiet"]:
        sys.exit(int(os.environ["AHEAD"]))
elif name == "python":
    report = pathlib.Path(args[args.index("--report") + 1])
    report.write_text(json.dumps({"status": "PASS", "no_change": True, "changed_files": [], "candidate": "v18.19.6"}))
elif name == "gh" and args[:2] == ["pr", "list"]:
    print(os.environ["EXISTING_PR"])
'''
    for name in ("git", "python", "gh"):
        command = commands / name
        command.write_text(f"#!{sys.executable}\n" + recorder)
        command.chmod(0o755)
    report = {
        "candidate": "v18.19.6", "artifact_release_tag": "qualified-controller-release",
        "source_sha": SOURCE_SHA, "jat_release_tag": "v0.1.12-jat-runtime",
        "jat_source_sha": "c" * 40, "jat_source_handoff": {"from": "d" * 40, "to": "c" * 40},
        "jat_source_archive": {"sha256": "e" * 64}, "release_version": "0.1.33",
        "platforms": {platform: {"input_digest": "sha256:" + "f" * 64, "artifact_digest": "sha256:" + "1" * 64} for platform in ("linux-x64", "win32-x64")},
        "jat_platforms": {platform: {"digest": "sha256:" + "2" * 64} for platform in ("linux-x64", "win32-x64")},
    }
    report_path = tmp_path / "controller-promotion-report.json"
    report_path.write_text(json.dumps(report))
    env = os.environ | {
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path), "CALL_LOG": str(tmp_path / "calls.jsonl"),
        "SOURCE_SHA": SOURCE_SHA, "BRANCH_SHA": BRANCH_SHA, "AHEAD": str(int(ahead)),
        "BRANCH_TIP": "f" * 40 if rebased else BRANCH_SHA,
        "EXISTING_PR": "123" if existing_pr else "", "GITHUB_REPOSITORY": "example/synthetic-room",
        "GITHUB_REPOSITORY_OWNER": "example", "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "12345", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    return calls, report_path, report


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="Bash and jq are required for the Actions shell contract")
@pytest.mark.parametrize("existing_pr", [False, True])
def test_unchanged_pushed_branch_resumes_pr_and_exact_sha_ci(tmp_path, existing_pr):
    calls, report_path, report = _handoff(tmp_path, existing_pr=existing_pr)
    assert not any(call[:2] in (["git", "commit"], ["git", "push"]) for call in calls)
    action = "edit" if existing_pr else "create"
    assert any(call[:3] == ["gh", "pr", action] for call in calls)
    assert ["gh", "workflow", "run", "ci.yml", "--repo", "example/synthetic-room", "--ref", "maintenance/rcc-runtimes", "--field", f"source_sha={BRANCH_SHA}"] in calls
    assert json.loads(report_path.read_text()) == report
    body = (tmp_path / "controller-promotion-pr.md").read_text()
    assert "Artifact release: qualified-controller-release" in body
    assert "JAT source handoff: " + "d" * 40 + " -> " + "c" * 40 in body


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="Bash and jq are required for the Actions shell contract")
def test_unchanged_branch_without_unmerged_commits_does_not_create_empty_pr(tmp_path):
    calls, _, _ = _handoff(tmp_path, ahead=False)
    assert not any(call[0] == "gh" for call in calls)


@pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="Bash and jq are required for the Actions shell contract")
def test_rebased_unchanged_candidate_is_pushed_with_exact_lease_before_ci(tmp_path):
    calls, _, _ = _handoff(tmp_path, rebased=True)
    push = ["git", "push", "--force-with-lease=refs/heads/maintenance/rcc-runtimes:" + "f" * 40, "--set-upstream", "origin", "maintenance/rcc-runtimes"]
    assert push in calls
    assert not any(call[:2] == ["git", "commit"] for call in calls)
    assert calls.index(push) < next(index for index, call in enumerate(calls) if call[:3] == ["gh", "workflow", "run"])
