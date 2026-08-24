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
    assert "ghcr.io/joshyorko/josh-room" not in workflows


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
    assert 'for task in Build Restore Serve 3tc' in body
    assert 'python -m jat.cli' in body
    assert "brew install age uv libsecret" in body
    assert "scripts/install_dependencies.sh" in body
    assert "CONDA_PREFIX=" in body
    assert "dnf " not in body
    assert "apt " not in body
    assert "rpm-ostree" not in body


def test_development_container_keeps_venv_outside_bind_mount():
    config = (ROOT / ".devcontainer/devcontainer.json").read_text()
    assert '"UV_PROJECT_ENVIRONMENT": "/home/vscode/.local/share/josh-room/dev-venv"' in config


def test_personal_template_keyring_mount_has_no_uid_assumption():
    config = (ROOT / "templates/room/.devcontainer/devcontainer.json").read_text()
    assert "/run/user/1000" not in config
    assert "/run/josh-room/host-session-bus" in config
