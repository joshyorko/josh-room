import hashlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

MAX_DECRYPTED_SIZE = 8 * 1024 * 1024 * 1024 + 2 * 1024 * 1024


class CryptoError(RuntimeError):
    pass


def _identity(path: Path) -> list[str]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
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
    args = ["age"]
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
    args = ["age", "--decrypt"]
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
