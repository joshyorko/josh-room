"""Managed Rustic pins, unsafe archive handling, cache and shipped parity."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from josh_room.room_store_bridge import (
    RoomStoreBridgeError,
    _store_engine,
    _verified_restic_executable,
)
from scripts import install_rustic as installer

ROOT = Path(__file__).resolve().parents[1]


def archive_bytes(name="rustic", *, link=False, duplicate=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        content = b'#!/bin/sh\nprintf "rustic v0.11.4\\n"\n'
        info = tarfile.TarInfo(name)
        info.size = len(content)
        if link:
            info.type = tarfile.SYMTYPE
            info.linkname = "outside"
            info.size = 0
        bundle.addfile(info, None if link else io.BytesIO(content))
        if duplicate:
            bundle.addfile(info, io.BytesIO(content))
    return output.getvalue()


def manifest_fixture(tmp_path, archive, platform="linux-x64"):
    data = json.loads(
        (ROOT / "vscode-extension/runtime/rustic-manifest.json").read_text()
    )
    data["platforms"][platform]["sha256"] = hashlib.sha256(archive).hexdigest()
    data["platforms"][platform]["size"] = len(archive)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    return path


def download_fixture(archive):
    def download(_url, destination, expected_size):
        assert expected_size == len(archive)
        destination.write_bytes(archive)

    return download


@pytest.mark.parametrize(
    "platform,binary", [("linux-x64", "rustic"), ("win32-x64", "rustic.exe")]
)
def test_install_and_checksum_verified_cache(tmp_path, platform, binary):
    archive = archive_bytes(binary)
    manifest = manifest_fixture(tmp_path, archive, platform)
    kwargs = {
        "download": download_fixture(archive),
        "verify_version": lambda _binary: None,
    }
    result = installer.install_rustic(
        manifest, tmp_path / "runtime", platform, **kwargs
    )
    assert not result["cached"] and result["version"] == "0.11.4"
    cached = installer.install_rustic(
        manifest,
        tmp_path / "runtime",
        platform,
        download=lambda *_: pytest.fail("downloaded cache"),
        verify_version=lambda _: None,
    )
    assert cached["cached"]
    Path(result["executable"]).write_bytes(b"tampered")
    with pytest.raises(installer.InstallError, match="checksum mismatch"):
        installer.install_rustic(manifest, tmp_path / "runtime", platform, **kwargs)


@pytest.mark.parametrize(
    "archive",
    [
        archive_bytes("../rustic"),
        archive_bytes("/rustic"),
        archive_bytes("rustic", link=True),
        archive_bytes("rustic", duplicate=True),
        archive_bytes("other"),
    ],
)
def test_unsafe_archive_never_promotes(tmp_path, archive):
    manifest = manifest_fixture(tmp_path, archive)
    with pytest.raises(installer.InstallError):
        installer.install_rustic(
            manifest,
            tmp_path / "runtime",
            "linux-x64",
            download=download_fixture(archive),
            verify_version=lambda _: None,
        )
    assert not list((tmp_path / "runtime").rglob("rustic.sha256"))


def test_wrong_version_and_hash_never_promote(tmp_path):
    archive = archive_bytes()
    manifest = manifest_fixture(tmp_path, archive)

    def fail(_binary):
        raise installer.InstallError("synthetic version mismatch")

    with pytest.raises(installer.InstallError):
        installer.install_rustic(
            manifest,
            tmp_path / "runtime",
            "linux-x64",
            download=download_fixture(archive),
            verify_version=fail,
        )
    data = json.loads(manifest.read_text())
    data["platforms"]["linux-x64"]["sha256"] = "a" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(installer.InstallError, match="checksum"):
        installer.install_rustic(
            manifest,
            tmp_path / "runtime",
            "linux-x64",
            download=download_fixture(archive),
            verify_version=lambda _: None,
        )
    assert not list((tmp_path / "runtime").rglob("rustic.sha256"))


def test_official_exact_pins_and_shipped_parity():
    canonical = ROOT / "vscode-extension/runtime/rustic-manifest.json"
    data = json.loads(canonical.read_text())
    assert data["version"] == "0.11.4"
    assert (
        data["platforms"]["linux-x64"]["sha256"]
        == "c20bc3682c6275de3cfbc9317101dca5c0800a2fb5c610d0f9ef2addfd27cf81"
    )
    assert (
        data["platforms"]["win32-x64"]["sha256"]
        == "aa3586c2646da30965e099393c739a32526c27314cfaaf97af7f65aab1ec230a"
    )
    for prefix in (
        "vscode-extension/runtime",
        "templates/room/vscode-extension/runtime",
    ):
        assert (
            ROOT / prefix / "rustic-manifest.json"
        ).read_bytes() == canonical.read_bytes()
        assert (ROOT / prefix / "controller/install_rustic.py").read_bytes() == (
            ROOT / "scripts/install_rustic.py"
        ).read_bytes()
        for module in ("rustic_store.py", "restic_store.py", "room_store_bridge.py"):
            assert (ROOT / prefix / "controller/josh_room" / module).read_bytes() == (
                ROOT / "src/josh_room" / module
            ).read_bytes()


def test_unknown_engine_and_handoff_cannot_use_arbitrary_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_STORE_ENGINE", "other")
    with pytest.raises(RoomStoreBridgeError):
        _store_engine()
    monkeypatch.setenv("JOSH_ROOM_STORE_ENGINE", "rustic")
    monkeypatch.setenv("JOSH_ROOM_RUSTIC_EXE", str(tmp_path / "unverified-rustic"))
    with pytest.raises(RoomStoreBridgeError) as error:
        _verified_restic_executable(runtime_root=tmp_path / "runtime", install=True)
    assert error.value.code == "rustic-runtime-invalid"


def test_version_probe_never_inherits_host_profile_or_authority(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("RUSTIC_USE_PROFILE", "synthetic-host-profile")
    monkeypatch.setenv("RUSTIC_PASSWORD_COMMAND", "synthetic-host-command")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-host-authority")

    def run(argv, **kwargs):
        assert argv == [str((tmp_path / "rustic").resolve()), "version"]
        assert kwargs["env"]["RUSTIC_USE_PROFILE"] != "synthetic-host-profile"
        assert Path(kwargs["env"]["RUSTIC_USE_PROFILE"]).read_text() == "[global]\n"
        assert (
            "RUSTIC_PASSWORD_COMMAND" not in kwargs["env"]
            and "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
        )
        assert kwargs["cwd"] == kwargs["env"]["HOME"]
        return SimpleNamespace(returncode=0, stdout="rustic v0.11.4\n")

    monkeypatch.setattr(installer.subprocess, "run", run)
    installer._verify_version(tmp_path / "rustic")
