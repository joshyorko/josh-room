from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "checks_completed": ["protect-private-runtime-root"],
    }


def test_changetime_probe_compares_mtime_at_native_100ns_resolution():
    before = 12_345_600
    assert verify_private_runtime._changetime_edit_predicates(
        before_size=28,
        after_size=28,
        expected_size=28,
        before_mtime_ns=before,
        after_mtime_ns=before + 99,
        before_change=1000,
        after_change=1100,
    ) == (True, True, True)

    assert verify_private_runtime._changetime_edit_predicates(
        before_size=28,
        after_size=28,
        expected_size=28,
        before_mtime_ns=before,
        after_mtime_ns=before + 100,
        before_change=1000,
        after_change=1100,
    ) == (True, False, True)


def test_native_clock_gate_requires_observed_filetime_advance(monkeypatch):
    observed = iter((100, 100, 101))
    elapsed = iter((0, 0, 0))
    monkeypatch.setattr(verify_private_runtime, "system_time_100ns", lambda: next(observed))
    monkeypatch.setattr(verify_private_runtime.time, "monotonic_ns", lambda: next(elapsed))
    monkeypatch.setattr(verify_private_runtime.time, "sleep", lambda _seconds: None)

    assert verify_private_runtime._wait_for_native_clock_advance(100, timeout_ns=10)


def test_native_clock_gate_times_out_without_clock_advance(monkeypatch):
    observed = iter((100, 100))
    elapsed = iter((0, 10))
    monkeypatch.setattr(verify_private_runtime, "system_time_100ns", lambda: next(observed))
    monkeypatch.setattr(verify_private_runtime.time, "monotonic_ns", lambda: next(elapsed))
    monkeypatch.setattr(verify_private_runtime.time, "sleep", lambda _seconds: None)

    assert not verify_private_runtime._wait_for_native_clock_advance(100, timeout_ns=10)


def test_windows_changetime_probe_closes_writer_before_observing_metadata(monkeypatch):
    created = []
    closed = []
    native_mkstemp = verify_private_runtime.tempfile.mkstemp

    def mkstemp(**kwargs):
        descriptor, filename = native_mkstemp(**kwargs)
        created.append(descriptor)
        return descriptor, filename

    def close(descriptor):
        closed.append(descriptor)
        os.close(descriptor)

    def native_change_time(_path, _metadata):
        assert created[0] in closed, "Windows defers timestamps while the writer remains open"
        native_change_time.calls += 1
        return native_change_time.calls * 1000

    native_change_time.calls = 0
    monkeypatch.setattr(verify_private_runtime.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(verify_private_runtime, "os", SimpleNamespace(
        name="nt", write=os.write, close=close, utime=os.utime,
    ))
    monkeypatch.setattr(verify_private_runtime, "_verify_windows_acl", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_private_runtime, "change_time_ns", native_change_time)
    native_clock = iter((10, 11))
    monkeypatch.setattr(verify_private_runtime, "system_time_100ns", lambda: next(native_clock))

    result = verify_private_runtime.run_probe()

    assert result["status"] == "passed"
    assert "same-size-edit-restored-mtime-changes-native-changetime" in result["checks"]
    assert closed.count(created[0]) == 1


def test_changetime_probe_keeps_size_and_native_change_predicates_separate():
    assert verify_private_runtime._changetime_edit_predicates(
        before_size=28,
        after_size=27,
        expected_size=28,
        before_mtime_ns=1000,
        after_mtime_ns=1000,
        before_change=1000,
        after_change=1100,
    ) == (False, True, True)
    assert verify_private_runtime._changetime_edit_predicates(
        before_size=28,
        after_size=28,
        expected_size=28,
        before_mtime_ns=1000,
        after_mtime_ns=1000,
        before_change=1000,
        after_change=1000,
    ) == (True, True, False)


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


def test_probe_keeps_file_protection_failure_stage_through_descriptor_close(monkeypatch):
    def fail(_descriptor, _path, _mode):
        raise private_paths.PrivatePathError(
            "private path security could not be applied", winerror=87
        )

    monkeypatch.setattr(private_paths, "secure_private_file", fail)

    with pytest.raises(verify_private_runtime._ProbeFailure) as failure:
        verify_private_runtime.run_probe()

    assert failure.value.failed_check == (
        "protect-private-file-handoff:private_path_security_could_not_be_applied"
    )
    assert failure.value.winerror == 87
    assert failure.value.checks_completed == ["private-directory-owner-and-access"]
