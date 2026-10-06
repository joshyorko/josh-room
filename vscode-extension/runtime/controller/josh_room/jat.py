import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from robocorp import log

from .cancellation import terminate_owned_process as _terminate_process
from .progress import report_progress

_STDOUT_LIMIT = 1_048_576
_RESULT_LIMIT = 1_048_576


class JATError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def _version(jat_root: Path) -> str:
    pinned = os.environ.get("JOSH_ROOM_JAT_SHA")
    if pinned:
        return pinned
    result = subprocess.run(["git", "-C", str(jat_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def _diagnostic(stderr: str) -> str:
    cleaned = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(stderr or ""))
    cleaned = " ".join(cleaned.split())
    for value in sorted(os.environ.values(), key=len, reverse=True):
        if value and len(value) > 3:
            cleaned = cleaned.replace(value, "[redacted]")
    cleaned = re.sub(r"\bAGE-SECRET-KEY-[A-Za-z0-9_-]+|\bage1[0-9a-z]{20,}", "[redacted]", cleaned)
    cleaned = re.sub(r"(?i)\bbearer\s+\S+", "******", cleaned)
    cleaned = re.sub(
        r"""(?i)\b((?:access[-_ ]?key(?:[-_ ]?id)?|secret[-_ ]?(?:access[-_ ]?)?key|session[-_ ]?token|password|token|authorization)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,]+)""",
        r"\1[redacted]",
        cleaned,
    )
    cleaned = re.sub(r"https?://\S+", "[redacted-url]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"""(?<![\w:])(?:[A-Za-z]:[\\/]|/)[^\s"'<>]+""", "[redacted-path]", cleaned)
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
    try:
        process = subprocess.Popen(argv, **options)
    except OSError as error:
        raise JATError(
            "JAT runtime execution is unavailable",
            {"command": "RCC execution", "exit_status": None, "error_type": type(error).__name__},
        ) from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        _terminate_process(process)
        raise JATError(
            "JAT operation timed out",
            {"command": "RCC execution", "exit_status": None, "timed_out": True},
        ) from error
    except BaseException:
        _terminate_process(process)
        raise
    return process.returncode, (stdout or "")[-_STDOUT_LIMIT:], _diagnostic(stderr)


def _run(argv: list[str], timeout: float | None, *, cwd: Path | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    exit_status, stdout, diagnostic = _run_cli(argv, timeout, cwd=cwd, env=env)
    return exit_status, _diagnostic(f"{stdout} {diagnostic}")


def _receipt_evidence(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {"state": "missing"}
    try:
        with path.open("rb") as handle:
            raw = handle.read(_RESULT_LIMIT + 1)
        if len(raw) > _RESULT_LIMIT:
            return {"state": "oversized"}
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"state": "invalid"}
    if not isinstance(value, dict):
        return {"state": "invalid"}
    evidence = {"state": "present"}
    for key in ("exitCode", "exit_code", "exit", "exit_status"):
        if type(value.get(key)) is int:
            evidence[key] = value[key]
    if isinstance(value.get("success"), bool):
        evidence["success"] = value["success"]
    for key in ("artifactDigest", "artifact_digest"):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get(key, ""))):
            evidence[key] = value[key]
    if isinstance(value.get("operation"), str) and value["operation"] in {
        "build", "restore", "doctor", "serve", "inspect", "extract", "export", "copy"
    }:
        evidence["operation"] = value["operation"]
    if isinstance(value.get("diagnostics"), str):
        evidence["diagnostic"] = _diagnostic(value["diagnostics"])
    return evidence


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
        with path.open("rb") as handle:
            raw = handle.read(_RESULT_LIMIT + 1)
        if len(raw) > _RESULT_LIMIT:
            raise JATError("managed RCC produced an oversized execution receipt")
        receipt = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise JATError("managed RCC produced an invalid execution receipt") from error
    if not isinstance(receipt, dict):
        raise JATError("managed RCC produced an incomplete execution receipt")
    observed = receipt.get("artifactDigest", receipt.get("artifact_digest"))
    reported = receipt.get("exitCode", receipt.get("exit_code", receipt.get("exit")))
    if observed != artifact or type(reported) is not int or reported != exit_status:
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
    if not Path(executable).is_absolute():
        raise JATError("managed RCC executable must be an absolute path")
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
    environment["PATH"] = os.pathsep.join(
        filter(None, [str(Path(executable).parent), environment.get("PATH")])
    )
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
    evidence = None
    try:
        report_progress("jat", f"Running JAT {task} through RCC")
        timeout = None if foreground else float(os.environ.get("JOSH_ROOM_JAT_TIMEOUT", "3600"))
        exit_status, stdout, stderr = _run_cli(argv, timeout, **run_kwargs)
        diagnostic = _diagnostic(f"{stdout} {stderr}")
        evidence = {
            "stage": f"jat-{task.lower()}",
            "command": f"python -m jat.task_runner run tasks.py -t {task}",
            "exit_status": exit_status,
            "stdout": _diagnostic(stdout),
            "stderr": stderr,
            "rcc_receipt": _receipt_evidence(rcc_receipt),
            "jat_result": _receipt_evidence(result_path),
        }
        if managed is not None:
            _validate_rcc_receipt(rcc_receipt, artifact, exit_status)
        if not result_path.is_file():
            message = "JAT task did not produce a fresh output/result.json"
            if diagnostic:
                message += f": {diagnostic}"
            raise JATError(message, {"argv": argv, "exit_status": exit_status, "diagnostic": diagnostic})
        try:
            with result_path.open("rb") as handle:
                raw = handle.read(_RESULT_LIMIT + 1)
            if len(raw) > _RESULT_LIMIT:
                raise JATError("JAT task produced an oversized output/result.json")
            result = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise JATError("JAT task produced an invalid output/result.json") from error
        if not isinstance(result, dict):
            raise JATError("JAT task produced an incomplete output/result.json")
        expected_operation = task.lower()
        if result.get("operation") != expected_operation:
            raise JATError(f"JAT receipt operation mismatch: expected {expected_operation}", result)
        if type(result.get("exit_status")) is not int or result.get("exit_status") != exit_status or not isinstance(result.get("success"), bool):
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
    except JATError as error:
        if evidence is not None:
            error.result = {
                **evidence,
                "diagnostic": diagnostic,
            }
            if diagnostic and diagnostic not in str(error):
                error.args = (f"{error}: {diagnostic}",)
        raise
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
        exit_status, stdout, stderr = _run_cli(argv, timeout, **run_kwargs)
        diagnostic = _diagnostic(f"{stdout} {stderr}")
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
        if type(result.get("exit_status")) is not int or result.get("exit_status") != exit_status or not isinstance(result.get("success"), bool):
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
    rcc_archive: Path | None = None,
    rcc_metadata: Path | None = None,
    brew_archive: Path | None = None,
    hauler_archive: Path | None = None,
    chunk_size: str | None = None,
    exclude_extras: bool = False,
    retries: int | None = None,
) -> dict:
    if (rcc_archive is None) != (rcc_metadata is None):
        raise ValueError("saved RCC archive and metadata must be supplied together")
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
    if rcc_archive is not None:
        request["rcc_archive"] = str(rcc_archive)
        request["rcc_metadata"] = str(rcc_metadata)
    if brew_archive is not None:
        request["brew_archive"] = str(brew_archive)
    if hauler_archive is not None:
        request["hauler_archive"] = str(hauler_archive)
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
