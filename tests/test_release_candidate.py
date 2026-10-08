import hashlib
import json
import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/verify_release_candidate.py"


def fixture(tmp_path):
    root = tmp_path / "source"
    extension = root / "vscode-extension"
    (extension / "runtime").mkdir(parents=True)
    files = {
        "package.json": json.dumps({"version": "0.1.26"}),
        "runtime/manifest.json": json.dumps({"extension_version": "0.1.26"}),
        "extension.js": "module.exports = {};\n",
        "README.md": "Synthetic readme\n",
        "LICENSE": "Synthetic license\n",
    }
    for name, body in files.items():
        (extension / name).write_text(body)
    candidate = tmp_path / "candidate.vsix"
    with zipfile.ZipFile(candidate, "w") as archive:
        for name, body in files.items():
            name = {"README.md": "readme.md", "LICENSE": "LICENSE.txt"}.get(name, name)
            archive.writestr("extension/" + name, body)
        archive.writestr("extension.vsixmanifest", "synthetic metadata")
        archive.writestr("[Content_Types].xml", "synthetic metadata")
    pin = {"version": "0.1.26", "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
    (root / "release-promotion.json").write_text(json.dumps(pin))
    return root, candidate, pin


def verify(root, candidate, tag="v0.1.26-standalone-vsix"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--candidate", str(candidate), "--tag", tag],
        capture_output=True, text=True, check=False,
    )


def test_verifies_exact_candidate_without_modifying_archive(tmp_path):
    root, candidate, pin = fixture(tmp_path)
    before = candidate.read_bytes()
    result = verify(root, candidate)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["sha256"] == pin["sha256"]
    assert json.loads(result.stdout)["source_files"] == 5
    assert candidate.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing", "checksum", "source", "missing-source", "unpackaged-source", "tag", "version", "extra-member"])
def test_rejects_candidate_or_source_drift_without_rebuilding(tmp_path, failure):
    root, candidate, pin = fixture(tmp_path)
    tag = "v0.1.26-standalone-vsix"
    if failure == "missing":
        candidate.unlink()
    elif failure == "checksum":
        candidate.write_bytes(candidate.read_bytes() + b"changed")
    elif failure == "source":
        (root / "vscode-extension/extension.js").write_text("changed")
    elif failure == "missing-source":
        (root / "vscode-extension/extension.js").unlink()
    elif failure == "unpackaged-source":
        (root / "vscode-extension/new-runtime.js").write_text("required source")
    elif failure == "tag":
        tag = "v0.1.27-standalone-vsix"
    elif failure == "version":
        pin["version"] = "0.1.27"
        tag = "v0.1.27-standalone-vsix"
    elif failure == "extra-member":
        with zipfile.ZipFile(candidate, "a") as archive:
            archive.writestr("extension/../outside", "unsafe")
        pin["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    (root / "release-promotion.json").write_text(json.dumps(pin))
    before = candidate.read_bytes() if candidate.exists() else None
    result = verify(root, candidate, tag)
    assert result.returncode != 0
    assert "candidate verification failed" in result.stderr
    assert (candidate.read_bytes() if candidate.exists() else None) == before


def workflow_step(name):
    workflow = (SCRIPT.parents[1] / ".github/workflows/release.yml").read_text()
    step = workflow.split(f"      - name: {name}\n", 1)[1].split("\n      - ", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1]).replace(
        "${{ steps.version.outputs.version }}", "0.1.26",
    )


@pytest.mark.parametrize("failure", [None, "checksum", "source", "not-draft", "wrong-source-sha", "wrong-tag-sha", "tag-moved-before-publish", "missing-download", "changed-before-publish", "changed-after-publish"])
@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "workflow_dispatch_existing"])
def test_workflow_promotes_only_verified_draft_asset(tmp_path, failure, event):
    root, candidate, _pin = fixture(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts/verify_release_candidate.py").symlink_to(SCRIPT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    gh = bin_dir / "gh"
    git = bin_dir / "git"
    git.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
        import os, pathlib, sys
        args = sys.argv[1:]
        if args[:2] != ["ls-remote", "origin"]:
            raise AssertionError(args)
        root = pathlib.Path.cwd()
        count_file = root / "git-calls"
        count = int(count_file.read_text()) + 1 if count_file.exists() else 1
        count_file.write_text(str(count))
        failure = os.environ["SCENARIO"]
        if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch" and os.environ.get("INITIAL_TAG") != "1" and not (root / "tag-created").exists() and failure != "wrong-tag-sha":
            sys.exit(0)
        if failure == "wrong-tag-sha" or (failure == "tag-moved-before-publish" and (root / "dist/package.whl").exists()) or (failure == "tag-moved-after-create" and (root / "tag-created").exists()):
            tag_sha = "b" * 40
        else:
            tag_sha = os.environ["RELEASE_SHA"]
        print(f"{tag_sha}\t{args[2]}")
    '''))
    git.chmod(0o700)
    gh.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
        import json, os, pathlib, shutil, sys
        args = sys.argv[1:]
        root = pathlib.Path.cwd()
        log = root / "gh-calls.jsonl"
        previous = log.read_text().splitlines() if log.exists() else []
        with log.open("a") as stream:
            stream.write(json.dumps(args) + "\\n")
        failure = os.environ["SCENARIO"]
        if args[:2] == ["release", "view"]:
            print(json.dumps({"isDraft": failure != "not-draft" and not (root / "published").exists(), "targetCommitish": "wrong" if failure == "wrong-source-sha" else os.environ["RELEASE_SHA"], "tagName": os.environ["RELEASE_TAG"]}))
        elif args[:2] == ["release", "download"]:
            if failure == "missing-download": sys.exit(1)
            target = pathlib.Path(args[args.index("--dir") + 1]) / args[args.index("--pattern") + 1]
            shutil.copyfile(os.environ["CANDIDATE"], target)
            if failure == "changed-before-publish" and any(json.loads(line)[:2] == ["release", "download"] for line in previous):
                target.write_bytes(target.read_bytes() + b"changed")
            if failure == "changed-after-publish" and (root / "published").exists():
                target.write_bytes(target.read_bytes() + b"changed")
        elif args[:2] == ["release", "upload"]:
            assert "--clobber" not in args
            assert not any(arg.endswith(".vsix") for arg in args)
            assert all(pathlib.Path(arg).is_file() for arg in args[3:args.index("--repo")])
        elif args[:2] == ["release", "edit"]:
            assert "--draft=false" in args
            (root / "published").touch()
        elif args[:3] == ["api", "--method", "POST"]:
            assert args[3] == "repos/joshyorko/josh-room/git/refs"
            assert "ref=refs/tags/" + os.environ["RELEASE_TAG"] in args
            assert "sha=" + os.environ["RELEASE_SHA"] in args
            if failure == "tag-create-race":
                sys.exit(1)
            (root / "tag-created").touch()
        else:
            raise AssertionError(args)
    '''))
    gh.chmod(0o700)
    if failure == "checksum":
        candidate.write_bytes(candidate.read_bytes() + b"changed")
    elif failure == "source":
        (root / "vscode-extension/extension.js").write_text("changed")
    before = candidate.read_bytes()
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           "GITHUB_REPOSITORY": "joshyorko/josh-room", "GITHUB_REF_NAME": "main" if event == "workflow_dispatch" else "v0.1.26-standalone-vsix",
           "GITHUB_SHA": ("b" if event == "workflow_dispatch" else "a") * 40, "RELEASE_SHA": "a" * 40, "RELEASE_TAG": "v0.1.26-standalone-vsix", "GITHUB_EVENT_NAME": "workflow_dispatch" if event.startswith("workflow_dispatch") else event, "INITIAL_TAG": "1" if event.endswith("_existing") else "0", "CANDIDATE": str(candidate), "SCENARIO": failure or ""}
    result = subprocess.run(["bash", "-c", workflow_step("Verify staged tested VSIX")], cwd=root, env=env, capture_output=True, text=True, check=False)
    if result.returncode == 0 and event.startswith("workflow_dispatch"):
        result = subprocess.run(["bash", "-c", workflow_step("Create verified release tag")], cwd=root, env=env, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        for name in ["package.whl", "package.tar.gz", "SHA256SUMS"]:
            (root / "dist" / name).write_text("synthetic ancillary artifact")
        result = subprocess.run(["bash", "-c", workflow_step("Publish GitHub release")], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (failure is None), result.stderr
    assert (root / "published").exists() == (failure in [None, "changed-after-publish"])
    assert candidate.read_bytes() == before
    calls = [json.loads(line) for line in (root / "gh-calls.jsonl").read_text().splitlines()] if (root / "gh-calls.jsonl").exists() else []
    assert all(call[:2] in [["release", action] for action in ["view", "download", "upload", "edit"]] or call[:3] == ["api", "--method", "POST"] for call in calls)
    if event == "workflow_dispatch_existing":
        assert not any(call[:1] == ["api"] for call in calls)
    if failure in ["checksum", "source", "not-draft", "wrong-source-sha", "wrong-tag-sha", "missing-download"]:
        assert not (root / "tag-created").exists()


def test_release_dispatch_keeps_existing_permissions_and_checks_out_explicit_source():
    import yaml

    workflow = yaml.safe_load((SCRIPT.parents[1] / ".github/workflows/release.yml").read_text())
    events = workflow.get("on", workflow.get(True))
    assert events["workflow_dispatch"]["inputs"]["source_sha"]["required"] is True
    assert events["workflow_dispatch"]["inputs"]["release_tag"]["required"] is True
    assert workflow["permissions"] == {"contents": "write"}
    assert "inputs.release_tag" in workflow["concurrency"]["group"]
    job = workflow["jobs"]["build"]
    assert "inputs.source_sha" in job["steps"][0]["with"]["ref"]
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Verify staged tested VSIX") < names.index("Create verified release tag")
    assert names.index("Create verified release tag") < names.index("Publish GitHub release")


@pytest.mark.parametrize("failure", [None, "malformed-sha", "injected-sha", "wrong-head", "untrusted-source", "wrong-workflow-ref", "wrong-tag", "injected-tag"])
def test_dispatch_version_gate_rejects_untrusted_or_ambiguous_source(tmp_path, failure):
    root, _candidate, _pin = fixture(tmp_path)
    template = root / "templates/room/vscode-extension"
    template.mkdir(parents=True)
    (template / "package.json").write_text('{"version":"0.1.26"}')
    binary = tmp_path / "bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(f"#!{sys.executable}\n" + textwrap.dedent('''\
        import os, sys
        if sys.argv[1:] == ["rev-parse", "HEAD"]:
            print(("b" if os.environ["SCENARIO"] == "wrong-head" else "a") * 40)
        elif sys.argv[1:3] == ["merge-base", "--is-ancestor"]:
            assert sys.argv[3:] == ["a" * 40, "origin/main"]
            sys.exit(1 if os.environ["SCENARIO"] == "untrusted-source" else 0)
        else:
            raise AssertionError(sys.argv)
    '''))
    git.chmod(0o700)
    sha = "a" * 40
    if failure == "malformed-sha":
        sha = "main"
    if failure == "injected-sha":
        sha = "$(touch injected)"
    tag = "v0.1.26-standalone-vsix"
    if failure == "wrong-tag":
        tag = "v0.1.27-standalone-vsix"
    if failure == "injected-tag":
        tag = "$(touch injected)"
    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
           "GITHUB_REPOSITORY": "joshyorko/josh-room", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_REF": "refs/heads/untrusted" if failure == "wrong-workflow-ref" else "refs/heads/main",
           "RELEASE_SHA": sha, "RELEASE_TAG": tag, "SCENARIO": failure or "", "GITHUB_OUTPUT": str(tmp_path / "outputs")}
    result = subprocess.run(["bash", "-c", workflow_step("Verify tag and package version")], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (failure is None), result.stderr
    assert not (root / "injected").exists()


@pytest.mark.parametrize("failure", ["tag-create-race", "tag-moved-after-create"])
def test_dispatch_tag_creation_races_fail_closed(tmp_path, failure):
    test_workflow_promotes_only_verified_draft_asset(tmp_path, failure, "workflow_dispatch")
