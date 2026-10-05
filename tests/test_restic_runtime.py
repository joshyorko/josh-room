from __future__ import annotations

import bz2
import errno
import hashlib
import json
import os
import ssl
import stat
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import scripts.install_restic as installer
from scripts.install_restic import InstallError, install_restic

LINUX_ASSET = "restic_0.19.1_linux_amd64.bz2"
WINDOWS_ASSET = "restic_0.19.1_windows_amd64.zip"
LINUX_URL = f"https://github.com/restic/restic/releases/download/v0.19.1/{LINUX_ASSET}"
WINDOWS_URL = f"https://github.com/restic/restic/releases/download/v0.19.1/{WINDOWS_ASSET}"


def _manifest(tmp_path: Path, platform: str, archive: bytes, *, expected_hash: str | None = None) -> Path:
    if platform == "linux-x64":
        asset, url, binary = LINUX_ASSET, LINUX_URL, "restic"
    else:
        asset, url, binary = WINDOWS_ASSET, WINDOWS_URL, "restic.exe"
    data = {
        "schema_version": 1,
        "version": "0.19.1",
        "repository_format": 2,
        "platforms": {
            platform: {
                "asset": asset,
                "url": url,
                "sha256": expected_hash or hashlib.sha256(archive).hexdigest(),
                "size": len(archive),
                "binary": binary,
            }
        },
    }
    path = tmp_path / "restic-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _script(version: str = "0.19.1") -> bytes:
    return f"#!/bin/sh\nprintf 'restic {version}\n'\n".encode()


def _download_fixture(archive: bytes):
    def download(_url: str, destination: Path, expected_size: int) -> None:
        assert len(archive) == expected_size
        destination.write_bytes(archive)

    return download


def test_download_uses_scoped_system_tls_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = object()
    calls: list[dict] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self) -> str:
            return LINUX_URL

        def read(self, _size: int = -1) -> bytes:
            if getattr(self, "read_once", False):
                return b""
            self.read_once = True
            return b"verified bytes"

    def urlopen(request, **kwargs):
        calls.append({"request": request, **kwargs})
        return Response()

    monkeypatch.setattr(installer, "system_ssl_context", lambda: context)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    destination = tmp_path / "archive"
    installer._download(LINUX_URL, destination, len(b"verified bytes"))

    assert calls[0]["context"] is context
    assert calls[0]["timeout"] == 60
    assert destination.read_bytes() == b"verified bytes"


def test_download_tls_failure_has_bounded_tls_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = ssl.SSLCertVerificationError(1, "synthetic private certificate detail")
    error = urllib.error.URLError(reason)
    monkeypatch.setattr(installer, "system_ssl_context", ssl.create_default_context)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(InstallError) as raised:
        installer._download(LINUX_URL, tmp_path / "archive", 1)

    assert str(raised.value) == "official restic release download failed"
    assert raised.value.diagnostic == {
        "boundary": "tls",
        "exception": "SSLCertVerificationError",
        "tls_errno": 1,
    }
    assert "synthetic private certificate detail" not in repr(raised.value.diagnostic)


def test_download_http_failure_reports_status_without_response_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(LINUX_URL, 503, "synthetic private response text", {}, None)
    monkeypatch.setattr(installer, "system_ssl_context", ssl.create_default_context)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(InstallError) as raised:
        installer._download(LINUX_URL, tmp_path / "archive", 1)

    assert str(raised.value) == "official restic release download failed"
    assert raised.value.diagnostic == {
        "boundary": "http",
        "exception": "HTTPError",
        "http_status": 503,
    }
    assert "synthetic private response text" not in repr(raised.value.diagnostic)


def test_linux_install_verifies_archive_version_and_reuses_verified_cache(tmp_path: Path) -> None:
    archive = bz2.compress(_script())
    manifest = _manifest(tmp_path, "linux-x64", archive)
    root = tmp_path / "owned-runtime"
    first = install_restic(manifest, root, "linux-x64", download=_download_fixture(archive))
    executable = Path(first["executable"])
    assert first["cached"] is False
    assert executable.read_bytes() == _script()
    assert stat.S_IMODE(executable.stat().st_mode) == 0o700

    second = install_restic(manifest, root, "linux-x64", download=lambda *_: pytest.fail("downloaded cache"))
    assert second["cached"] is True
    executable.write_bytes(_script() + b"# changed\n")
    with pytest.raises(InstallError, match="cached binary checksum mismatch"):
        install_restic(manifest, root, "linux-x64", download=lambda *_: pytest.fail("downloaded cache"))


def test_wrong_archive_hash_does_not_promote_or_replace_binary(tmp_path: Path) -> None:
    archive = bz2.compress(_script())
    manifest = _manifest(tmp_path, "linux-x64", archive, expected_hash="0" * 64)
    root = tmp_path / "owned-runtime"
    with pytest.raises(InstallError, match="archive checksum mismatch"):
        install_restic(manifest, root, "linux-x64", download=_download_fixture(archive))
    assert not (root / "restic" / "0.19.1" / "linux-x64" / "restic").exists()


def test_wrong_version_does_not_promote_binary(tmp_path: Path) -> None:
    archive = bz2.compress(_script("0.19.0"))
    manifest = _manifest(tmp_path, "linux-x64", archive)
    root = tmp_path / "owned-runtime"
    with pytest.raises(InstallError, match="not version 0.19.1"):
        install_restic(manifest, root, "linux-x64", download=_download_fixture(archive))
    assert not (root / "restic" / "0.19.1" / "linux-x64" / "restic").exists()


