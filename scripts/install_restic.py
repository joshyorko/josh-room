"""Install the pinned restic binary into a caller-owned private runtime root.

This is intentionally invoked on demand by the managed controller. It does not
run during Josh Room or VS Code startup and never installs a host package.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from josh_room.tls import system_ssl_context

VERSION = "0.19.1"
MAX_BINARY_SIZE = 128 * 1024 * 1024
PLATFORMS = {"linux-x64", "win32-x64"}
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class InstallError(RuntimeError):
    """A bounded, user-safe installer failure."""

    def __init__(self, message: str, *, diagnostic: dict[str, int | str] | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic


def _regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("managed restic destination is not a regular file")
    return True


def _ensure_runtime_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError("managed restic runtime directory could not be inspected") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError("managed restic runtime directory is not a real directory")


def _manifest_pin(manifest_path: Path, platform: str) -> tuple[dict, dict]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("restic runtime manifest could not be read") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise InstallError("restic runtime manifest schema is unsupported")
    if manifest.get("version") != VERSION or manifest.get("repository_format") != 2:
        raise InstallError("restic runtime manifest pin is unsupported")
    if platform not in PLATFORMS:
        raise InstallError("restic runtime platform is unsupported")
    pin = manifest.get("platforms", {}).get(platform)
    if not isinstance(pin, dict):
        raise InstallError("restic runtime platform pin is missing")
    expected_binary = "restic.exe" if platform == "win32-x64" else "restic"
    expected_asset = (
        "restic_0.19.1_windows_amd64.zip"
        if platform == "win32-x64"
        else "restic_0.19.1_linux_amd64.bz2"
    )
    expected_ext = ".zip" if platform == "win32-x64" else ".bz2"
    url = pin.get("url")
    if (
        pin.get("asset") != expected_asset
        or pin.get("binary") != expected_binary
        or not isinstance(url, str)
        or url != f"https://github.com/restic/restic/releases/download/v{VERSION}/{expected_asset}"
        or not url.endswith(expected_ext)
        or not isinstance(pin.get("size"), int)
        or pin["size"] <= 0
        or not isinstance(pin.get("sha256"), str)
        or not DIGEST.fullmatch(pin["sha256"])
    ):
        raise InstallError("restic runtime platform pin is invalid")
    return manifest, pin


def _download(url: str, destination: Path, expected_size: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "josh-room-managed-runtime"})
    try:
        response = urllib.request.urlopen(request, timeout=60, context=system_ssl_context())
    except urllib.error.HTTPError as exc:
        raise InstallError(
            "official restic release download failed",
            diagnostic={"boundary": "http", "exception": type(exc).__name__, "http_status": exc.code},
        ) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            diagnostic: dict[str, int | str] = {
                "boundary": "tls",
                "exception": type(reason).__name__,
            }
            if isinstance(reason.errno, int):
                diagnostic["tls_errno"] = reason.errno
        elif isinstance(reason, OSError):
            diagnostic = {"boundary": "transport", "exception": type(reason).__name__}
            if isinstance(reason.errno, int):
                diagnostic["errno"] = reason.errno
        else:
            diagnostic = {"boundary": "transport", "exception": type(reason).__name__}
        raise InstallError("official restic release download failed", diagnostic=diagnostic) from exc
    except ssl.SSLError as exc:
        diagnostic = {"boundary": "tls", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["tls_errno"] = exc.errno
        raise InstallError("official restic release download failed", diagnostic=diagnostic) from exc
    except OSError as exc:
        diagnostic = {"boundary": "transport", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["errno"] = exc.errno
        raise InstallError("official restic release download failed", diagnostic=diagnostic) from exc

    try:
        with response, destination.open("xb") as output:
            if not response.geturl().startswith("https://"):
                raise InstallError("restic download did not use HTTPS")
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > expected_size:
                    raise InstallError("restic release archive exceeds its pinned size")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except InstallError:
        raise
    except OSError as exc:
        diagnostic = {"boundary": "local-io", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["errno"] = exc.errno
        raise InstallError("restic archive could not be written", diagnostic=diagnostic) from exc
    if destination.stat().st_size != expected_size:
        raise InstallError("restic release archive size mismatch")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_bounded(source: BinaryIO, destination: Path) -> None:
    total = 0
    with destination.open("xb") as output:
        while block := source.read(1024 * 1024):
            total += len(block)
            if total > MAX_BINARY_SIZE:
                raise InstallError("restic binary exceeds the extraction size limit")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if total == 0:
        raise InstallError("restic archive contains an empty binary")


def _safe_zip_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    member_path = PurePosixPath(name)
    if member_path.is_absolute() or ".." in member_path.parts or "\\" in name or ":" in name:
        raise InstallError("restic archive contains an unsafe path")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise InstallError("restic archive contains a symbolic link")
    if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise InstallError("restic archive contains an unsupported entry type")
    return not info.is_dir()


def _extract_archive(archive: Path, destination: Path, platform: str) -> None:
    if platform == "linux-x64":
        try:
            with bz2.open(archive, "rb") as source:
                _copy_bounded(source, destination)
        except (OSError, EOFError) as exc:
            raise InstallError("restic bzip2 archive is invalid") from exc
        return

    expected_member = "restic_0.19.1_windows_amd64.exe"
    try:
        with zipfile.ZipFile(archive) as bundle:
            files = [info for info in bundle.infolist() if _safe_zip_member(info)]
            matches = [info for info in files if info.filename == expected_member]
            if len(matches) != 1 or matches[0].file_size > MAX_BINARY_SIZE:
                raise InstallError("restic Windows archive has no unique expected binary")
            with bundle.open(matches[0], "r") as source:
                _copy_bounded(source, destination)
    except InstallError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise InstallError("restic Windows zip archive is invalid") from exc


def _verify_version(binary: Path) -> None:
    try:
        result = subprocess.run(
            [str(binary), "version"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("managed restic version check could not run") from exc
    lines = result.stdout.splitlines()
    first_line = lines[0].split() if lines else []
    if result.returncode or len(first_line) < 2 or first_line[:2] != ["restic", VERSION]:
        raise InstallError("managed restic binary is not version 0.19.1")


def install_restic(
    manifest_path: Path,
    runtime_root: Path,
    platform: str,
    *,
    download: Callable[[str, Path, int], None] = _download,
    verify_version: Callable[[Path], None] | None = None,
) -> dict[str, str | bool]:
    """Install and return the versioned binary path; runtime_root is caller-owned."""
    _manifest, pin = _manifest_pin(manifest_path, platform)
    host_platform = (
        "win32-x64" if os.name == "nt" and sys.maxsize > 2**32
        else "linux-x64" if sys.platform.startswith("linux") and sys.maxsize > 2**32
        else None
    )
    if verify_version is None and platform != host_platform:
        raise InstallError("restic version can only be probed on the current supported host")
    check_version = verify_version or _verify_version
    version_root = runtime_root / "restic" / VERSION / platform
    _ensure_runtime_directory(runtime_root)
    _ensure_runtime_directory(runtime_root / "restic")
    _ensure_runtime_directory(runtime_root / "restic" / VERSION)
    _ensure_runtime_directory(version_root)
    executable = version_root / pin["binary"]
    digest_marker = version_root / (pin["binary"] + ".sha256")
    if _regular_file(executable):
        if not _regular_file(digest_marker):
            raise InstallError("managed restic cache is missing its verified digest")
        try:
            expected_binary_digest = digest_marker.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise InstallError("managed restic cache digest could not be read") from exc
        if not DIGEST.fullmatch(expected_binary_digest) or _sha256_file(executable) != expected_binary_digest:
            raise InstallError("managed restic cached binary checksum mismatch")
        check_version(executable)
        return {"executable": str(executable), "version": VERSION, "platform": platform, "cached": True}
    if executable.exists() or executable.is_symlink():
        raise InstallError("managed restic destination already exists with an unsafe type")

    with tempfile.TemporaryDirectory(prefix=".restic-install-", dir=version_root) as temporary_name:
        temporary_directory = Path(temporary_name)
        archive = temporary_directory / pin["asset"]
        extracted = temporary_directory / pin["binary"]
        download(pin["url"], archive, pin["size"])
        if _sha256_file(archive) != pin["sha256"]:
            raise InstallError("official restic release archive checksum mismatch")
        _extract_archive(archive, extracted, platform)
        if platform == "linux-x64":
            extracted.chmod(0o700)
        check_version(extracted)
        binary_digest = _sha256_file(extracted)
        marker_temporary = temporary_directory / (pin["binary"] + ".sha256")
        marker_temporary.write_text(binary_digest + "\n", encoding="ascii")
        marker_temporary.chmod(0o600)
        with marker_temporary.open("rb") as marker_file:
            os.fsync(marker_file.fileno())
        os.replace(marker_temporary, digest_marker)
        os.replace(extracted, executable)
    return {"executable": str(executable), "version": VERSION, "platform": platform, "cached": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    args = parser.parse_args()
    try:
        result = install_restic(args.manifest, args.destination, args.platform)
    except InstallError as exc:
        result: dict[str, object] = {"error": str(exc)}
        if exc.diagnostic:
            result["diagnostic"] = exc.diagnostic
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
