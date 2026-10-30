"""The hosted presentation fixture must not delete anything during activation."""
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared_ui_fixture(tmp_path):
    vsix = tmp_path / "candidate.vsix"
    with zipfile.ZipFile(vsix, "w") as archive:
        for name in ("extension.js", "package.json"):
            archive.write(ROOT / "vscode-extension" / name, f"extension/{name}")
    digest = hashlib.sha256(vsix.read_bytes()).hexdigest()
    output = tmp_path / "presentation"
    result = subprocess.run(
        ["node", str(ROOT / "scripts/prepare_issue165_ui_fixture.js"), str(vsix), str(output)],
        check=True, capture_output=True, text=True,
        env={**os.environ, "ISSUE165_PYTHON": os.sys.executable},
    )
    return vsix, digest, output, json.loads(result.stdout)


def test_presentation_fixture_keeps_original_vsix_and_public_command_manifest(prepared_ui_fixture):
    vsix, digest, output, receipt = prepared_ui_fixture
    assert hashlib.sha256(vsix.read_bytes()).hexdigest() == digest
    manifest = json.loads((output / receipt["candidate_extension"] / "package.json").read_text())
    assert manifest == json.loads((ROOT / "vscode-extension/package.json").read_text())


def test_fixture_driver_only_activates_and_loads_the_catalog(prepared_ui_fixture, tmp_path):
    _vsix, _digest, output, receipt = prepared_ui_fixture
    driver = output / receipt["driver_extension"] / "extension.js"
    probe = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const commands = [];
const api = {
  workspace: { workspaceFolders: [{ uri: { fsPath: process.argv[2] } }] },
  extensions: { getExtension: () => ({ activate: async () => {} }) },
  commands: { executeCommand: async (name) => { commands.push(name); return 'loaded'; } },
};
const context = { exports: {}, require: (name) => name === 'vscode' ? api : require(name),
  setTimeout: (fn) => { fn(); }, process };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
context.exports.activate().then(() => console.log(JSON.stringify(commands)));
'''
    result = subprocess.run(["node", "-e", probe, str(driver), str(tmp_path)],
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == ["workbench.view.extension.josh-room", "joshRoom.testInjectSyntheticCatalog"]
