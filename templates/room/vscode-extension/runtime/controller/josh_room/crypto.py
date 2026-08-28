import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_DECRYPTED_SIZE = 8 * 1024 * 1024 * 1024 + 2 * 1024 * 1024


class CryptoError(RuntimeError):
    pass


def _runtime_prefixes(platform: str) -> list[Path]:
    executable = Path(os.path.abspath(sys.executable))
    parent = executable.parent
    inferred = parent.parent if parent.name.lower() in {"bin", "scripts"} else parent
    values = [inferred]
    declared = os.environ.get("CONDA_PREFIX")
    if declared:
        values.append(Path(declared))
    unique = []
    seen = set()
    for value in values:
        key = os.path.normcase(os.path.abspath(value))
        if key not in seen:
            seen.add(key)
            unique.append(value)
    return unique


def _managed_executable(
    name: str,
    *,
    prefixes: list[Path] | None = None,
    platform: str | None = None,
    extension_mode: bool | None = None,
    path_search=None,
) -> Path:
    if name not in {"age", "age-keygen"}:
        raise ValueError("unsupported managed executable")
    platform = platform or sys.platform
    windows = platform.startswith("win")
    extension_mode = os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1" if extension_mode is None else extension_mode
    if prefixes is None:
        prefixes = _runtime_prefixes(platform)
        if extension_mode:
            prefixes = prefixes[:1]
    else:
        prefixes = [Path(value) for value in prefixes]
    for prefix in prefixes:
        candidates = (
            [prefix / "Library" / "bin" / f"{name}.exe", prefix / "Scripts" / f"{name}.exe", prefix / f"{name}.exe"]
            if windows
            else [prefix / "bin" / name]
        )
        for candidate in candidates:
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    if not extension_mode:
        found = (path_search or shutil.which)(name)
        if found:
            candidate = Path(found)
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    if extension_mode:
        raise CryptoError(f"managed controller environment does not provide {name}")
    raise CryptoError(f"{name} executable is unavailable")


def _identity(path: Path, platform: str | None = None) -> list[str]:
    if not path.is_file() or path.is_symlink():
        raise CryptoError(f"identity must be a regular file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if not (platform or sys.platform).startswith("win") and mode & 0o077:
        raise CryptoError(f"identity permissions must be private: {path}")
    return ["-i", str(path)]


def encrypt(data: bytes, recipients: list[str], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, source_name = tempfile.mkstemp(prefix=".age-source.", dir=output.parent)
    source = Path(source_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        encrypt_file(source, recipients, output)
    finally:
        source.unlink(missing_ok=True)


def encrypt_file(source: Path, recipients: list[str], output: Path) -> None:
    if len(recipients) < 2:
        raise CryptoError("production snapshots require two recipients")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [str(_managed_executable("age"))]
    for recipient in recipients:
        args.extend(["-r", recipient])
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        proc = subprocess.run([*args, "-o", str(temp), str(source)], capture_output=True, check=False)
        if proc.returncode:
            raise CryptoError(proc.stderr.decode(errors="replace"))
        temp.chmod(0o600)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)


def decrypt(path: Path, identity_paths: list[Path], max_bytes: int = MAX_DECRYPTED_SIZE) -> bytes:
    fd, output_name = tempfile.mkstemp(prefix=".age-compat.", dir=path.parent)
    os.close(fd)
    output = Path(output_name)
    try:
        decrypt_file(path, identity_paths, output, max_bytes)
        return output.read_bytes()
    finally:
        output.unlink(missing_ok=True)


def decrypt_file(
    path: Path, identity_paths: list[Path], output: Path, max_bytes: int = MAX_DECRYPTED_SIZE
) -> tuple[int, str]:
    args = [str(_managed_executable("age")), "--decrypt"]
    for identity in identity_paths:
        args += _identity(identity)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        proc = subprocess.run([*args, "-o", str(temp), str(path)], capture_output=True, check=False)
        if proc.returncode:
            raise CryptoError(proc.stderr.decode(errors="replace"))
        if temp.stat().st_size > max_bytes:
            raise CryptoError("decrypted data exceeds maximum size")
        digest = hashlib.sha256()
        size = 0
        with temp.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        temp.chmod(0o600)
        os.replace(temp, output)
        return size, digest.hexdigest()
    finally:
        temp.unlink(missing_ok=True)