def test_truncated_bzip_archive_is_rejected_atomically(tmp_path: Path) -> None:
    archive = bz2.compress(_script())[:-8]
    manifest = _manifest(tmp_path, "linux-x64", archive)
    root = tmp_path / "owned-runtime"
    with pytest.raises(InstallError):
        install_restic(manifest, root, "linux-x64", download=_download_fixture(archive))
    assert not (root / "restic" / "0.19.1" / "linux-x64" / "restic").exists()


def test_windows_zip_extracts_only_expected_regular_binary(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr("restic_0.19.1_windows_amd64.exe", b"fixture executable")
        bundle.writestr("README", "ignored")
    archive = archive_path.read_bytes()
    manifest = _manifest(tmp_path, "win32-x64", archive)
    root = tmp_path / "owned-runtime"
    result = install_restic(
        manifest,
        root,
        "win32-x64",
        download=_download_fixture(archive),
        verify_version=lambda binary: None if binary.read_bytes() == b"fixture executable" else pytest.fail("bad binary"),
    )
    assert Path(result["executable"]).read_bytes() == b"fixture executable"


def test_windows_install_fsyncs_files_but_skips_unsupported_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr("restic_0.19.1_windows_amd64.exe", b"fixture executable")
    archive = archive_path.read_bytes()
    manifest = _manifest(tmp_path, "win32-x64", archive)
    real_fsync = installer.os.fsync
    file_fsyncs: list[int] = []

    def fsync_file_only(descriptor: int) -> None:
        assert stat.S_ISREG(installer.os.fstat(descriptor).st_mode), "Windows must not fsync a directory"
        file_fsyncs.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(installer.os, "fsync", fsync_file_only)
    result = install_restic(
        manifest,
        tmp_path / "owned-runtime",
        "win32-x64",
        download=_download_fixture(archive),
        verify_version=lambda _binary: None,
    )

    assert file_fsyncs
    assert Path(result["executable"]).read_bytes() == b"fixture executable"


def test_digest_marker_is_fsynced_through_writable_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fcntl = pytest.importorskip("fcntl")
    archive = bz2.compress(_script())
    manifest = _manifest(tmp_path, "linux-x64", archive)
    real_fsync = installer.os.fsync
    access_modes: list[tuple[bool, int]] = []

    def require_writable(descriptor: int) -> None:
        is_directory = stat.S_ISDIR(installer.os.fstat(descriptor).st_mode)
        access_mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        access_modes.append((is_directory, access_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(installer.os, "fsync", require_writable)
    install_restic(manifest, tmp_path / "owned-runtime", "linux-x64", download=_download_fixture(archive))

    assert access_modes
    assert all(mode != os.O_RDONLY for is_directory, mode in access_modes if not is_directory)
    assert any(is_directory for is_directory, _mode in access_modes)


def test_posix_install_retains_directory_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = installer.os.fsync
    synced_directories: list[bool] = []

    def record_fsync(descriptor: int) -> None:
        synced_directories.append(stat.S_ISDIR(installer.os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(installer.os, "fsync", record_fsync)
    installer._fsync_directory(tmp_path, "linux-x64")

    assert synced_directories == [True]


def test_main_reports_unexpected_oserror_without_traceback_or_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private_path = str(tmp_path / "private-managed-runtime")

    def fail_install(*_args) -> None:
        raise OSError(errno.EBADF, "Bad file descriptor", private_path)

    monkeypatch.setattr(installer, "install_restic", fail_install)
    monkeypatch.setattr(
        "sys.argv",
        ["install_restic.py", "--manifest", "manifest.json", "--destination", private_path, "--platform", "linux-x64"],
    )

    assert installer.main() == 1
    stderr = capsys.readouterr().err
    assert '"boundary": "local-io"' in stderr
    assert '"errno": 9' in stderr
    assert private_path not in stderr
    assert "Traceback" not in stderr


def test_windows_zip_symlink_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.zip"
    info = zipfile.ZipInfo("restic_0.19.1_windows_amd64.exe")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr(info, "target")
    archive = archive_path.read_bytes()
    manifest = _manifest(tmp_path, "win32-x64", archive)
    root = tmp_path / "owned-runtime"
    with pytest.raises(InstallError, match="symbolic link"):
        install_restic(
            manifest,
            root,
            "win32-x64",
            download=_download_fixture(archive),
            verify_version=lambda _binary: None,
        )
    assert not (root / "restic" / "0.19.1" / "win32-x64" / "restic.exe").exists()


@pytest.mark.parametrize("member", ["../restic_0.19.1_windows_amd64.exe", "C:/restic_0.19.1_windows_amd64.exe"])
def test_windows_zip_traversal_is_rejected(tmp_path: Path, member: str) -> None:
    archive_path = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive_path, "w") as bundle:
        bundle.writestr(member, b"fixture executable")
    archive = archive_path.read_bytes()
    manifest = _manifest(tmp_path, "win32-x64", archive)
    root = tmp_path / "owned-runtime"
    with pytest.raises(InstallError, match="unsafe path"):
        install_restic(
            manifest,
            root,
            "win32-x64",
            download=_download_fixture(archive),
            verify_version=lambda _binary: None,
        )
    assert not (root / "restic" / "0.19.1" / "win32-x64" / "restic.exe").exists()


def test_symlink_cache_is_not_followed_or_replaced(tmp_path: Path) -> None:
    archive = bz2.compress(_script())
    manifest = _manifest(tmp_path, "linux-x64", archive)
    root = tmp_path / "owned-runtime"
    destination = root / "restic" / "0.19.1" / "linux-x64" / "restic"
    destination.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(_script())
    try:
        destination.symlink_to(elsewhere)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(InstallError, match="not a regular file"):
        install_restic(manifest, root, "linux-x64", download=_download_fixture(archive))
    assert elsewhere.read_bytes() == _script()
