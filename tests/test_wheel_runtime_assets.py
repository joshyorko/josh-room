"""The installed CLI wheel owns its pinned Restic bootstrap assets."""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.integration
def test_installed_wheel_resolves_its_own_restic_assets(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the real CLI wheel")
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        check=False, cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert built.returncode == 0, built.stderr
    wheel, = (tmp_path / "dist").glob("*.whl")
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("josh_room/_runtime/restic-manifest.json") == (
            ROOT / "vscode-extension/runtime/restic-manifest.json"
        ).read_bytes()
        assert archive.read("josh_room/_runtime/install_restic.py") == (
            ROOT / "scripts/install_restic.py"
        ).read_bytes()
        archive.extractall(installed)
    probe = subprocess.run(
        [sys.executable, "-c", """
import importlib.util
import sys
from pathlib import Path
from josh_room import room_store_bridge

root = Path(sys.argv[1]).resolve()
assert Path(room_store_bridge.__file__).resolve().is_relative_to(root)
manifest, installer = room_store_bridge._managed_runtime_assets()
assert manifest.parent == installer.parent == root / 'josh_room' / '_runtime'
spec = importlib.util.spec_from_file_location('installed_restic_installer', installer)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.VERSION == '0.19.1'
for platform in ('linux-x64', 'win32-x64'):
    _, pin = module._manifest_pin(manifest, platform)
    assert pin['binary'] == ('restic.exe' if platform == 'win32-x64' else 'restic')
assert callable(module.install_restic)
""", str(installed)],
        check=False, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(installed), "PYTHONNOUSERSITE": "1",
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
