from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/request_release.py"


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for name in (
        "vscode-extension/package.json",
        "templates/room/vscode-extension/package.json",
        "vscode-extension/runtime/manifest.json",
        "templates/room/vscode-extension/runtime/manifest.json",
        "release-lock.json",
        "templates/room/vscode-extension/runtime/release-lock.json",
        "templates/room/devcontainer-template.json",
        ".devcontainer/bootstrap.sh",
        "templates/room/.devcontainer/bootstrap.sh",
        "release-promotion.json",
        "pyproject.toml",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
        if name.endswith(".json") and name != "release-promotion.json":
            value = json.loads(target.read_text())
            if name.endswith("package.json"):
                value["version"] = "0.1.43"
            elif name.endswith("/manifest.json"):
                value["extension_version"] = "0.1.43"
            elif name.endswith("release-lock.json"):
                value["candidate_version"] = "0.1.43"
                value["template"]["version"] = "0.1.13"
            elif name.endswith("devcontainer-template.json"):
                value["version"] = "0.1.13"
            target.write_text(json.dumps(value, indent=2) + "\n")
        elif name.endswith("bootstrap.sh"):
            target.write_text(
                re.sub(
                    r"joshyorko\.josh-room-\d+\.\d+\.\d+",
                    "joshyorko.josh-room-0.1.43",
                    target.read_text(),
                )
            )
    (root / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.1.43]\n\n- Previous release note\n"
    )
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "config", "user.name", "Fixture")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial fixture")
    git(root, "tag", "v0.1.43-standalone-vsix")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "example.txt").write_text("synthetic\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Handle ordinary nonconventional change")
    releases = tmp_path / "releases.json"
    releases.write_text(
        json.dumps(
            [
                {
                    "tag_name": "v0.1.43-standalone-vsix",
                    "draft": False,
                    "prerelease": False,
                },
                {
                    "tag_name": "rcc-v99.0.0-controller-example",
                    "draft": False,
                    "prerelease": True,
                },
            ]
        )
    )
    return root, releases


def run(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--root", str(root)],
        check=False,
        text=True,
        capture_output=True,
    )


def prepare(repository, *args):
    root, releases = repository
    return run(root, "prepare", "--releases", str(releases), *args)


def test_actual_cli_prepares_all_versions_and_nonconventional_notes_without_changing_pins(
    repository,
):
    root, _ = repository
    before = json.loads((root / "release-lock.json").read_text())
    result = prepare(repository, "--version", "minor")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["version"] == "0.2.0"
    assert report["source_sha"] == git(root, "rev-parse", "HEAD")
    assert report["baseline"] == "v0.1.43-standalone-vsix"
    assert (
        "Handle ordinary nonconventional change" in (root / "CHANGELOG.md").read_text()
    )
    changelog = (root / "CHANGELOG.md").read_text()
    assert changelog.index("## [0.2.0]") < changelog.index("## [0.1.43]")
    assert "Previous release note" in changelog
    assert report["branch"] == "release/v0.2.0"
    assert "owner" in report["body"].lower()
    for name in (
        "vscode-extension/package.json",
        "templates/room/vscode-extension/package.json",
    ):
        assert json.loads((root / name).read_text())["version"] == "0.2.0"
    for name in (
        "vscode-extension/runtime/manifest.json",
        "templates/room/vscode-extension/runtime/manifest.json",
    ):
        assert json.loads((root / name).read_text())["extension_version"] == "0.2.0"
    after = json.loads((root / "release-lock.json").read_text())
    assert after["candidate_version"] == "0.2.0"
    assert after["template"]["version"] == "0.1.14"
    for key in ("controller", "jat", "rcc", "josh_room"):
        assert after[key] == before[key]
    assert (root / "release-promotion.json").read_bytes() == (
        ROOT / "release-promotion.json"
    ).read_bytes()
    assert (root / "pyproject.toml").read_bytes() == (
        ROOT / "pyproject.toml"
    ).read_bytes()
    assert (
        "joshyorko.josh-room-0.2.0" in (root / ".devcontainer/bootstrap.sh").read_text()
    )


