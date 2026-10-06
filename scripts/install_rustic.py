"""Install the pinned rustic binary into a caller-owned private runtime root.

This is intentionally invoked on demand by the managed controller. It does not
run during Josh Room or VS Code startup and never installs a host package.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from josh_room.tls import system_ssl_context

VERSION = "0.11.4"
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
        raise InstallError("managed rustic destination is not a regular file")
    return True


def _ensure_runtime_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError(
            "managed rustic runtime directory could not be inspected"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InstallError("managed rustic runtime directory is not a real directory")


def _manifest_pin(manifest_path: Path, platform: str) -> tuple[dict, dict]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError("rustic runtime manifest could not be read") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise InstallError("rustic runtime manifest schema is unsupported")
    if manifest.get("version") != VERSION or manifest.get("repository_format") != 2:
        raise InstallError("rustic runtime manifest pin is unsupported")
    if platform not in PLATFORMS:
        raise InstallError("rustic runtime platform is unsupported")
    pin = manifest.get("platforms", {}).get(platform)
    if not isinstance(pin, dict):
        raise InstallError("rustic runtime platform pin is missing")
    expected_binary = "rustic.exe" if platform == "win32-x64" else "rustic"
    target = (
        "x86_64-pc-windows-msvc"
        if platform == "win32-x64"
        else "x86_64-unknown-linux-gnu"
    )
    expected_asset = f"rustic-v{VERSION}-{target}.tar.gz"
    expected_ext = ".tar.gz"
    url = pin.get("url")
    if (
        pin.get("asset") != expected_asset
        or pin.get("binary") != expected_binary
        or not isinstance(url, str)
        or url
        != f"https://github.com/rustic-rs/rustic/releases/download/v{VERSION}/{expected_asset}"
        or not url.endswith(expected_ext)
        or not isinstance(pin.get("size"), int)
        or pin["size"] <= 0
        or not isinstance(pin.get("sha256"), str)
        or not DIGEST.fullmatch(pin["sha256"])
    ):
        raise InstallError("rustic runtime platform pin is invalid")
    return manifest, pin


def _download(url: str, destination: Path, expected_size: int) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "josh-room-managed-runtime"}
    )
    try:
        response = urllib.request.urlopen(
            request, timeout=60, context=system_ssl_context()
        )
    except urllib.error.HTTPError as exc:
        raise InstallError(
            "official rustic release download failed",
            diagnostic={
                "boundary": "http",
                "exception": type(exc).__name__,
                "http_status": exc.code,
            },
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
        raise InstallError(
            "official rustic release download failed", diagnostic=diagnostic
        ) from exc
    except ssl.SSLError as exc:
        diagnostic = {"boundary": "tls", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["tls_errno"] = exc.errno
        raise InstallError(
            "official rustic release download failed", diagnostic=diagnostic
        ) from exc
    except OSError as exc:
        diagnostic = {"boundary": "transport", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["errno"] = exc.errno
        raise InstallError(
            "official rustic release download failed", diagnostic=diagnostic
        ) from exc

    try:
        with response, destination.open("xb") as output:
            if not response.geturl().startswith("https://"):
                raise InstallError("rustic download did not use HTTPS")
            total = 0
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > expected_size:
                    raise InstallError("rustic release archive exceeds its pinned size")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except InstallError:
        raise
    except OSError as exc:
        diagnostic = {"boundary": "local-io", "exception": type(exc).__name__}
        if isinstance(exc.errno, int):
            diagnostic["errno"] = exc.errno
        raise InstallError(
            "rustic archive could not be written", diagnostic=diagnostic
        ) from exc
    if destination.stat().st_size != expected_size:
        raise InstallError("rustic release archive size mismatch")


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
                raise InstallError("rustic binary exceeds the extraction size limit")
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if total == 0:
        raise InstallError("rustic archive contains an empty binary")


def _fsync_directory(directory: Path, platform: str) -> None:
    if platform == "win32-x64":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _extract_archive(archive: Path, destination: Path, platform: str) -> None:
    expected_member = "rustic.exe" if platform == "win32-x64" else "rustic"
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            matches = []
            for count, member in enumerate(bundle, start=1):
                path = PurePosixPath(member.name)
                if (
                    count > 4096
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or ":" in member.name
                    or not (member.isfile() or member.isdir())
                ):
                    raise InstallError("rustic archive contains an unsafe member")
                if member.name == expected_member:
                    matches.append(member)
            if len(matches) != 1 or not 0 < matches[0].size <= MAX_BINARY_SIZE:
                raise InstallError("rustic archive has no unique expected binary")
            source = bundle.extractfile(matches[0])
            if source is None:
                raise InstallError("rustic archive binary is unreadable")
            with source:
                _copy_bounded(source, destination)
    except InstallError:
        raise
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise InstallError("rustic tar archive is invalid") from exc


def _verify_version(binary: Path) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix=".rustic-version-") as name:
            profile = Path(name) / "profile.toml"
            profile.write_text("[global]\n", encoding="utf-8")
            environment = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
                if key in os.environ
            }
            environment.update(
                RUSTIC_USE_PROFILE=str(profile),
                HOME=name,
                USERPROFILE=name,
                APPDATA=name,
                XDG_CONFIG_HOME=name,
                LC_ALL="C",
                TZ="UTC",
                RUSTIC_NO_PROGRESS="true",
                RUSTIC_LOG_LEVEL="error",
            )
            result = subprocess.run(
                [str(binary.resolve()), "version"],
                cwd=name,
                env=environment,
                stdin=subprocess.DEVNULL,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError("managed rustic version check could not run") from exc
    lines = result.stdout.splitlines()
    first_line = lines[0].split() if lines else []
    if result.returncode or len(lines) != 1 or first_line != ["rustic", "v" + VERSION]:
        raise InstallError("managed rustic binary is not version 0.11.4")


def install_rustic(
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
        "win32-x64"
        if os.name == "nt" and sys.maxsize > 2**32
        else "linux-x64"
        if sys.platform.startswith("linux") and sys.maxsize > 2**32
        else None
    )
    if verify_version is None and platform != host_platform:
        raise InstallError(
            "rustic version can only be probed on the current supported host"
        )
    check_version = verify_version or _verify_version
    version_root = runtime_root / "rustic" / VERSION / platform
    _ensure_runtime_directory(runtime_root)
    _ensure_runtime_directory(runtime_root / "rustic")
    _ensure_runtime_directory(runtime_root / "rustic" / VERSION)
    _ensure_runtime_directory(version_root)
    executable = version_root / pin["binary"]
    digest_marker = version_root / (pin["binary"] + ".sha256")
    if _regular_file(executable):
        if not _regular_file(digest_marker):
            raise InstallError("managed rustic cache is missing its verified digest")
        try:
            expected_binary_digest = digest_marker.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise InstallError("managed rustic cache digest could not be read") from exc
        if (
            not DIGEST.fullmatch(expected_binary_digest)
            or _sha256_file(executable) != expected_binary_digest
        ):
            raise InstallError("managed rustic cached binary checksum mismatch")
        check_version(executable)
        return {
            "executable": str(executable),
            "version": VERSION,
            "platform": platform,
            "cached": True,
        }
    if executable.exists() or executable.is_symlink():
        raise InstallError(
            "managed rustic destination already exists with an unsafe type"
        )

    with tempfile.TemporaryDirectory(
        prefix=".rustic-install-", dir=version_root
    ) as temporary_name:
        temporary_directory = Path(temporary_name)
        archive = temporary_directory / pin["asset"]
        extracted = temporary_directory / pin["binary"]
        download(pin["url"], archive, pin["size"])
        if _sha256_file(archive) != pin["sha256"]:
            raise InstallError("official rustic release archive checksum mismatch")
        _extract_archive(archive, extracted, platform)
        if platform == "linux-x64":
            extracted.chmod(0o700)
        check_version(extracted)
        binary_digest = _sha256_file(extracted)
        marker_temporary = temporary_directory / (pin["binary"] + ".sha256")
        with marker_temporary.open("xb") as marker_file:
            marker_file.write((binary_digest + "\n").encode("ascii"))
            marker_file.flush()
            os.fsync(marker_file.fileno())
        if os.name != "nt":
            marker_temporary.chmod(0o600)
        os.replace(marker_temporary, digest_marker)
        os.replace(extracted, executable)
        _fsync_directory(version_root, platform)
    return {
        "executable": str(executable),
        "version": VERSION,
        "platform": platform,
        "cached": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    args = parser.parse_args()
    try:
        result = install_rustic(args.manifest, args.destination, args.platform)
    except InstallError as exc:
        result: dict[str, object] = {"error": str(exc)}
        if exc.diagnostic:
            result["diagnostic"] = exc.diagnostic
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        return 1
    except OSError as exc:
        diagnostic: dict[str, int | str] = {
            "boundary": "local-io",
            "exception": type(exc).__name__,
        }
        if isinstance(exc.errno, int):
            diagnostic["errno"] = exc.errno
        print(
            json.dumps(
                {
                    "error": "rustic runtime installation failed",
                    "diagnostic": diagnostic,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
