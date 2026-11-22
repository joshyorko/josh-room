import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
FEATURE_MERGE_SHA = "4e4f5d68133d1b562bdc80eb66bc07fca5db76a9"
PROMOTED_VSIX_SHA256 = "4cb6d27fc52eb0f35cbfed2fdd3e21bc5f29785efa1fa82c9cee245c25a1f7c2"


def test_repository_contains_no_copied_ror_implementation():
    forbidden_directories = [
        ROOT / "src/common",
        ROOT / "src/wolfi",
        ROOT / "src/ubuntu-noble",
        ROOT / "src/debian-trixie",
        ROOT / "automation/maintenance-robot",
    ]
    forbidden_files = [
        ROOT / "docker-bake.hcl",
        ROOT / "release-please-config.json",
    ]
    assert not [path for path in forbidden_directories if path.exists() and any(item.is_file() for item in path.rglob("*"))]
    assert not [path for path in forbidden_files if path.exists()]


def test_windows_checkout_preserves_linux_script_line_endings():
    attributes = (ROOT / ".gitattributes").read_text()
    assert "*.sh text eol=lf" in attributes
    scripts = list((ROOT / ".devcontainer").glob("*.sh")) + list(
        (ROOT / "templates/room/.devcontainer").glob("*.sh")
    )
    assert scripts
    assert not [script for script in scripts if b"\r\n" in script.read_bytes()]


def test_readme_and_workflows_are_josh_room_first():
    assert (ROOT / "README.md").read_text().startswith("# Josh Room\n")
    workflows = "\n".join(path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml"))
    assert "Build and publish the Room of Requirement" not in workflows
    assert "docker build" not in workflows

def test_environment_artifact_boundary_is_current_and_other_projections_deferred():
    readme = (ROOT / "README.md").read_text()
    deferred = (ROOT / "docs/DEFERRED-INTEGRATIONS.md").read_text()
    rcc_version = json.loads((ROOT / "vscode-extension/runtime/manifest.json").read_text())["rcc"]["version"]
    for name in (
        "README.md", "docs/architecture.md", "docs/DEFERRED-INTEGRATIONS.md",
        "docs/adr/0007-deferred-integrations.md", "docs/native-room-store.md",
    ):
        assert re.findall(r"RCC\s+(v\d+\.\d+\.\d+)", (ROOT / name).read_text()) == [rcc_version], name
    components = (ROOT / "docs/room-store-components.md").read_text()
    assert f"RCC version is pinned to `{rcc_version}`" in components
    assert "rcc_environment=auto" in readme
    assert "Actions Runtime integration" in deferred and "remain deferred" in deferred


def test_thin_template_consumes_secure_ror_image():
    template = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "ghcr.io/joshyorko/room-of-requirement@sha256:" in template
    assert "dockerfile" not in template.lower()


def test_template_bootstrap_is_product_owned_and_distro_agnostic():
    bootstrap = ROOT / "templates/room/.devcontainer/bootstrap.sh"
    assert bootstrap.is_file()
    body = bootstrap.read_text()
    assert "Optional golden-host extension copy complete" in body
    extension_version = json.loads((ROOT / "vscode-extension/package.json").read_text())["version"]
    assert f"joshyorko.josh-room-{extension_version}" in body
    assert "Room of Requirement" not in body
    assert "brew" not in body.lower()
    assert "action-server" not in body
    assert "uv tool install" not in body
    assert "bootstrap-jat-environment" not in body
    assert "install_dependencies" not in body
    assert "JOSH_ROOM_GIT_SHA" not in body
    assert "dnf " not in body
    assert "apt " not in body
    assert "rpm-ostree" not in body


def test_vscode_bridge_is_bundled_and_installed_without_marketplace_dependency():
    package = json.loads((ROOT / "vscode-extension/package.json").read_text())
    commands = {item["command"] for item in package["contributes"]["commands"]}
    assert commands == {
        "joshRoom.checkWorkspace",
        "joshRoom.splitWorkspace",
        "joshRoom.addStorage",
        "joshRoom.connectCloudflare",
        "joshRoom.reconnectCloudflare",
        "joshRoom.connectStorage",
        "joshRoom.reconnectStorage",
        "joshRoom.editConnection",
        "joshRoom.disconnectStorage",
        "joshRoom.showLogs",
        "joshRoom.clearLocalFallback",
        "joshRoom.editStorageSettings",
        "joshRoom.link",
        "joshRoom.repair",
        "joshRoom.new", "joshRoom.save", "joshRoom.saveFolderAs", "joshRoom.enter", "joshRoom.remove", "joshRoom.deleteRecoveryPoint", "joshRoom.deleteRoom", "joshRoom.serve", "joshRoom.refresh",
        "joshRoom.prepare",
        "joshRoom.jatBuild", "joshRoom.jatInspect", "joshRoom.jatExtract", "joshRoom.jatRestore",
        "joshRoom.jatServe", "joshRoom.jatExport", "joshRoom.jatCopy",
        "joshRoom.initializeEncryption", "joshRoom.migrateEncryption", "joshRoom.resumeEncryption",
        "joshRoom.exportRecovery", "joshRoom.importRecovery",
        "joshRoom.inspectLogicalSnapshot", "joshRoom.exportPortableJat", "joshRoom.extractLogicalSnapshot", "joshRoom.serveLogicalSnapshot",
        "joshRoom.agentSessionsStatus",
        "joshRoom.agentRecoveryStatus",
        "joshRoom.captureAgentRecovery",
        "joshRoom.restoreAgentRecovery",
