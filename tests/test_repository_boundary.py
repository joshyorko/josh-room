import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]


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
    architecture = (ROOT / "docs/architecture.md").read_text()
    readme = (ROOT / "README.md").read_text()
    deferred = (ROOT / "docs/DEFERRED-INTEGRATIONS.md").read_text()
    assert "immutable JAT RCC v18.19.3 Environment" in architecture
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
    assert "joshyorko.josh-room-0.1.16" in body
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
        "joshRoom.new", "joshRoom.save", "joshRoom.enter", "joshRoom.remove", "joshRoom.serve", "joshRoom.refresh",
        "joshRoom.jatBuild", "joshRoom.jatInspect", "joshRoom.jatExtract", "joshRoom.jatRestore",
        "joshRoom.jatServe", "joshRoom.jatExport", "joshRoom.jatCopy",
    }
    extension = (ROOT / "vscode-extension/extension.js").read_text()
    assert "josh-room" in extension
    assert "projects" in extension and "hydrate" in extension and "snapshot" in extension
    assert "showQuickPick" in extension and "showInputBox" in extension and "showWarningMessage" in extension
    assert "already has a working folder" in extension
    assert "Replace Latest" in extension
    assert "Delete Snapshot" in extension and "also deletes the Room" in extension
    assert "Opening existing" in extension
    assert '"--snapshot"' in extension
    assert "showOpenDialog" in extension and '"--source"' in extension
    assert "registerTaskProvider" not in extension
    assert "createTreeView" in extension and "createOutputChannel" in extension
    assert "childProcess.spawn" in extension and "onCancellationRequested" in extension
    assert "SIGTERM" in extension
    assert "No saved Rooms" in extension and "Couldn't load Rooms" in extension
    assert "is already open" in extension
    assert "forceCreate" in extension
    assert "createStatusBarItem" in extension and "sync~spin" in extension
    assert "createFileSystemWatcher" in extension and "onDidChangeTextDocument" in extension and "Needs save" in extension
    assert "onStartupFinished" in package["activationEvents"]
    assert "createTerminal" in extension and "runtime.command" in extension
    assert "Include local OCI images" in extension and '"--all-images"' in extension
    assert "onTaskType:josh-room" not in package["activationEvents"]
    assert "taskDefinitions" not in package["contributes"]
    assert package["contributes"]["viewsContainers"]["activitybar"][0]["id"] == "josh-room"
    assert package["contributes"]["views"]["josh-room"][0]["id"] == "joshRoom.rooms"
    assert package["contributes"]["viewsWelcome"][0]["view"] == "joshRoom.rooms"
    assert {view["id"] for view in package["contributes"]["views"]["josh-room"]} == {"joshRoom.rooms", "joshRoom.jatTools"}
    assert "Pack Folder into JAT" in extension and "Restore Workspace" in extension and "Serve JAT…" in extension
    assert "joshRoom.jatInspect" in extension and "joshRoom.jatCopy" in extension
    jat_command_ids = {
        "joshRoom.jatBuild", "joshRoom.jatInspect", "joshRoom.jatExtract", "joshRoom.jatRestore",
        "joshRoom.jatServe", "joshRoom.jatExport", "joshRoom.jatCopy",
    }
    assert jat_command_ids <= commands
    assert "Use Dimension" not in extension
    assert "Open Dimension" not in extension


def test_vsix_owns_the_runtime_bootstrap_contract():
    package = json.loads((ROOT / "vscode-extension/package.json").read_text())
    runtime = json.loads((ROOT / "vscode-extension/runtime/manifest.json").read_text())
    extension = (ROOT / "vscode-extension/extension.js").read_text()

    assert package["version"] == "0.1.16"
    assert package["scripts"]["package"]
    assert (ROOT / "vscode-extension/.vscodeignore").is_file()
    vscodeignore = (ROOT / "vscode-extension/.vscodeignore").read_text()
    assert "runtime/controller/**/__pycache__/**" in vscodeignore
    assert "runtime/controller/**/*.pyc" in vscodeignore
    assert runtime["extension_version"] == package["version"]
    assert runtime["rcc"]["version"] == "v18.19.3"
    assert runtime["rcc"]["platforms"]["linux-x64"]["asset"] == "rcc-linux64"
    assert runtime["jat"]["environment_artifact"]["digest"].startswith("sha256:")
    assert "ensureManagedRcc" in extension and "ensureJatRuntime" in extension
    assert 'childProcess.spawn("josh-room"' not in extension
    assert "runtime.command" in extension and "runtime.args(args)" in extension


