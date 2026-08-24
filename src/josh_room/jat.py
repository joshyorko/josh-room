import hashlib
import os
import signal
import subprocess
from pathlib import Path


class JATError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def _version(jat_root: Path) -> str:
    result = subprocess.run(["git", "-C", str(jat_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def _diagnostic(stderr: str) -> str:
    cleaned = " ".join(stderr.replace("\x1b", "").split())
    for value in os.environ.values():
        if value and len(value) > 3:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned[:2048]


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate()
        raise JATError("JAT operation timed out", {"argv": argv, "exit_status": None, "timed_out": True}) from error
    except BaseException:
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate()
        raise
    return process.returncode, _diagnostic(stderr)


def run_build(jat_root: Path, source: Path, output: Path) -> dict:
    argv = ["bash", str(jat_root / "joshs-all-the-things.sh"), "build", "--folder", str(source), "--output", str(output)]
    exit_status, diagnostic = _run(argv, float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600")))
    result = {"executable": str(jat_root / "joshs-all-the-things.sh"), "version": _version(jat_root), "argv": argv, "exit_status": exit_status, "diagnostic": diagnostic}
    if exit_status:
        raise JATError(f"JAT build failed with exit {exit_status}", result)
    body = output.read_bytes()
    result.update({"payload_path": str(output), "payload_size": len(body), "payload_sha256": hashlib.sha256(body).hexdigest()})
    return result


def run_restore(jat_root: Path, haul: Path, destination: Path) -> dict:
    argv = ["bash", str(jat_root / "joshs-all-the-things.sh"), "restore", "--haul", str(haul), "--destination", str(destination)]
    exit_status, diagnostic = _run(argv, float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600")))
    result = {"executable": str(jat_root / "joshs-all-the-things.sh"), "version": _version(jat_root), "argv": argv, "exit_status": exit_status, "diagnostic": diagnostic}
    if exit_status:
        raise JATError(f"JAT restore failed with exit {exit_status}", result)
    return result
