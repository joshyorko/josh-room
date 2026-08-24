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
    assert "ghcr.io/joshyorko/room-of-requirement:secure" in template
    assert "dockerfile" not in template.lower()


def test_template_bootstrap_is_product_owned_and_distro_agnostic():
    bootstrap = ROOT / "templates/room/.devcontainer/bootstrap.sh"
    assert bootstrap.is_file()
    body = bootstrap.read_text()
    assert "git clone --depth 1" in body
    assert "rcc ht vars" in body
    assert 'for task in Build Restore Serve JAT' in body
    assert 'python -m jat.cli' in body
    assert "brew install age uv libsecret" in body
    assert "scripts/install_dependencies.sh" in body
    assert "CONDA_PREFIX=" in body
    assert "dnf " not in body
    assert "apt " not in body
    assert "rpm-ostree" not in body


def test_setup_is_host_only_and_room_config_is_read_only():
    readme = (ROOT / "README.md").read_text()
    setup = (ROOT / "docs/R2-SETUP.md").read_text()
    template = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "Run setup on the Bluefin host" in readme
    assert "Do not run `josh-room setup` inside the Room" in setup
    assert "type=bind,readonly" in template


def test_root_devcontainer_is_the_personal_room_and_matches_template():
    assert (ROOT / ".devcontainer/devcontainer.json").read_bytes() == (
        ROOT / "templates/room/.devcontainer/devcontainer.json"
    ).read_bytes()
    assert (ROOT / ".devcontainer/bootstrap.sh").read_bytes() == (
        ROOT / "templates/room/.devcontainer/bootstrap.sh"
    ).read_bytes()
    assert (ROOT / ".vscode/tasks.json").read_bytes() == (ROOT / "templates/room/.vscode/tasks.json").read_bytes()


def test_template_publish_workflow_is_narrow_and_uses_devcontainer_cli():
    workflow = (ROOT / ".github/workflows/publish-template.yml").read_text()
    assert 'paths:' in workflow
    assert '"templates/room/**"' in workflow
    assert "packages: write" in workflow
    assert "devcontainer templates publish" in workflow
    assert "--namespace ${{ github.repository }}/templates" in workflow
    assert '"templates/room"' in workflow
    assert "docker build" not in workflow
    assert "build-image" not in workflow


def test_personal_template_keyring_mount_has_no_uid_assumption():
    config = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "/run/user/1000" not in config
    assert "/run/josh-room/host-session-bus" in config
