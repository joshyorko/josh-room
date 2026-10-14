"""Exercise the release shell across GitHub's draft/published API states."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
TAG = "v0.1.34-standalone-vsix"
pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("jq"), reason="Actions shell requires Bash and jq")


def _fixture(tmp_path, mode):
    staged = tmp_path / "release-stage-files"
    staged.mkdir()
    (staged / "candidate.vsix").write_bytes(b"synthetic VSIX")
    (staged / "release-candidate.json").write_text("{}")
    assets = [{"name": p.name, "size": p.stat().st_size, "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()} for p in staged.iterdir()]
    release = {"id": 91, "tag_name": TAG, "target_commitish": SOURCE, "draft": True, "prerelease": False, "assets": []}
    state = {"release": release, "tag_sha": "", "creates": 0, "uploads": 0, "publishes": 0, "templates": 0, "retargets": 0, "list_reads": 0, "id_reads": 0, "mode": mode}
    if mode == "new":
        state["release"] = None
    elif mode == "partial-draft":
        release["assets"] = assets[:1]
    elif mode == "published":
        release.update(draft=False, assets=assets, target_commitish="main")
        state["tag_sha"] = SOURCE
    elif mode == "stale-draft":
        release["target_commitish"] = "b" * 40
    elif mode == "wrong-tag":
        state["tag_sha"] = "b" * 40
    elif mode == "conflicting-asset":
        release["assets"] = [assets[0] | {"digest": "sha256:" + "0" * 64}]
    elif mode == "published-without-tag":
        release.update(draft=False, assets=assets)
    elif mode == "orphan-tag":
        state.update(release=None, tag_sha=SOURCE)
    if state["release"]:
        state["release"]["body"] = f"Exact CI-qualified Josh Room candidate for source {state['release']['target_commitish']}. Published after explicit owner approval."
    (tmp_path / "state.json").write_text(json.dumps(state))
    template = tmp_path / "templates/room/devcontainer-template.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"version":"0.1.4"}')
    commands = tmp_path / "commands"
    commands.mkdir()
    boundary = '''import hashlib, json, os, pathlib, sys
path = pathlib.Path(os.environ["STATE"])
s = json.loads(path.read_text())
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
def save(): path.write_text(json.dumps(s))
def fail(): save(); sys.exit(1)
def publish():
    assert s["release"] and s["release"]["draft"]
    s["release"]["draft"] = False
    s["tag_sha"] = s["release"]["target_commitish"]
    s["publishes"] += 1
if name == "git":
    if args[0] == "rev-parse": print(os.environ["SOURCE_SHA"])
    elif args[0] == "ls-remote":
        if s["tag_sha"] and not args[-1].endswith("^{}"):
            print(s["tag_sha"] + "\\trefs/tags/" + os.environ["RELEASE_TAG"])
        elif "--exit-code" in args: sys.exit(2)
elif name == "gh":
    if args[0] == "api":
        endpoint = next(a for a in args[1:] if a.startswith("repos/"))
        if "/releases/tags/" in endpoint:
            if not s["release"] or s["release"]["draft"]: fail()
            print(json.dumps(s["release"]))
        elif endpoint.endswith("/releases?per_page=100"):
            s["list_reads"] += 1
            if s["mode"] == "list-failure": fail()
            print("[]")  # A preceding nonmatching page from --paginate.
            print(json.dumps([s["release"]] * (2 if s["mode"] == "duplicate-drafts" else 1) if s["release"] else []))
        elif endpoint.endswith("/releases/91"):
            s["id_reads"] += 1
            if "PATCH" in args:
                target = next((a.split("=", 1)[1] for a in args if a.startswith("target_commitish=")), None)
                if target:
                    s["release"]["target_commitish"] = target
                    s["release"]["body"] = next(a.split("=", 1)[1] for a in args if a.startswith("body="))
                    s["retargets"] += 1
                else: publish()
            print(json.dumps(s["release"]))
        else: raise AssertionError(args)
    elif args[:2] == ["release", "create"]:
        assert s["release"] is None and not s["tag_sha"], "duplicate release or tag creation"
        s["release"] = {"id":91,"tag_name":args[2],"target_commitish":args[args.index("--target")+1],"draft":True,"prerelease":False,"assets":[]}
        s["creates"] += 1
    elif args[:2] == ["release", "upload"]:
        assert "--clobber" not in args
        p = pathlib.Path(args[3])
        assert not any(a["name"] == p.name for a in s["release"]["assets"]), "asset overwrite"
        s["release"]["assets"].append({"name":p.name,"size":p.stat().st_size,"digest":"sha256:"+hashlib.sha256(p.read_bytes()).hexdigest()})
        s["uploads"] += 1
    elif args[:2] == ["release", "edit"]: publish()
    else: raise AssertionError(args)
elif name == "docker": sys.stdin.read()
elif name == "devcontainer": s["templates"] += 1
save()
'''
    for name in ("git", "gh", "docker", "devcontainer"):
        command = commands / name
        command.write_text(f"#!{sys.executable}\n" + boundary)
        command.chmod(0o755)
    return os.environ | {
        "PATH": str(commands) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(ROOT), "RUNNER_TEMP": str(tmp_path), "STATE": str(tmp_path / "state.json"),
        "SOURCE_SHA": SOURCE, "RELEASE_TAG": TAG, "GITHUB_REPOSITORY": "example/synthetic-room",
        "GITHUB_ACTOR": "synthetic-owner", "GH_TOKEN": "synthetic-token", "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "RESUME_EMPTY_DRAFT_ID": "", "EXPECTED_DRAFT_SOURCE_SHA": "",
    }


def _run(tmp_path, env):
    steps = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["publish"]["steps"]
    for name in ("Create or safely resume the owner-approved draft release", "Publish the verified release and approved template"):
        step = next(step for step in steps if step.get("name") == name)
        result = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, env=env, text=True, capture_output=True, check=False)
        if result.returncode:
            return result
        output = tmp_path / "output"
        if output.exists():
            env["RELEASE_ID"] = dict(line.split("=", 1) for line in output.read_text().splitlines())["release_id"]
    return result


@pytest.mark.parametrize("mode", ["new", "draft", "partial-draft", "published"])
def test_draft_discovery_upload_publication_and_published_retry(tmp_path, mode):
    env = _fixture(tmp_path, mode)
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["release"]["draft"] is False and state["tag_sha"] == SOURCE
    assert len(state["release"]["assets"]) == 2
    assert state["creates"] == int(mode == "new")
    assert state["uploads"] == (0 if mode == "published" else 1 if mode == "partial-draft" else 2)
    assert state["publishes"] == int(mode != "published")
    assert state["id_reads"] > 0
    if mode != "published":
        assert state["list_reads"] > 0
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    retried = json.loads((tmp_path / "state.json").read_text())
    assert all(retried[key] == state[key] for key in ("creates", "uploads", "publishes"))


@pytest.mark.parametrize("mode", ["stale-draft", "wrong-tag", "conflicting-asset", "published-without-tag", "orphan-tag", "list-failure", "duplicate-drafts"])
def test_draft_recovery_rejects_wrong_source_tag_or_assets_before_mutation(tmp_path, mode):
    env = _fixture(tmp_path, mode)
    result = _run(tmp_path, env)
    assert result.returncode != 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert all(state[key] == 0 for key in ("creates", "uploads", "publishes", "templates"))


def test_explicit_empty_draft_recovery_and_already_applied_retry(tmp_path):
    env = _fixture(tmp_path, "stale-draft")
    env.update(RESUME_EMPTY_DRAFT_ID="91", EXPECTED_DRAFT_SOURCE_SHA="b" * 40)
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["retargets"] == 1 and state["publishes"] == 1
    assert state["release"]["target_commitish"] == SOURCE and state["tag_sha"] == SOURCE
    assert state["release"]["body"] == f"Exact CI-qualified Josh Room candidate for source {SOURCE}. Published after explicit owner approval."
    result = _run(tmp_path, env)
    assert result.returncode == 0, result.stdout + result.stderr
    retried = json.loads((tmp_path / "state.json").read_text())
    assert all(retried[key] == state[key] for key in ("retargets", "creates", "uploads", "publishes"))


@pytest.mark.parametrize("conflict", ["asset", "published", "tag", "source", "id", "id-only", "source-only", "invalid-id", "invalid-source", "prerelease", "missing-release", "release-tag", "custom-body"])
def test_explicit_empty_draft_recovery_rejects_conflicts_before_mutation(tmp_path, conflict):
    env = _fixture(tmp_path, "stale-draft")
    env.update(RESUME_EMPTY_DRAFT_ID="91", EXPECTED_DRAFT_SOURCE_SHA="b" * 40)
    path = tmp_path / "state.json"
    state = json.loads(path.read_text())
    if conflict == "asset":
        state["release"]["assets"] = [{"name": "unexpected", "size": 1, "digest": "sha256:" + "0" * 64}]
    elif conflict == "published":
        state["release"]["draft"] = False
    elif conflict == "tag":
        state["tag_sha"] = SOURCE
    elif conflict == "source":
        env["EXPECTED_DRAFT_SOURCE_SHA"] = "c" * 40
    elif conflict == "id":
        env["RESUME_EMPTY_DRAFT_ID"] = "92"
    elif conflict == "id-only":
        env["EXPECTED_DRAFT_SOURCE_SHA"] = ""
    elif conflict == "source-only":
        env["RESUME_EMPTY_DRAFT_ID"] = ""
    elif conflict == "invalid-id":
        env["RESUME_EMPTY_DRAFT_ID"] = "not-an-id"
    elif conflict == "invalid-source":
        env["EXPECTED_DRAFT_SOURCE_SHA"] = "main"
    elif conflict == "prerelease":
        state["release"]["prerelease"] = True
    elif conflict == "missing-release":
        state["release"] = None
    elif conflict == "release-tag":
        state["release"]["tag_name"] = "v0.0.0-wrong"
    elif conflict == "custom-body":
        state["release"]["body"] = "Custom release content must not be overwritten."
    path.write_text(json.dumps(state))
    result = _run(tmp_path, env)
    assert result.returncode != 0
    observed = json.loads(path.read_text())
    assert all(observed[key] == 0 for key in ("retargets", "creates", "uploads", "publishes", "templates"))
