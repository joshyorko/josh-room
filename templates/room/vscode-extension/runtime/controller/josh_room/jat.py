import json
import os
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

from robocorp import log

from .progress import report_progress

_STDOUT_LIMIT = 1_048_576
_RESULT_LIMIT = 1_048_576


class JATError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def _terminate_process(process, platform: str | None = None) -> None:
    platform = platform or os.name
    if platform == "nt":
        process.terminate()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    process.communicate()


def _version(jat_root: Path) -> str:
    pinned = os.environ.get("JOSH_ROOM_JAT_SHA")
    if pinned:
        return pinned
    result = subprocess.run(["git", "-C", str(jat_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def _diagnostic(stderr: str) -> str:
    cleaned = " ".join(stderr.replace("\x1b", "").split())
    for value in os.environ.values():
        if value and len(value) > 3:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned[-4096:]


def _run_cli(
    argv: list[str], timeout: float | None, *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run one bounded subprocess and return (exit status, capped stdout, redacted diagnostic)."""
    options = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name != "nt":
        options["start_new_session"] = True
    process = subprocess.Popen(argv, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process(process)
        raise JATError("JAT operation timed out", {"argv": argv, "exit_status": None, "timed_out": True}) from error
    except BaseException:
        _terminate_process(process)
        raise
    return process.returncode, (stdout or "")[-_STDOUT_LIMIT:], _diagnostic(stderr or stdout)


def _run(argv: list[str], timeout: float | None, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    exit_status, _stdout, diagnostic = _run_cli(argv, timeout, cwd=cwd, env=env)
    return exit_status, diagnostic


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


def _rcc_receipt_path(root: Path, task: str) -> Path:
    return root / "output" / f".rcc-{task.lower()}-{uuid.uuid4().hex}.json"


def _validate_rcc_receipt(path: Path, artifact: str, exit_status: int) -> None:
    if not path.is_file():
        raise JATError("managed RCC did not produce its execution receipt")
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise JATError("managed RCC produced an invalid execution receipt") from error
    observed = receipt.get("artifactDigest", receipt.get("artifact_digest"))
    reported = receipt.get("exitCode", receipt.get("exit_code", receipt.get("exit")))
    if observed != artifact or reported is not None and int(reported) != exit_status:
        raise JATError("managed RCC execution receipt does not match the selected artifact")


def _managed_runtime(jat_root: Path) -> tuple[str, str, dict[str, str]] | None:
    extension_mode = os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1"
    handoff_values = (
        os.environ.get("JOSH_ROOM_RCC_EXE"),
        os.environ.get("JOSH_ROOM_JAT_ARTIFACT"),
        os.environ.get("JOSH_ROOM_RCC_HOME"),
    )
    if not extension_mode and any(handoff_values):
        raise JATError("managed Josh Room runtime is incomplete")
    if not extension_mode:
        return None
    executable = os.environ.get("JOSH_ROOM_RCC_EXE")
    artifact = os.environ.get("JOSH_ROOM_JAT_ARTIFACT")
    rcc_home = os.environ.get("JOSH_ROOM_RCC_HOME")
    if not executable or not artifact or not rcc_home:
        raise JATError("managed Josh Room runtime is incomplete")
    environment = os.environ.copy()
    environment.update(
        {
            "ROBOCORP_HOME": rcc_home,
            "RCC_HOLOTREE_MODE": "private",
            "JOSH_ROOM_JAT_ROOT": str(jat_root),
            "ROBOT_ARTIFACTS": str(jat_root / "output"),
            "JAT_RUN_DIR": str(jat_root / "output"),
        }
    )
    python_path = [str(jat_root / "src"), str(jat_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return executable, artifact, environment


def _run_task(jat_root: Path, task: str, request: dict | None, *, foreground: bool = False) -> dict:
    jat_root.mkdir(parents=True, exist_ok=True)
    (jat_root / "output").mkdir(parents=True, exist_ok=True)
    request_path = _request_file(jat_root, task.lower(), request) if request is not None else None
    result_path = jat_root / "output" / "result.json"
    result_path.unlink(missing_ok=True)
    managed = _managed_runtime(jat_root)
    rcc_receipt = None
    if managed is None:
        argv = ["rcc", "run", "-r", str(jat_root / "robot.yaml"), "-t", task]
        if request_path is not None:
            argv.extend(("--", "--json-input", str(request_path)))
        environment = os.environ.copy()
        environment.update({
            "JOSH_ROOM_JAT_ROOT": str(jat_root),
            "ROBOT_ARTIFACTS": str(jat_root / "output"),
            "JAT_RUN_DIR": str(jat_root / "output"),
        })
        run_kwargs = {"cwd": jat_root, "env": environment}
    else:
        executable, artifact, environment = managed
        rcc_receipt = _rcc_receipt_path(jat_root, task)
        argv = [
            executable,
            "--no-build",
            "env",
            "exec",
            "--artifact",
            artifact,
            "--permissive-local",
            "--inherit-streams",
            "--receipt-file",
            str(rcc_receipt),
            "--json",
            "--",
            "python",
            "-m",
            "jat.task_runner",
            "run",
            str(jat_root / "tasks.py"),
            "-t",
            task,
        ]
        if request_path is not None:
            argv.extend(("--", "--json-input", str(request_path)))
        run_kwargs = {"cwd": jat_root, "env": environment}
    try:
        report_progress("jat", f"Running JAT {task} through RCC")
        timeout = None if foreground else float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600"))
        exit_status, diagnostic = _run(argv, timeout, **run_kwargs)
        if managed is not None:
            _validate_rcc_receipt(rcc_receipt, artifact, exit_status)
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
        report_progress("jat", f"JAT {task} completed")
        return result
    finally:
        if request_path is not None:
            request_path.unlink(missing_ok=True)
        if rcc_receipt is not None:
            rcc_receipt.unlink(missing_ok=True)


def _json_object_candidates(text: str):
    """Yield only top-level JSON object substrings from noisy subprocess output."""
    start = None
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                yield text[start : index + 1]
                start = None


def _extract_operation_result(output: str) -> dict | None:
    """Pull the canonical JAT OperationResult out of combined RCC/CLI stdout."""
    text = str(output or "")[-_STDOUT_LIMIT:]
    chosen = None
    for candidate in _json_object_candidates(text):
        if len(candidate) > _RESULT_LIMIT:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("operation"), str)
            and isinstance(parsed.get("success"), bool)
            and "exit_status" in parsed
        ):
            chosen = parsed
    return chosen


def _run_jat_cli(jat_root: Path, cli_args: list[str], *, foreground: bool = False) -> dict:
    """Invoke the canonical JAT CLI contract inside the acquired JAT runtime.

    Managed mode runs `python -m jat.cli` inside the pinned JAT Environment
    Artifact via RCC env exec; the plain fallback runs the repository's `JAT`
    robot task. Hauler behavior stays owned by JAT — this bridge only invokes
    and validates the machine-readable result.
    """
    jat_root.mkdir(parents=True, exist_ok=True)
    (jat_root / "output").mkdir(parents=True, exist_ok=True)
    operation = cli_args[0]
    managed = _managed_runtime(jat_root)
    rcc_receipt = None
    if managed is None:
        argv = ["rcc", "run", "-r", str(jat_root / "robot.yaml"), "-t", "JAT", "--", *cli_args]
        environment = os.environ.copy()
        environment.update({
            "JOSH_ROOM_JAT_ROOT": str(jat_root),
            "ROBOT_ARTIFACTS": str(jat_root / "output"),
            "JAT_RUN_DIR": str(jat_root / "output"),
        })
        run_kwargs = {"cwd": jat_root, "env": environment}
    else:
        executable, artifact, environment = managed
        rcc_receipt = _rcc_receipt_path(jat_root, operation)
        argv = [
            executable,
            "--no-build",
            "env",
            "exec",
            "--artifact",
            artifact,
            "--permissive-local",
            "--inherit-streams",
            "--receipt-file",
            str(rcc_receipt),
            "--json",
            "--",
            "python",
            "-m",
            "jat.cli",
            *cli_args,
        ]
        run_kwargs = {"cwd": jat_root, "env": environment}
    try:
        report_progress("jat", f"Running jat {operation} through RCC")
        timeout = None if foreground else float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600"))
        exit_status, stdout, diagnostic = _run_cli(argv, timeout, **run_kwargs)
        if managed is not None:
            _validate_rcc_receipt(rcc_receipt, artifact, exit_status)
        result = _extract_operation_result(stdout)
        if result is None:
            message = "JAT CLI did not produce a machine-readable result"
            if diagnostic:
                message += f": {diagnostic}"
            raise JATError(message, {"argv": argv, "exit_status": exit_status, "diagnostic": diagnostic})
        if result.get("operation") != operation:
            raise JATError(f"JAT receipt operation mismatch: expected {operation}", result)
        if result.get("exit_status") != exit_status or not isinstance(result.get("success"), bool):
            raise JATError("JAT receipt exit status is inconsistent with RCC", result)
        result.setdefault("diagnostics", diagnostic)
        result["executable"] = argv[0]
        result["argv"] = argv
        result["version"] = _version(jat_root)
        result["diagnostic"] = _diagnostic(result.get("diagnostics", diagnostic))
        log.info(f"JAT {operation} completed with exit status {exit_status}")
        if exit_status or not result.get("success", False):
            raise JATError(f"jat {operation} failed with exit {exit_status}", result)
        report_progress("jat", f"JAT {operation} completed")
        return result
    finally:
        if rcc_receipt is not None:
            rcc_receipt.unlink(missing_ok=True)


def run_build(
    jat_root: Path,
    source: Path,
    output: Path,
    *,
    images: list[str] | None = None,
    all_images: bool = False,
    rcc_environment: str | None = None,
    images_files: list[str] | None = None,
    hauler_manifests: list[str] | None = None,
    chunk_size: str | None = None,
    exclude_extras: bool = False,
    retries: int | None = None,
) -> dict:
    request = {
        "folder": str(source),
        "output": str(output),
        "images": images or [],
        "all_images": all_images,
    }
    if rcc_environment is not None:
        request["rcc_environment"] = rcc_environment
    if images_files:
        request["images_files"] = [str(value) for value in images_files]
    if hauler_manifests:
        request["hauler_manifests"] = [str(value) for value in hauler_manifests]
    if chunk_size:
        request["chunk_size"] = str(chunk_size)
    if exclude_extras:
        request["exclude_extras"] = True
    if retries is not None:
        request["retries"] = int(retries)
    return _run_task(jat_root, "Build", request)


def run_restore(jat_root: Path, haul: Path, destination: Path) -> dict:
    return _run_task(jat_root, "Restore", {"haul": str(haul), "destination": str(destination)})


def run_serve(jat_root: Path, haul: Path, *, mode: str = "auto") -> dict:
    return _run_jat_cli(
        jat_root,
        ["serve", "--haul", str(haul), "--mode", str(mode), "--json"],
        foreground=True,
    )


def run_inspect(jat_root: Path, haul: Path) -> dict:
    return _run_jat_cli(jat_root, ["inspect", "--haul", str(haul), "--json"])


def run_extract(jat_root: Path, haul: Path, reference: str, destination: Path) -> dict:
    return _run_jat_cli(
        jat_root,
        [
            "extract",
            "--haul",
            str(haul),
            "--reference",
            str(reference),
            "--destination",
            str(destination),
            "--json",
        ],
    )


def run_export(jat_root: Path, haul: Path, output: Path) -> dict:
    return _run_jat_cli(
        jat_root,
        ["export", "--haul", str(haul), "--format", "containerd", "--output", str(output), "--json"],
    )


def run_copy(
    jat_root: Path,
    haul: Path,
    to: str,
    *,
    retries: int | None = None,
    plain_http: bool = False,
    insecure: bool = False,
) -> dict:
    cli_args = ["copy", "--haul", str(haul), "--to", str(to)]
    if retries is not None:
        cli_args.extend(("--retries", str(int(retries))))
    if plain_http:
        cli_args.append("--plain-http")
    if insecure:
        cli_args.append("--insecure")
    cli_args.append("--json")
    return _run_jat_cli(jat_root, cli_args)


def run_doctor(jat_root: Path) -> dict:
    return _run_task(jat_root, "Doctor", None)
