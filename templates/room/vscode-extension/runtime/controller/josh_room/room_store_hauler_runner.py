"""Managed adapter that runs JAT's native Hauler API in its selected artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import jat, private_paths

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 4096
_OPERATIONS = {"sync", "sync_image_txt", "sync_files", "inventory", "save", "acquire_rcc"}
_VERSION = re.compile(r"(?<![A-Za-z0-9])v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?![A-Za-z0-9])")
JAT_PLATFORM_MAP = {"linux-x64": "linux_amd64", "win32-x64": "windows_amd64"}
_HAULER_VERSION_PROBE = (
    "import os,shutil,subprocess,sys; e=shutil.which('hauler'); p=os.environ.get('CONDA_PREFIX'); "
    "r=os.path.realpath(p) if p else ''; x=os.path.realpath(e) if e else ''; q=os.path.realpath(sys.executable); "
    "ok=bool(r and x.startswith(r+os.sep) and q.startswith(r+os.sep)); "
    "sys.exit(127 if not ok else subprocess.run([x,'version'],check=False).returncode)"
)


class ManagedHaulerError(RuntimeError):
    """Bounded, path-free managed Hauler failure."""


def _require_managed_runtime(jat_root: Path):
    try:
        runtime_root = Path(jat_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ManagedHaulerError("managed JAT environment is unavailable") from None
    if not runtime_root.is_dir():
        raise ManagedHaulerError("managed JAT environment is unavailable")
    managed = jat._managed_runtime(runtime_root)
    if managed is None:
        raise ManagedHaulerError("managed JAT environment is unavailable")
    executable, artifact, environment = managed
    if (
        not isinstance(executable, str)
        or not executable
        or not isinstance(artifact, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact)
    ):
        raise ManagedHaulerError("managed JAT environment is unavailable")
    private_home = environment.get("ROBOCORP_HOME")
    if not isinstance(private_home, str) or not private_home:
        raise ManagedHaulerError("managed JAT private home is unavailable")
    try:
        private_paths.verify_private_path(Path(private_home), directory=True)
    except private_paths.PrivatePathError:
        raise ManagedHaulerError("managed JAT private home is unsafe") from None
    source_root = str(Path(__file__).resolve().parent.parent)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        [source_root, *([existing] if isinstance(existing, str) and existing else [])]
    )
    return executable, artifact, environment


def _validate_worker_result(path: Path, operation: str, process_status: int) -> Any:
    try:
        private_paths.verify_private_path(path, directory=False)
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_RESULT_BYTES:
            raise ManagedHaulerError("managed Hauler returned no valid operation receipt")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ManagedHaulerError:
        raise
    except private_paths.PrivatePathError:
        raise ManagedHaulerError("managed Hauler returned no valid operation receipt") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ManagedHaulerError("managed Hauler returned no valid operation receipt") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "operation", "success", "exit_status", "value", "error"}
        or value.get("format_version") != 1
        or value.get("operation") != operation
        or type(value.get("success")) is not bool
        or type(value.get("exit_status")) is not int
        or value["exit_status"] != process_status
        or (value["success"] != (process_status == 0))
    ):
        raise ManagedHaulerError("managed Hauler operation receipt is inconsistent")
    if process_status != 0:
        raise ManagedHaulerError("native Hauler operation failed")
    if value["error"] is not None:
        raise ManagedHaulerError("managed Hauler operation receipt is invalid")
    if operation == "acquire_rcc":
        metadata = value["value"]
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"artifact", "specification_digest", "platform"}
            or not isinstance(metadata["artifact"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["artifact"])
            or not isinstance(metadata["specification_digest"], str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata["specification_digest"])
            or not isinstance(metadata["platform"], str)
            or not re.fullmatch(r"[a-z0-9]+_[a-z0-9]+", metadata["platform"])
        ):
            raise ManagedHaulerError("managed RCC verification returned invalid metadata")
    return value["value"]


def _request_file(path: Path, request: Mapping[str, Any]) -> None:
    body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(body) > MAX_REQUEST_BYTES:
        raise ManagedHaulerError("managed Hauler request exceeds its size limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        private_paths.secure_private_file(descriptor, path)
        with os.fdopen(descriptor, "wb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        raise ManagedHaulerError("managed Hauler request could not be prepared") from None
    except private_paths.PrivatePathError:
        raise ManagedHaulerError("managed Hauler request could not be prepared") from None


def _operation_result_path(root: Path, operation: str) -> Path:
    return root / f"{operation}-{uuid.uuid4().hex}.json"


class ManagedHaulerAdapter:
    """Small proxy for the JAT HaulerAdapter API used by Room Store capture."""

    def __init__(self, jat_root: Path, cancellation: Any = None):
        self.jat_root = Path(jat_root).resolve(strict=True)
        self.cancellation = cancellation
        self.timeout = _hauler_timeout()
        self.executable, self.artifact, self.environment = _require_managed_runtime(self.jat_root)
        self.hauler_version = self._observe_version()
        _check_cancel(self.cancellation)

    def _observe_version(self) -> str:
        _check_cancel(self.cancellation)
        with tempfile.TemporaryDirectory(prefix="josh-room-hauler-version-") as temporary:
            root = Path(temporary)
            private_paths.protect_private_directory(root)
            private_paths.verify_private_path(root, directory=True)
            receipt = root / "rcc-receipt.json"
            argv = [
                self.executable,
                "--no-build",
                "env",
                "exec",
                "--artifact",
                self.artifact,
                "--permissive-local",
                "--inherit-streams",
                "--receipt-file",
                str(receipt),
                "--json",
                "--",
                "python",
                "-c",
                _HAULER_VERSION_PROBE,
            ]
            try:
                status, stdout, _diagnostic = jat._run_cli(
                    argv, min(self.timeout, 120), cwd=self.jat_root, env=self.environment
                )
                private_paths.protect_private_file(receipt)
                jat._validate_rcc_receipt(receipt, self.artifact, status)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise ManagedHaulerError("selected JAT artifact could not verify Hauler") from None
            if status != 0:
                raise ManagedHaulerError("selected JAT artifact could not verify Hauler")
            versions = _VERSION.findall(stdout)
            if len(versions) != 1:
                raise ManagedHaulerError("selected JAT artifact returned an invalid Hauler version")
            return versions[0]

    def _invoke(self, operation: str, values: Mapping[str, Any]) -> Any:
        if operation not in _OPERATIONS:
            raise ManagedHaulerError("unsupported managed Hauler operation")
        _check_cancel(self.cancellation)
        with tempfile.TemporaryDirectory(prefix="josh-room-hauler-call-") as temporary:
            root = Path(temporary)
            private_paths.protect_private_directory(root)
            private_paths.verify_private_path(root, directory=True)
            request = root / "request.json"
            result = _operation_result_path(root, operation)
            receipt = root / "rcc-receipt.json"
            _request_file(request, {"format_version": 1, "operation": operation, **values})
            argv = [
                self.executable,
                "--no-build",
                "env",
                "exec",
                "--artifact",
                self.artifact,
                "--permissive-local",
                "--inherit-streams",
                "--receipt-file",
                str(receipt),
                "--json",
                "--",
                "python",
                "-m",
                "josh_room.room_store_hauler_runner",
                "--worker",
                "--request-file",
                str(request),
                "--result-file",
                str(result),
            ]
            try:
                status, _stdout, _diagnostic = jat._run_cli(
                    argv, self.timeout, cwd=self.jat_root, env=self.environment
                )
                private_paths.protect_private_file(receipt)
                private_paths.protect_private_file(result)
                jat._validate_rcc_receipt(receipt, self.artifact, status)
            except (OSError, RuntimeError, TypeError, ValueError):
                raise ManagedHaulerError("managed Hauler operation was interrupted or failed") from None
            _check_cancel(self.cancellation)
            return _validate_worker_result(result, operation, status)

    @staticmethod
    def _store_temp(store: Path, temp: Path) -> dict[str, str]:
        return {"store": _absolute_path(store), "temp": _absolute_path(temp)}

    def sync(
        self,
        store: Path,
        temp: Path,
        *manifests: str | Path,
        retries: int | None = None,
        exclude_extras: bool = False,
        concurrency: int | None = None,
        ca_file: Path | None = None,
        insecure_skip_tls_verify: bool = False,
    ):
        request = self._store_temp(store, temp)
        request.update(
            {
                "manifests": [_absolute_path(path) for path in manifests],
                "retries": retries,
                "exclude_extras": exclude_extras,
                "concurrency": concurrency,
                "ca_file": _absolute_path(ca_file) if ca_file is not None else None,
                "insecure_skip_tls_verify": insecure_skip_tls_verify,
            }
        )
        return self._invoke("sync", request)

    def sync_image_txt(
        self,
        store: Path,
        temp: Path,
        sources: Sequence[str],
        retries: int | None = None,
        exclude_extras: bool = False,
        concurrency: int | None = None,
        ca_file: Path | None = None,
        insecure_skip_tls_verify: bool = False,
        platform: str | None = None,
    ):
        request = self._store_temp(store, temp)
        request.update(
            {
                "sources": [_absolute_path(path) for path in sources],
                "retries": retries,
                "exclude_extras": exclude_extras,
                "concurrency": concurrency,
                "ca_file": _absolute_path(ca_file) if ca_file is not None else None,
                "insecure_skip_tls_verify": insecure_skip_tls_verify,
                "platform": platform,
            }
        )
        return self._invoke("sync_image_txt", request)

    def sync_files(
        self,
        store: Path,
        temp: Path,
        files: Sequence[tuple[Path, str]],
        images: Sequence[str] | None = None,
        retries: int | None = None,
        exclude_extras: bool = False,
    ):
        if images:
            raise ManagedHaulerError("local Docker image publication is unavailable in Room Store capture")
        request = self._store_temp(store, temp)
        request.update(
            {
                "files": [[_absolute_path(path), name] for path, name in files],
                "retries": retries,
                "exclude_extras": exclude_extras,
            }
        )
        return self._invoke("sync_files", request)

    def inventory(self, store: Path, temp: Path, check: bool = False) -> list[dict[str, Any]]:
        value = self._invoke("inventory", {**self._store_temp(store, temp), "check": check})
        if not isinstance(value, list) or len(value) > MAX_ITEMS or any(not isinstance(item, dict) for item in value):
            raise ManagedHaulerError("managed Hauler returned invalid inventory")
        return value

    def save(self, store: Path, temp: Path, haul: Path, chunk_size: str | None = None, containerd: bool = False):
        return self._invoke(
            "save",
            {
                **self._store_temp(store, temp),
                "haul": _absolute_path(haul),
                "chunk_size": chunk_size,
                "containerd": containerd,
            },
        )

    def acquire_rcc(self, archive: Path, metadata: Path, workspace: Path, robot_file: Path) -> dict[str, str]:
        value = self._invoke(
            "acquire_rcc",
            {
                "archive": _absolute_path(archive),
                "metadata": _absolute_path(metadata),
                "workspace": _absolute_path(workspace),
                "robot_file": _absolute_path(robot_file),
            },
        )
        if not isinstance(value, dict):
            raise ManagedHaulerError("managed RCC verification returned invalid metadata")
        return value


def create_managed_hauler_adapter(jat_root: Path, cancellation: Any = None) -> ManagedHaulerAdapter:
    """Create an adapter bound to the currently selected JAT Environment Artifact."""
    return ManagedHaulerAdapter(jat_root, cancellation)


def _hauler_timeout() -> float:
    value = os.environ.get("JOSH_ROOM_HAULER_TIMEOUT", "900")
    try:
        timeout = float(value)
    except ValueError:
        raise ManagedHaulerError("Hauler timeout configuration is invalid") from None
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 3600:
        raise ManagedHaulerError("Hauler timeout configuration is invalid")
    return timeout


def _absolute_path(value: str | Path) -> str:
    try:
        return str(Path(value).resolve())
    except (OSError, RuntimeError):
        raise ManagedHaulerError("managed Hauler path is unavailable") from None


def _check_cancel(token: Any) -> None:
    if token is not None and token.cancelled:
        raise ManagedHaulerError("managed Hauler operation was cancelled")


def _worker_request(path: Path) -> dict[str, Any]:
    try:
        private_paths.verify_private_path(path.parent, directory=True)
        private_paths.verify_private_path(path, directory=False)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            raise ManagedHaulerError("managed Hauler request is invalid")
        request = json.loads(path.read_text(encoding="utf-8"))
    except ManagedHaulerError:
        raise
    except private_paths.PrivatePathError:
        raise ManagedHaulerError("managed Hauler request is invalid") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ManagedHaulerError("managed Hauler request is invalid") from None
    if not isinstance(request, dict) or request.get("format_version") != 1:
        raise ManagedHaulerError("managed Hauler request is invalid")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ManagedHaulerError("managed Hauler operation is unsupported")
    required = {
        "sync": {"format_version", "operation", "store", "temp", "manifests", "retries", "exclude_extras", "concurrency", "ca_file", "insecure_skip_tls_verify"},
        "sync_image_txt": {"format_version", "operation", "store", "temp", "sources", "retries", "exclude_extras", "concurrency", "ca_file", "insecure_skip_tls_verify", "platform"},
        "sync_files": {"format_version", "operation", "store", "temp", "files", "retries", "exclude_extras"},
        "inventory": {"format_version", "operation", "store", "temp", "check"},
        "save": {"format_version", "operation", "store", "temp", "haul", "chunk_size", "containerd"},
        "acquire_rcc": {"format_version", "operation", "archive", "metadata", "workspace", "robot_file"},
    }[operation]
    if set(request) != required:
        raise ManagedHaulerError("managed Hauler request is invalid")
    path_fields = (
        ("archive", "metadata", "workspace", "robot_file")
        if operation == "acquire_rcc"
        else ("store", "temp")
    )
    for field in path_fields:
        if not isinstance(request[field], str) or not request[field] or len(request[field]) > 4096:
            raise ManagedHaulerError("managed Hauler request is invalid")
    if operation == "acquire_rcc":
        return request
    if operation == "sync":
        _validate_strings(request["manifests"], MAX_ITEMS)
        _validate_common_options(request)
    elif operation == "sync_image_txt":
        _validate_strings(request["sources"], MAX_ITEMS)
        _validate_common_options(request)
        if request["platform"] is not None and not isinstance(request["platform"], str):
            raise ManagedHaulerError("managed Hauler request is invalid")
    elif operation == "sync_files":
        files = request["files"]
        if (
            not isinstance(files, list)
            or len(files) > MAX_ITEMS
            or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) or not value or len(value) > 4096 for value in item)
                for item in files
            )
        ):
            raise ManagedHaulerError("managed Hauler request is invalid")
        _validate_retries(request)
        if type(request["exclude_extras"]) is not bool:
            raise ManagedHaulerError("managed Hauler request is invalid")
    elif operation == "inventory":
        if type(request["check"]) is not bool:
            raise ManagedHaulerError("managed Hauler request is invalid")
    else:
        if (
            not isinstance(request["haul"], str)
            or not request["haul"]
            or len(request["haul"]) > 4096
            or request["chunk_size"] is not None and not isinstance(request["chunk_size"], str)
            or type(request["containerd"]) is not bool
        ):
            raise ManagedHaulerError("managed Hauler request is invalid")
    return request


def _validate_strings(value: object, limit: int) -> None:
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in value)
    ):
        raise ManagedHaulerError("managed Hauler request is invalid")


def _validate_retries(request: Mapping[str, Any]) -> None:
    retries = request["retries"]
    if retries is not None and (type(retries) is not int or retries < 0 or retries > 100):
        raise ManagedHaulerError("managed Hauler request is invalid")


def _validate_common_options(request: Mapping[str, Any]) -> None:
    _validate_retries(request)
    if type(request["exclude_extras"]) is not bool:
        raise ManagedHaulerError("managed Hauler request is invalid")
    concurrency = request["concurrency"]
    if concurrency is not None and (type(concurrency) is not int or concurrency < 1 or concurrency > 1024):
        raise ManagedHaulerError("managed Hauler request is invalid")
    if request["ca_file"] is not None and (
        not isinstance(request["ca_file"], str) or not request["ca_file"] or len(request["ca_file"]) > 4096
    ):
        raise ManagedHaulerError("managed Hauler request is invalid")
    if type(request["insecure_skip_tls_verify"]) is not bool:
        raise ManagedHaulerError("managed Hauler request is invalid")


def _worker_value(request: dict[str, Any]) -> Any:
    operation = request["operation"]
    if operation == "acquire_rcc":
        return _worker_acquire_rcc(request)
    from jat.hauler import HaulerAdapter
    from jat.process import ProcessRunner

    adapter = HaulerAdapter(ProcessRunner(), timeout=_hauler_timeout())
    store = Path(request["store"])
    temp = Path(request["temp"])
    if operation == "sync":
        adapter.sync(
            store,
            temp,
            *request["manifests"],
            retries=request["retries"],
            exclude_extras=request["exclude_extras"],
            concurrency=request["concurrency"],
            ca_file=Path(request["ca_file"]) if request["ca_file"] else None,
            insecure_skip_tls_verify=request["insecure_skip_tls_verify"],
        )
        return None
    if operation == "sync_image_txt":
        adapter.sync_image_txt(
            store,
            temp,
            request["sources"],
            retries=request["retries"],
            exclude_extras=request["exclude_extras"],
            concurrency=request["concurrency"],
            ca_file=Path(request["ca_file"]) if request["ca_file"] else None,
            insecure_skip_tls_verify=request["insecure_skip_tls_verify"],
            platform=request["platform"],
        )
        return None
    if operation == "sync_files":
        adapter.sync_files(
            store,
            temp,
            [(Path(path), name) for path, name in request["files"]],
            retries=request["retries"],
            exclude_extras=request["exclude_extras"],
        )
        return None
    if operation == "inventory":
        return adapter.inventory(store, temp, check=request["check"])
    adapter.save(store, temp, Path(request["haul"]), chunk_size=request["chunk_size"], containerd=request["containerd"])
    return None


def _worker_acquire_rcc(request: Mapping[str, Any]) -> dict[str, str]:
    from jat.models import EnvironmentArtifactMetadata
    from jat.process import ProcessRunner
    from jat.rcc_artifacts import EXPECTED_RCC_VERSION, RCCArtifactAdapter
    from jat.services import _source_robot_path

    archive = Path(request["archive"])
    metadata_path = Path(request["metadata"])
    workspace = Path(request["workspace"])
    robot_file = Path(request["robot_file"])
    try:
        if (
            archive.is_symlink()
            or metadata_path.is_symlink()
            or not archive.is_file()
            or not metadata_path.is_file()
            or metadata_path.stat().st_size > 1024 * 1024
        ):
            raise ManagedHaulerError("RCC component input is invalid")
        source = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            raise ManagedHaulerError("RCC component metadata is invalid")
        required = {
            "format_version", "artifact_digest", "specification_digest", "legacy_blueprint_key",
            "archive_sha256", "archive_size", "rcc_version", "platform", "robot_relative_path",
        }
        if required - source.keys():
            raise ManagedHaulerError("RCC component metadata is incomplete")
        allowed = {
            "format_version", "source_input_sha256", "artifact_digest", "specification_digest",
            "legacy_blueprint_key", "archive_sha256", "archive_size", "rcc_version", "platform",
            "robot_relative_path",
        }
        if (
            set(source) - allowed
            or type(source.get("format_version")) is not int
            or source.get("format_version") != 1
            or type(source.get("archive_size")) is not int
            or source.get("archive_size") <= 0
        ):
            raise ManagedHaulerError("RCC component metadata is invalid")
        if source.get("source_input_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", source["source_input_sha256"]):
            raise ManagedHaulerError("RCC component metadata is invalid")
        archive_digest = _sha256_file(archive)
        if archive.stat().st_size != source.get("archive_size") or archive_digest != source.get("archive_sha256"):
            raise ManagedHaulerError("RCC component archive does not match its metadata")
        private_paths.verify_private_path(archive, directory=False)
        private_paths.verify_private_path(metadata_path, directory=False)
        relative_robot = Path(source["robot_relative_path"])
        resolved_robot = _source_robot_path(workspace, relative_robot)
        if resolved_robot.resolve(strict=True) != robot_file.resolve(strict=True):
            raise ManagedHaulerError("RCC robot path does not match the selected workspace")
        native_platform = normalize_jat_platform(source["platform"])
        expected = EnvironmentArtifactMetadata.model_validate(
            {
                "artifact": source["artifact_digest"],
                "specification_digest": source["specification_digest"],
                "legacy_blueprint_key": source["legacy_blueprint_key"],
                "archive": archive,
                "archive_sha256": archive_digest,
                "archive_size": archive.stat().st_size,
                "rcc_version": source["rcc_version"],
                "platform": native_platform,
                "robot": relative_robot,
                "provider": "local",
                "acquired": False,
            }
        )
        if expected.rcc_version != EXPECTED_RCC_VERSION:
            raise ManagedHaulerError("RCC component version is unsupported")
        executable = os.environ.get("JOSH_ROOM_RCC_EXE")
        private_home = os.environ.get("ROBOCORP_HOME")
        if not executable or not private_home:
            raise ManagedHaulerError("managed RCC runtime is unavailable")
        private_paths.verify_private_path(Path(private_home), directory=True)
        rcc = RCCArtifactAdapter(
            ProcessRunner(),
            executable=executable,
            timeout=min(_hauler_timeout(), 600),
        )
        acquired = rcc.acquire(
            archive,
            resolved_robot,
            expected.rcc_version,
            expected.specification_digest,
            expected.legacy_blueprint_key,
            artifact_digest=expected.artifact,
            expected_platform=expected.platform,
            runtime_home=Path(private_home),
            strict_identity=True,
        )
        rcc.verify(resolved_robot)
        if (
            acquired.artifact != expected.artifact
            or acquired.specification_digest != expected.specification_digest
            or acquired.legacy_blueprint_key != expected.legacy_blueprint_key
            or acquired.platform != expected.platform
        ):
            raise ManagedHaulerError("RCC acquire verification did not match saved metadata")
        return {
            "artifact": acquired.artifact,
            "specification_digest": acquired.specification_digest,
            "platform": acquired.platform,
        }
    except ManagedHaulerError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        raise ManagedHaulerError("native RCC verification failed") from None


def normalize_jat_platform(value: Any) -> str:
    mapped = JAT_PLATFORM_MAP.get(value)
    if not isinstance(mapped, str) or not re.fullmatch(r"[a-z0-9]+_[a-z0-9]+", mapped):
        raise ManagedHaulerError("RCC component platform is invalid")
    return mapped


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_worker_result(path: Path, value: Mapping[str, Any]) -> None:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(body) > MAX_RESULT_BYTES:
        raise ManagedHaulerError("managed Hauler result exceeds its size limit")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        private_paths.secure_private_file(descriptor, temporary)
        with os.fdopen(descriptor, "wb") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        private_paths.verify_private_path(path, directory=False)
    finally:
        temporary.unlink(missing_ok=True)


def _worker_main(request_path: Path, result_path: Path) -> int:
    request = _worker_request(request_path)
    operation = request["operation"]
    try:
        value = _worker_value(request)
        body = {
            "format_version": 1,
            "operation": operation,
            "success": True,
            "exit_status": 0,
            "value": value,
            "error": None,
        }
        status = 0
    except (OSError, RuntimeError, TypeError, ValueError):
        body = {
            "format_version": 1,
            "operation": operation,
            "success": False,
            "exit_status": 1,
            "value": None,
            "error": "native Hauler operation failed",
        }
        status = 1
    try:
        _write_worker_result(result_path, body)
    except (OSError, RuntimeError, TypeError, ValueError):
        return 2
    return status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args(argv)
    if not args.worker or args.request_file is None or args.result_file is None:
        return 2
    try:
        return _worker_main(args.request_file, args.result_file)
    except ManagedHaulerError:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
