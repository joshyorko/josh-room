import hashlib
import json
import subprocess
import sys
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


def verify_staged(root, candidate, *extra_args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--candidate", str(candidate), *extra_args],
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


def test_build_ci_candidate_does_not_need_a_release_tag_or_existing_digest_pin(tmp_path):
    root, candidate, _pin = fixture(tmp_path)
    (root / "release-promotion.json").unlink()
    candidate.rename(tmp_path / "josh-room-0.1.26.vsix")
    candidate = tmp_path / "josh-room-0.1.26.vsix"
    receipt = tmp_path / "candidate.json"

    result = verify_staged(
        root,
        candidate,
        "--version", "0.1.26",
        "--source-sha", "a" * 40,
        "--ci-run-id", "12345",
        "--report", str(receipt),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(receipt.read_text())
    assert payload["artifact"] == candidate.name
    assert payload["version"] == "0.1.26"
    assert payload["source_sha"] == "a" * 40
    assert payload["ci_run_id"] == 12345
    assert payload["size"] == candidate.stat().st_size


def test_build_ci_candidate_inside_extension_is_not_counted_as_source(tmp_path):
    root, candidate, _pin = fixture(tmp_path)
    (root / "release-promotion.json").unlink()
    candidate.rename(root / "vscode-extension/josh-room-0.1.26.vsix")
    candidate = root / "vscode-extension/josh-room-0.1.26.vsix"

    result = verify_staged(
        root,
        candidate,
        "--version", "0.1.26",
        "--source-sha", "a" * 40,
        "--ci-run-id", "12345",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source_files"] == 5


@pytest.mark.parametrize("change", ["source", "run", "artifact"])
def test_candidate_receipt_rejects_stale_source_or_run_or_artifact(tmp_path, change):
    root, candidate, _pin = fixture(tmp_path)
    (root / "release-promotion.json").unlink()
    candidate.rename(tmp_path / "josh-room-0.1.26.vsix")
    candidate = tmp_path / "josh-room-0.1.26.vsix"
    receipt = tmp_path / "candidate.json"
    report_args = ["--report", str(receipt)]
    baseline = verify_staged(
        root, candidate, "--version", "0.1.26", "--source-sha", "a" * 40,
        "--ci-run-id", "12345", *report_args,
    )
    assert baseline.returncode == 0, baseline.stderr
    report = json.loads(receipt.read_text())
    expected_sha = "a" * 40
    expected_run = "12345"
    if change == "source":
        expected_sha = "b" * 40
    elif change == "run":
        expected_run = "54321"
    else:
        report["sha256"] = "0" * 64
        receipt.write_text(json.dumps(report))

    result = verify_staged(
        root, candidate, "--version", "0.1.26", "--source-sha", expected_sha,
        "--ci-run-id", expected_run, "--verify-report", str(receipt),
    )

    assert result.returncode != 0
    assert "candidate receipt differs" in result.stderr
