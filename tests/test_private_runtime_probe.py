from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room import private_paths
from scripts import verify_private_runtime

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt", reason="This test asserts native POSIX owner and mode semantics")
def test_managed_runtime_probe_reports_bounded_posix_acceptance():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "verify_private_runtime.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["platform"] == sys.platform
    assert "private-directory-owner-and-access" in payload["checks"]
    assert "private-file-owner-and-access" in payload["checks"]
    assert "symlink-rejection" in payload["checks"]
    assert str(ROOT) not in result.stdout
    assert "S-1-" not in result.stdout
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="This test asserts native POSIX owner and mode semantics")
def test_posix_helpers_check_owner_mode_and_nofollow(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    file = directory / "handoff"
    file.write_text("synthetic", encoding="utf-8")
    file.chmod(0o600)

    private_paths.verify_private_path(directory, directory=True)
    private_paths.verify_private_path(file, directory=False)
    assert stat.S_IMODE(directory.lstat().st_mode) == 0o700
    assert stat.S_IMODE(file.lstat().st_mode) == 0o600
    assert directory.lstat().st_uid == os.getuid()
    assert file.lstat().st_uid == os.getuid()

    link = tmp_path / "handoff-link"
    link.symlink_to(file)
    with pytest.raises(private_paths.PrivatePathError, match="link"):
        private_paths.verify_private_path(link, directory=False)

    monkeypatch.setattr(private_paths.os, "getuid", lambda: file.lstat().st_uid + 1)
    with pytest.raises(private_paths.PrivatePathError, match="owner"):
        private_paths.verify_private_path(file, directory=False)


def test_probe_failure_json_keeps_safe_check_and_error_metadata(monkeypatch, capsys):
    def failed_probe():
        raise verify_private_runtime._ProbeFailure(
            "native-directory-owner-dacl-inheritance",
            "PrivatePathError",
            5,
            ["protect-private-runtime-root"],
        )

    monkeypatch.setattr(verify_private_runtime, "run_probe", failed_probe)

    assert verify_private_runtime.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "failed",
        "platform": sys.platform,
        "failed_check": "native-directory-owner-dacl-inheritance",
        "error_type": "PrivatePathError",
        "winerror": 5,
        "checks_completed": ["protect-private-runtime-root"],
    }


def test_probe_failure_includes_static_helper_failure_code(monkeypatch):
    def fail(_path):
        raise private_paths.PrivatePathError(
            "private path security is unsafe", winerror=5
        )

    monkeypatch.setattr(private_paths, "protect_private_directory", fail)

    with pytest.raises(verify_private_runtime._ProbeFailure) as failure:
        verify_private_runtime.run_probe()

    assert failure.value.failed_check == (
        "protect-private-runtime-root:private_path_security_is_unsafe"
    )
    assert failure.value.error_type == "PrivatePathError"
    assert failure.value.winerror == 5
    assert failure.value.checks_completed == []
