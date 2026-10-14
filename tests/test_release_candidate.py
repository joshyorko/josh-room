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