@pytest.mark.parametrize(
    "version",
    ["0.1.43", "0.1.1", "v0.1.44", "01.2.3", "0.1.44;echo injected", "0.1.44-beta"],
)
def test_invalid_versions_leave_checkout_unchanged(repository, version):
    root, _ = repository
    result = prepare(repository, "--version", version)
    assert result.returncode != 0
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["draft", "tag", "branch", "published"])
def test_existing_version_or_branch_is_never_overwritten(repository, kind):
    root, releases = repository
    if kind in {"draft", "published"}:
        items = json.loads(releases.read_text())
        items.append(
            {
                "tag_name": "v0.1.44-standalone-vsix",
                "draft": kind == "draft",
                "prerelease": False,
            }
        )
        releases.write_text(json.dumps(items))
    elif kind == "tag":
        git(root, "tag", "v0.1.44-standalone-vsix")
    else:
        git(root, "update-ref", "refs/remotes/origin/release/v0.1.44", "HEAD")
    result = prepare(repository)
    assert result.returncode != 0
    assert git(root, "status", "--porcelain") == ""


def test_stale_source_is_rejected(repository):
    root, _ = repository
    git(root, "checkout", "--orphan", "diverged")
    git(root, "commit", "-m", "Other history")
    result = prepare(repository)
    assert result.returncode != 0
    assert "main" in result.stderr


def test_baseline_excludes_drafts_and_nonancestor_releases(repository):
    root, releases = repository
    git(root, "tag", "v0.1.44-standalone-vsix")
    git(root, "checkout", "--orphan", "other")
    git(root, "commit", "-m", "Other release")
    git(root, "tag", "v0.1.45-standalone-vsix")
    git(root, "checkout", "main")
    items = json.loads(releases.read_text())
    items.extend(
        [
            {"tag_name": "v0.1.44-standalone-vsix", "draft": True, "prerelease": False},
            {
                "tag_name": "v0.1.45-standalone-vsix",
                "draft": False,
                "prerelease": False,
            },
        ]
    )
    releases.write_text(json.dumps(items))
    result = prepare(repository, "--version", "0.1.46")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["baseline"] == "v0.1.43-standalone-vsix"


def test_no_published_ancestor_fails_without_mutation(repository):
    root, releases = repository
    releases.write_text("[]")
    result = prepare(repository)
    assert result.returncode != 0
    assert "ancestor" in result.stderr
    assert git(root, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "text",
    [
        "",
        "## [0.1.43]\n",
        "## [0.1.43] (2026-11-06)\n",
        "## [0.1.43] (2026-11-06)\n- note\n## [0.1.43] (2026-11-06)\n- duplicate\n",
    ],
)
def test_release_changelog_requires_unique_nonempty_section(repository, text):
    root, _ = repository
    (root / "CHANGELOG.md").write_text(text)
    result = run(root, "validate", "--branch", "release/v0.1.43")
    assert result.returncode != 0


def test_maintenance_without_current_notes_is_allowed_but_existing_empty_section_is_not(
    repository,
):
    root, _ = repository
    assert run(root, "validate", "--branch", "maintenance/rcc-runtimes").returncode == 0
    (root / "CHANGELOG.md").write_text("## [0.1.43]\n")
    assert run(root, "validate", "--branch", "main").returncode != 0


