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


def test_installer_metadata_does_not_change_the_public_manifest_contract(prepared_ui_fixture, tmp_path):
    _vsix, _digest, output, receipt = prepared_ui_fixture
    source = ROOT / "vscode-extension/package.json"
    manifest = json.loads(source.read_text())
    installed = tmp_path / "installed-package.json"
    installed.write_text(json.dumps({**manifest, "__metadata": {"source": "vsix", "installedTimestamp": 1}}, indent="\t"))
    fixture = output / receipt["candidate_extension"] / "package.json"
    code = "require(process.argv[1]).verifyInstalledManifests(process.argv[2], process.argv[3], process.argv[4])"
    result = subprocess.run(["node", "-e", code, str(ROOT / "scripts/capture_issue165_windows_ui.js"),
                             str(installed), str(fixture), str(source)], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_installer_metadata_exception_never_accepts_changed_commands(prepared_ui_fixture, tmp_path):
    _vsix, _digest, output, receipt = prepared_ui_fixture
    source = ROOT / "vscode-extension/package.json"
    manifest = json.loads(source.read_text())
    manifest["contributes"]["commands"] = []
    installed = tmp_path / "installed-package.json"
    installed.write_text(json.dumps({**manifest, "__metadata": {"source": "vsix"}}, indent="\t"))
    fixture = output / receipt["candidate_extension"] / "package.json"
    code = "require(process.argv[1]).verifyInstalledManifests(process.argv[2], process.argv[3], process.argv[4])"
    result = subprocess.run(["node", "-e", code, str(ROOT / "scripts/capture_issue165_windows_ui.js"),
                             str(installed), str(fixture), str(source)], check=False, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Installed public manifest differs" in result.stderr
