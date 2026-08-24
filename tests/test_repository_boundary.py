import json
import subprocess
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


def test_readme_and_workflows_are_josh_room_first():
    assert (ROOT / "README.md").read_text().startswith("# Josh Room\n")
    workflows = "\n".join(path.read_text() for path in (ROOT / ".github/workflows").glob("*.yml"))
    assert "Build and publish the Room of Requirement" not in workflows
    assert "docker build" not in workflows


def test_thin_template_consumes_secure_ror_image():
    template = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "ghcr.io/joshyorko/room-of-requirement@sha256:" in template
    assert "dockerfile" not in template.lower()


def test_template_bootstrap_is_product_owned_and_distro_agnostic():
    bootstrap = ROOT / "templates/room/.devcontainer/bootstrap.sh"
    assert bootstrap.is_file()
    body = bootstrap.read_text()
    assert "git -C \"$jat_root\" fetch" in body
    assert "bootstrap-jat-hololib.sh" in body
    assert 'sudo "$(command -v rcc)" ht shared --enable --once' in body
    assert "rcc ht init" in body
    assert body.index("rcc ht init") < body.index("bootstrap-jat-hololib.sh")
    assert 'for task in Build Restore Serve JAT' in body
    assert 'python -m jat.cli' in body
    assert "brew install age uv libsecret" in body
    assert "scripts/install_dependencies.sh" in body
    assert "CONDA_PREFIX=" in body
    assert "dnf " not in body
    assert "apt " not in body
    assert "rpm-ostree" not in body


def test_vscode_bridge_is_bundled_and_installed_without_marketplace_dependency():
    package = json.loads((ROOT / "vscode-extension/package.json").read_text())
    commands = {item["command"] for item in package["contributes"]["commands"]}
    assert commands == {"joshRoom.save", "joshRoom.enter", "joshRoom.remove", "joshRoom.serve"}
    extension = (ROOT / "vscode-extension/extension.js").read_text()
    assert "josh-room" in extension
    assert "projects" in extension and "hydrate" in extension and "snapshot" in extension
    assert "showQuickPick" in extension and "showInputBox" in extension and "showWarningMessage" in extension
    assert "already has a working folder" in extension
    assert "Replace Latest" in extension
    assert "Opening existing" in extension
    assert '"--snapshot"' in extension
    assert "showOpenDialog" in extension and '"--source"' in extension
    assert "registerTaskProvider" in extension
    assert "createTerminal" in extension and "josh-room serve" in extension
    assert "Include local OCI images" in extension and '"--all-images"' in extension
    assert "onTaskType:josh-room" in package["activationEvents"]
    assert package["contributes"]["taskDefinitions"] == [{"type": "josh-room", "required": ["action"], "properties": {"action": {"type": "string"}}}]
    bootstrap = (ROOT / ".devcontainer/bootstrap.sh").read_text()
    assert ".vscode-server-insiders/extensions/joshyorko.josh-room-0.1.0" in bootstrap
    template_package = json.loads((ROOT / "templates/room/vscode-extension/package.json").read_text())
    assert template_package["contributes"] == package["contributes"]
    assert "showQuickPick" in (ROOT / "templates/room/vscode-extension/extension.js").read_text()


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
    assert lock["candidate_version"] == "0.1.0"
    assert lock["room_of_requirement"]["image"].endswith("@" + lock["room_of_requirement"]["digest"])
    assert len(lock["josh_room"]["git_sha"]) == 40
    assert len(lock["jat"]["git_sha"]) == 40
    hololib = lock["jat"]["hololib"]
    assert hololib["reference"].startswith("ghcr.io/") and "@sha256:" in hololib["reference"]
    assert len(hololib["manifest_digest"].removeprefix("sha256:")) == 64
    assert len(hololib["zip_sha256"]) == 64
    assert hololib["environment_hash"]
    assert hololib["rcc_version"] == lock["rcc"]["version"]
    assert lock["rcc"]["version"].startswith("v")
    for config_path in (
        ROOT / ".devcontainer/devcontainer.json",
        ROOT / "templates/room/.devcontainer/devcontainer.json",
    ):
        assert json.loads(config_path.read_text())["image"] == lock["room_of_requirement"]["image"]
    bootstrap = (ROOT / ".devcontainer/bootstrap.sh").read_text()
    assert lock["josh_room"]["git_sha"] in bootstrap
    assert lock["jat"]["git_sha"] in bootstrap
    assert lock["rcc"]["version"] in bootstrap
    assert "josh-room.git@main" not in bootstrap
    assert "brew install age uv libsecret jq oras" in bootstrap
    assert "bootstrap-jat-hololib.sh" in bootstrap
    helper = (ROOT / ".devcontainer/bootstrap-jat-hololib.sh").read_text()
    assert "oras pull" in helper
    assert "rcc ht import" in helper
    assert "rcc --no-build ht vars" in helper
    assert "Falling back to normal RCC environment build" in helper
    assert (ROOT / ".devcontainer/bootstrap-jat-hololib.sh").read_bytes() == (
        ROOT / "templates/room/.devcontainer/bootstrap-jat-hololib.sh"
    ).read_bytes()
    assert "git clone --depth 1" not in bootstrap
    manifest = json.loads((ROOT / "templates/room/devcontainer-template.json").read_text())
    assert manifest["version"] == lock["template"]["version"]


def test_hololib_bootstrap_falls_back_to_normal_rcc_build(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "rcc.log"
    oras = fake_bin / "oras"
    oras.write_text("#!/usr/bin/env bash\nexit 1\n")
    oras.chmod(0o755)
    rcc = fake_bin / "rcc"
    rcc.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$RCC_LOG"\n')
    rcc.chmod(0o755)
    robot = tmp_path / "robot.yaml"
    robot.write_text("tasks: {}\n")
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "RCC_LOG": str(log),
        "JAT_HOLOLIB_REFERENCE": "ghcr.io/example/hololib@sha256:" + "a" * 64,
        "JAT_HOLOLIB_ZIP_SHA256": "b" * 64,
        "JAT_HOLOLIB_ZIP_SIZE": "1",
        "JAT_HOLOLIB_ENVIRONMENT_HASH": "environment",
        "JAT_GIT_SHA": "c" * 40,
        "EXPECTED_RCC_VERSION": "v18.18.1",
    }

    completed = subprocess.run(
        ["bash", str(ROOT / ".devcontainer/bootstrap-jat-hololib.sh"), str(robot)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Falling back to normal RCC environment build" in completed.stderr
    assert log.read_text().strip() == f"ht vars --robot {robot} --json"