def test_request_uses_trusted_main_owner_guard_and_exact_ci_dispatch():
    workflow = yaml.load(
        (ROOT / ".github/workflows/request-release.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    job = workflow["jobs"]["request"]
    for expected in (
        "github.actor == 'joshyorko'",
        "github.triggering_actor == 'joshyorko'",
        "refs/heads/main",
    ):
        assert expected in job["if"]
    checkouts = [
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkouts[0]["with"]["ref"] == "${{ github.sha }}"
    assert all(step["with"]["persist-credentials"] == "false" for step in checkouts)
    steps = "\n".join(step.get("run", "") for step in job["steps"])
    assert "--draft" in steps
    assert 'source_sha="$release_sha"' in steps
    assert "git merge-base --is-ancestor" in steps
    assert "--force" not in steps
    assert "gh release" not in steps


@pytest.mark.parametrize("heading", ["## [0.1.43]", "## [0.1.43] (2026-11-06)"])
def test_valid_notes_without_blank_line_are_accepted(repository, heading):
    root, _ = repository
    (root / "CHANGELOG.md").write_text(f"{heading}\n- note\n")
    result = run(root, "validate", "--branch", "release/v0.1.43")
    assert result.returncode == 0, result.stderr


def test_prepare_rejects_existing_requested_section_without_mutation(repository):
    root, _ = repository
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.1.44]\n- existing\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Existing planned notes")
    result = prepare(repository)
    assert result.returncode != 0
    assert "already contains" in result.stderr
    assert git(root, "status", "--porcelain") == ""


def test_fixture_stays_stable_after_product_version_changes(
    repository, tmp_path, monkeypatch
):
    root, _ = repository
    assert prepare(repository).returncode == 0
    monkeypatch.setattr(sys.modules[__name__], "ROOT", root)
    next_run = tmp_path / "next-run"
    next_run.mkdir()
    regenerated = globals()["repository"].__wrapped__(next_run)
    result = prepare(regenerated)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["version"] == "0.1.44"


@pytest.mark.parametrize("fail_pr", [False, True])
def test_workflow_pushes_generated_branch_and_dispatches_its_exact_sha(
    repository, fail_pr
):
    import os

    root, releases = repository
    workspace = root.parent
    candidate = workspace / "candidate"
    root.rename(candidate)
    root = candidate
    remote = workspace / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "main", "--tags")
    source_sha = git(root, "rev-parse", "HEAD")
    (workspace / "automation").symlink_to(ROOT, target_is_directory=True)
    runtime = workspace / "runtime"
    runtime.mkdir()
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with Path(os.environ['GH_CALLS']).open('a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'api': print(json.dumps([json.loads(Path(os.environ['RELEASES']).read_text())]))\n"
        "if sys.argv[1:3] == ['pr', 'create'] and os.environ['FAIL_PR'] == '1': sys.exit(1)\n"
    )
    fake_gh.chmod(0o755)
    output = runtime / "output"
    output.touch()
    calls = runtime / "calls"
    # Keep even gh's setup helper confined to this test's temporary directory.
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GIT_CONFIG_GLOBAL": str(runtime / "gitconfig"),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_REPOSITORY": "joshyorko/josh-room",
        "RUNNER_TEMP": str(runtime),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(runtime / "summary"),
        "SOURCE_SHA": source_sha,
        "MAIN_SHA": source_sha,
        "SOURCE_BRANCH": "main",
        "BUMP": "patch",
        "RELEASES": str(releases),
        "GH_CALLS": str(calls),
        "FAIL_PR": "1" if fail_pr else "0",
    }
    workflow = yaml.load(
        (ROOT / ".github/workflows/request-release.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["request"]["steps"]
    prepare_step = next(step for step in steps if step.get("id") == "prepared")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", prepare_step["run"]],
        check=False,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    environment.update(RELEASE_BRANCH="release/v0.1.44", RELEASE_VERSION="0.1.44")
    commit_step = next(step for step in steps if step.get("id") == "publish")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", commit_step["run"]],
        check=False,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if fail_pr else 0), result.stderr
    release_sha = git(remote, "rev-parse", "refs/heads/release/v0.1.44")
    assert release_sha != source_sha
    assert git(remote, "rev-parse", "refs/heads/main") == source_sha
    assert f"release_sha={release_sha}" in output.read_text()
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert any(call[:2] == ["pr", "create"] and "--draft" in call for call in recorded)
    dispatches = [call for call in recorded if call[:2] == ["workflow", "run"]]
    if fail_pr:
        assert dispatches == []
    else:
        assert len(dispatches) == 1
        assert f"source_sha={release_sha}" in dispatches[0]
    assert not any(call[0] == "release" for call in recorded)
