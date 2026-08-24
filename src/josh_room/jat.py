import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from robocorp import log


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


def _run(argv: list[str], timeout: float | None) -> tuple[int, str]:
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
    return process.returncode, _diagnostic(stderr or _stdout)


def _jat_contract(jat_root: Path) -> dict[str, bool]:
    robot = jat_root / "robot.yaml"
    tasks = jat_root / "tasks.py"
    try:
        robot_text = robot.read_text()
        tasks_text = tasks.read_text()
    except OSError:
        return {"robot": False, "tasks": False, "interactive": False}
    return {
        "robot": all(f"  {name}:" in robot_text for name in ("Build", "Restore", "Serve")),
        "tasks": all(f"def {name}(" in tasks_text for name in ("Build", "Restore", "Serve")),
        "interactive": "  JAT:" in robot_text and "jat.cli" in robot_text,
    }


def _request_file(root: Path, operation: str, request: dict) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix=f".josh-room-{operation}-", suffix=".json", dir=root, delete=False
    ) as handle:
        path = Path(handle.name)
        json.dump(request, handle, sort_keys=True)
        handle.write("\n")
    return path


def _run_task(jat_root: Path, task: str, request: dict, *, foreground: bool = False) -> dict:
    request_path = _request_file(jat_root, task.lower(), request)
    result_path = jat_root / "output" / "result.json"
    result_path.unlink(missing_ok=True)
    argv = ["rcc", "run", "-r", str(jat_root / "robot.yaml"), "-t", task, "--", "--json-input", str(request_path)]
    try:
        timeout = None if foreground else float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600"))
        exit_status, diagnostic = _run(argv, timeout)
        if not result_path.is_file():
            message = "JAT task did not produce a fresh output/result.json"
            if diagnostic:
                message += f": {diagnostic}"
            raise JATError(message, {"argv": argv, "exit_status": exit_status, "diagnostic": diagnostic})
        result = json.loads(result_path.read_text())
        expected_operation = task.lower()
        if result.get("operation") != expected_operation:
            raise JATError(f"JAT receipt operation mismatch: expected {expected_operation}", result)
        if result.get("exit_status") != exit_status or not isinstance(result.get("success"), bool):
            raise JATError("JAT receipt exit status is inconsistent with RCC", result)
        result.setdefault("diagnostics", diagnostic)
        result["executable"] = argv[0]
        result["argv"] = argv
        result["version"] = _version(jat_root)
        result["diagnostic"] = _diagnostic(result.get("diagnostics", diagnostic))
        if "payload_sha256" not in result and result.get("sha256"):
            result["payload_sha256"] = result["sha256"]
        log.info(f"JAT {task} completed with exit status {exit_status}")
        if exit_status or not result.get("success", False):
            raise JATError(f"JAT {task.lower()} failed with exit {exit_status}", result)
        return result
    finally:
        request_path.unlink(missing_ok=True)


def run_build(
    jat_root: Path,
    source: Path,
    output: Path,
    *,
    images: list[str] | None = None,
    all_images: bool = False,
) -> dict:
    return _run_task(jat_root, "Build", {
        "folder": str(source),
        "output": str(output),
        "images": images or [],
        "all_images": all_images,
    })


def run_restore(jat_root: Path, haul: Path, destination: Path) -> dict:
    return _run_task(jat_root, "Restore", {"haul": str(haul), "destination": str(destination)})


def run_serve(jat_root: Path, haul: Path) -> dict:
    return _run_task(jat_root, "Serve", {"haul": str(haul)}, foreground=True)
