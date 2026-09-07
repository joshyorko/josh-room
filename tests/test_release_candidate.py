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
        "package.json": json.dumps({"version": "0.1.24"}),
        "runtime/manifest.json": json.dumps({"extension_version": "0.1.24"}),
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
    pin = {"version": "0.1.24", "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
    (root / "release-promotion.json").write_text(json.dumps(pin))
    return root, candidate, pin


def verify(root, candidate, tag="v0.1.24-standalone-vsix"):
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
    tag = "v0.1.24-standalone-vsix"
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
        tag = "v0.1.25-standalone-vsix"
    elif failure == "version":
        pin["version"] = "0.1.25"
        tag = "v0.1.25-standalone-vsix"
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
        "${{ steps.version.outputs.version }}", "0.1.24",
    )


@pytest.mark.parametrize("failure", [None, "checksum", "source", "not-draft", "wrong-source-sha", "missing-download", "changed-before-publish", "changed-after-publish"])
def test_workflow_promotes_only_verified_draft_asset(tmp_path, failure):
    root, candidate, _pin = fixture(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts/verify_release_candidate.py").symlink_to(SCRIPT)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").symlink_to(sys.executable)
    gh = bin_dir / "gh"
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
            print(json.dumps({"isDraft": failure != "not-draft" and not (root / "published").exists(), "targetCommitish": "wrong" if failure == "wrong-source-sha" else os.environ["GITHUB_SHA"], "tagName": os.environ["GITHUB_REF_NAME"]}))
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
           "GITHUB_REPOSITORY": "joshyorko/josh-room", "GITHUB_REF_NAME": "v0.1.24-standalone-vsix",
           "GITHUB_SHA": "a" * 40, "CANDIDATE": str(candidate), "SCENARIO": failure or ""}
    result = subprocess.run(["bash", "-c", workflow_step("Verify staged tested VSIX")], cwd=root, env=env, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        for name in ["package.whl", "package.tar.gz", "SHA256SUMS"]:
            (root / "dist" / name).write_text("synthetic ancillary artifact")
        result = subprocess.run(["bash", "-c", workflow_step("Publish GitHub release")], cwd=root, env=env, capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == (failure is None), result.stderr
    assert (root / "published").exists() == (failure in [None, "changed-after-publish"])
    assert candidate.read_bytes() == before
    calls = [json.loads(line) for line in (root / "gh-calls.jsonl").read_text().splitlines()]
    assert all(call[:2] in [["release", action] for action in ["view", "download", "upload", "edit"]] for call in calls)