def test_real_vsix_contains_the_owned_runtime_contract(tmp_path):
    output = tmp_path / "josh-room.vsix"
    result = subprocess.run(
        ["npm", "run", "package", "--", "--out", str(output)],
        cwd=ROOT / "vscode-extension",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"vsce package failed: stdout={result.stdout!r} stderr={result.stderr!r}"

    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert "extension/runtime.js" in names
        assert "extension/runtime/manifest.json" in names
        assert "extension/runtime/controller/robot.yaml" in names
        assert "extension/runtime/controller/conda.yaml" in names
        assert any(name.startswith("extension/runtime/controller/josh_room/") and name.endswith(".py") for name in names)
        assert not any("__pycache__" in name or name.endswith((".pyc", ".test.js")) for name in names)
        assert not any(name.startswith("extension/runtime/controller/output/") for name in names)
        package = json.loads(archive.read("extension/package.json"))
        manifest = json.loads(archive.read("extension/runtime/manifest.json"))
        assert manifest["extension_version"] == package["version"]
        assert manifest["jat"]["environment_artifact"]["digest"].startswith("sha256:")


def test_tag_release_publishes_versioned_vsix_and_checksums():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert 'tags:\n      - "v*"' in workflow
    assert "contents: write" in workflow
    assert "npm run package" in workflow
    assert "josh-room-${version}.vsix" in workflow
    assert "sha256sum * > SHA256SUMS" in workflow
    assert 'gh release create "$GITHUB_REF_NAME"' in workflow
    assert "--verify-tag" in workflow and "--prerelease" not in workflow


def test_packaged_controller_uses_the_module_entrypoint_not_a_global_script():
    package = json.loads((ROOT / "vscode-extension/package.json").read_text())
    recipe = (ROOT / "vscode-extension/runtime/controller/robot.yaml").read_text()
    assert "shell: python -m josh_room\n" in recipe
    assert "python -m josh_room.cli" not in recipe
    remove_menu = next(item for item in package["contributes"]["menus"]["view/item/context"] if item["command"] == "joshRoom.remove")
    assert remove_menu["group"].startswith("inline")
    assert (ROOT / "vscode-extension/media/room.svg").is_file()
    bootstrap = (ROOT / ".devcontainer/bootstrap.sh").read_text()
    assert "joshyorko.josh-room-0.1.16" in bootstrap
    template_package = json.loads((ROOT / "templates/room/vscode-extension/package.json").read_text())
    assert template_package["contributes"] == package["contributes"]
    assert "showQuickPick" in (ROOT / "templates/room/vscode-extension/extension.js").read_text()


def test_vscode_extension_root_and_template_copies_are_byte_identical():
    for name in (
        "extension.js", "package.json", "dirty.js", "progress.js", "registry.js", "runtime.js",
        "runtime/manifest.json", "runtime/controller/robot.yaml", "runtime/controller/conda.yaml",
        "runtime/controller/environment_linux_amd64_freeze.yaml", "media/room.svg",
    ):
        assert (ROOT / "vscode-extension" / name).read_bytes() == (
            ROOT / "templates/room/vscode-extension" / name
        ).read_bytes()


def test_oauth_room_requires_no_host_setup_mount():
    readme = (ROOT / "README.md").read_text()
    setup = (ROOT / "docs/R2-SETUP.md").read_text()
    template = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "no host enrollment" in readme
    assert "Normal use requires no host setup" in setup
    assert '"mounts"' not in template
    assert '"initializeCommand"' not in template


def test_root_devcontainer_is_the_personal_room_and_matches_template():
    assert (ROOT / ".devcontainer/devcontainer.json").read_bytes() == (
        ROOT / "templates/room/.devcontainer/devcontainer.json"
    ).read_bytes()
    assert (ROOT / ".devcontainer/bootstrap.sh").read_bytes() == (
        ROOT / "templates/room/.devcontainer/bootstrap.sh"
    ).read_bytes()
    assert not (ROOT / ".vscode/tasks.json").exists()
    assert not (ROOT / "templates/room/.vscode/tasks.json").exists()


def test_devcontainer_does_not_publish_host_runtime_paths():
    root_config = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
    template_config = json.loads((ROOT / "templates/room/.devcontainer/devcontainer.json").read_text())
    for config in (root_config, template_config):
        assert config["remoteEnv"] == {"JOSH_ROOM_INSTANCE": "/home/vscode/.local/state/josh-room"}
        assert "ROBOCORP_HOME" not in config["remoteEnv"]
        assert "JOSH_ROOM_JAT_ROOT" not in config["remoteEnv"]


def test_devcontainer_opens_clean_room_not_controller_source():
    config = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
    assert config["workspaceFolder"] == "/workspaces/room"
    assert "${localWorkspaceFolderBasename}" not in config["onCreateCommand"]
    assert "/workspaces/room/.devcontainer/prepare-workspace.sh" in config["onCreateCommand"]
    assert "/home/vscode/.local/share/josh-room/controller/.devcontainer/bootstrap.sh" == config["postCreateCommand"].removeprefix("bash ")
    prepare = (ROOT / ".devcontainer/prepare-workspace.sh").read_text()
    assert "JOSH_ROOM_CONTROLLER_ROOT" in prepare
    assert (ROOT / ".devcontainer/prepare-workspace.sh").read_bytes() == (
        ROOT / "templates/room/.devcontainer/prepare-workspace.sh"
    ).read_bytes()


def test_prepare_workspace_relocates_controller_and_leaves_clean_room(tmp_path):
    room = tmp_path / "room"
    controller = tmp_path / "controller"
    (room / ".devcontainer").mkdir(parents=True)
    (room / "src/josh_room").mkdir(parents=True)
    (room / ".devcontainer/prepare-workspace.sh").write_bytes(
        (ROOT / ".devcontainer/prepare-workspace.sh").read_bytes()
    )
    (room / "src/josh_room/__init__.py").write_text("")
    (room / "README.md").write_text("controller source")

    subprocess.run(
        ["bash", str(room / ".devcontainer/prepare-workspace.sh"), str(room)],
        env={"PATH": "/usr/bin:/bin", "JOSH_ROOM_CONTROLLER_ROOT": str(controller)},
        check=True,
    )

    assert (controller / "README.md").read_text() == "controller source"
    assert not any(room.iterdir())


def test_template_publish_workflow_is_narrow_and_uses_devcontainer_cli():
    workflow = (ROOT / ".github/workflows/publish-template.yml").read_text()
    assert 'paths:' in workflow
    assert '"templates/room/**"' in workflow
    assert "packages: write" in workflow
    assert "docker/login-action@" in workflow
    assert "secrets.GITHUB_TOKEN" in workflow
    assert "devcontainer templates publish" in workflow
    assert "--namespace ${{ github.repository }}/templates" in workflow
    assert '"templates/room"' in workflow
    assert "docker build" not in workflow
    assert "build-image" not in workflow


def test_ci_installs_real_age_tooling_before_tests():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "apt-get install" in workflow
    assert "age" in workflow


def test_personal_template_has_no_host_runtime_assumption():
    config = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    bootstrap = (ROOT / "templates/room/.devcontainer/bootstrap.sh").read_text()
    assert "/run/user/1000" not in config
    assert "/run/user" not in config
    assert "prepare-kubernetes-secret" not in config
    assert "RUNTIME_SECRET_NAME" not in bootstrap


def test_kubernetes_secret_authority_is_narrow_and_automatic():
    prepare = (ROOT / ".devcontainer/prepare-kubernetes-secret.sh").read_text()
    assert "mint_r2_temp.py" in prepare
    assert "parent-secret" in prepare
    assert "TTL_SECONDS=21600" in prepare
    assert "resourceNames" in prepare
    assert "kubectl" in prepare and "apply -f" in prepare
    assert "--from-literal" not in prepare
    assert "DEVPOD_ADDITIONAL_ENV" not in prepare
    assert (ROOT / ".devcontainer/prepare-kubernetes-secret.sh").read_bytes() == (
        ROOT / "templates/room/.devcontainer/prepare-kubernetes-secret.sh"
    ).read_bytes()


def test_v0_1_candidate_tuple_is_immutable_and_consumed_by_both_entries():
    lock = json.loads((ROOT / "release-lock.json").read_text())
    assert lock["format_version"] == 1
    assert lock["candidate_version"] == "0.1.16"
    assert lock["optional_golden_host"]["image"].endswith("@" + lock["optional_golden_host"]["digest"])
    assert len(lock["josh_room"]["git_sha"]) == 40
    assert lock["josh_room"]["git_sha"] == "efb1c71157861fa1c7b7ad01698d3a272cb2f1aa"
    assert len(lock["jat"]["git_sha"]) == 40
    artifact = lock["jat"]["environment_artifact"]
    assert artifact["archive_url"].endswith("/jat-runtime-linux-amd64.rcca")
    assert artifact["release_tag"] == "v0.1.9-jat-runtime"
    assert len(artifact["archive_sha256"]) == 64
    assert artifact["archive_size"] > 0
    assert artifact["rcc_artifact_digest"].startswith("sha256:")
    assert artifact["rcc_version"] == "v18.19.3"
    assert lock["rcc"]["version"] == "v18.19.3"
    assert lock["rcc"]["source_sha"] == "4148c2b71705c9d2baf0e88b48d08a79cb7bda0f"
    assert lock["rcc"]["managed_asset"] == "rcc-linux64"
    assert len(lock["rcc"]["managed_asset_sha256"]) == 64
    for config_path in (
        ROOT / ".devcontainer/devcontainer.json",
        ROOT / "templates/room/.devcontainer/devcontainer.json",
    ):
        assert json.loads(config_path.read_text())["image"] == lock["optional_golden_host"]["image"]
    bootstrap = (ROOT / ".devcontainer/bootstrap.sh").read_text()
    assert "Optional golden-host extension copy complete" in bootstrap
    assert "brew" not in bootstrap.lower()
    assert "action-server" not in bootstrap
    assert "josh-room.git@main" not in bootstrap
    manifest = json.loads((ROOT / "templates/room/devcontainer-template.json").read_text())
    assert manifest["version"] == lock["template"]["version"]
