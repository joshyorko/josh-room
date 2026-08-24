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
    if len(recipients) < 2:
        raise CryptoError("production snapshots require two recipients")
    output.parent.mkdir(parents=True, exist_ok=True)
    args = ["age"]
    for recipient in recipients:
        args.extend(["-r", recipient])
    proc = subprocess.run([*args, "-o", str(output)], input=data, capture_output=True, check=False)
    if proc.returncode:
        raise CryptoError(proc.stderr.decode(errors="replace"))


def decrypt(path: Path, identity_paths: list[Path], max_bytes: int = MAX_DECRYPTED_SIZE) -> bytes:
    args = ["age", "--decrypt"]
    for identity in identity_paths:
        args += _identity(identity)
    fd, temp_name = tempfile.mkstemp(prefix=".age-decrypt.", dir=path.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        proc = subprocess.run([*args, "-o", str(temp), str(path)], capture_output=True, check=False)
        if proc.returncode:
            raise CryptoError(proc.stderr.decode(errors="replace"))
        if temp.stat().st_size > max_bytes:
            raise CryptoError("decrypted data exceeds maximum size")
        return temp.read_bytes()
    finally:
        temp.unlink(missing_ok=True)
