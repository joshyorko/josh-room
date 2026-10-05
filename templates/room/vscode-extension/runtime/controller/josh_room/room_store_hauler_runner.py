"""Managed adapter that runs JAT's native Hauler API in its selected artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import jat, private_paths

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 4096
_OPERATIONS = {"sync", "sync_image_txt", "sync_files", "inventory", "save", "acquire_rcc", "local_images", "validate_brew_archive", "manifest_inputs", "verify_local_images"}
_VERSION = re.compile(r"(?<![A-Za-z0-9])v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?(?![A-Za-z0-9])")
JAT_PLATFORM_MAP = {"linux-x64": "linux_amd64", "win32-x64": "windows_amd64"}
MAX_HAUL_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
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
        home = Path(private_home)
        metadata = home.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ManagedHaulerError("managed JAT private home is unsafe")
        if os.name == "nt":
            api = private_paths._windows_api()
            owner, _protected, _aces = api.read_security(home)
            if owner != api.current_user_sid():
                raise ManagedHaulerError("managed JAT private home is unsafe")
        elif metadata.st_uid != os.getuid():
            raise ManagedHaulerError("managed JAT private home is unsafe")
        private_paths.protect_private_directory(home)
        private_paths.verify_private_path(home, directory=True)
    except (OSError, private_paths.PrivatePathError):
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
        request = self._store_temp(store, temp)
        request.update(
            {
                "files": [[_absolute_path(path), name] for path, name in files],
                "retries": retries,
                "exclude_extras": exclude_extras,
                **({"images": list(images)} if images else {}),
            }
        )
        return self._invoke("sync_files", request)

    def local_images(self, images: Sequence[str] = (), *, all_images: bool = False) -> list[tuple[str, str]]:
        value = self._invoke("local_images", {"images": list(images), "all_images": all_images})
        if not isinstance(value, list) or len(value) > MAX_ITEMS or any(
            not isinstance(row, list) or len(row) != 2
            or not isinstance(row[0], str) or not isinstance(row[1], str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", row[1]) is None
            for row in value
        ):
            raise ManagedHaulerError("local image inventory is invalid")
        return [(name, identity) for name, identity in value]

    def validate_brew_archive(self, archive: Path) -> None:
        self._invoke("validate_brew_archive", {"archive": _absolute_path(archive)})

    def manifest_inputs(self, manifests, staging=None):
        value = self._invoke("manifest_inputs", {
            "manifests": [_absolute_path(path) for path in manifests],
            "staging": _absolute_path(staging) if staging is not None else None,
        })
        if (not isinstance(value, dict) or set(value) != {"sha256", "manifests", "local_images", "fully_pinned"}
                or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
                or type(value["fully_pinned"]) is not bool
                or not isinstance(value["local_images"], list)
                or any(not isinstance(row, list) or len(row) != 2
                       or not isinstance(row[0], str) or not row[0]
                       or not isinstance(row[1], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", row[1])
                       for row in value["local_images"])
                or not isinstance(value["manifests"], list) or len(value["manifests"]) != len(manifests)):
            raise ManagedHaulerError("manifest input evidence is invalid")
        if staging is not None:
            root = Path(staging).resolve(strict=True)
            for path in value["manifests"]:
                Path(path).resolve(strict=True).relative_to(root)
        return value

    def verify_local_images(self, store, images):
        self._invoke("verify_local_images", {"store": _absolute_path(store), "images": [list(row) for row in images]})

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
    if operation == "manifest_inputs":
        if set(request) != {"format_version", "operation", "manifests", "staging"}:
            raise ManagedHaulerError("managed manifest request is invalid")
        _validate_strings(request["manifests"], MAX_ITEMS)
        if request["staging"] is not None and (not isinstance(request["staging"], str) or not request["staging"]):
            raise ManagedHaulerError("managed manifest request is invalid")
        return request
    if operation == "verify_local_images":
        if (set(request) != {"format_version", "operation", "store", "images"}
                or not isinstance(request["store"], str) or not request["store"]
                or not isinstance(request["images"], list) or len(request["images"]) > MAX_ITEMS
                or any(not isinstance(row, list) or len(row) != 2
                       or not isinstance(row[0], str) or not row[0]
                       or not isinstance(row[1], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", row[1])
                       for row in request["images"])):
            raise ManagedHaulerError("managed local image evidence is invalid")
        return request
    required = {
        "sync": {"format_version", "operation", "store", "temp", "manifests", "retries", "exclude_extras", "concurrency", "ca_file", "insecure_skip_tls_verify"},
        "sync_image_txt": {"format_version", "operation", "store", "temp", "sources", "retries", "exclude_extras", "concurrency", "ca_file", "insecure_skip_tls_verify", "platform"},
        "sync_files": {"format_version", "operation", "store", "temp", "files", "retries", "exclude_extras"},
        "inventory": {"format_version", "operation", "store", "temp", "check"},
        "save": {"format_version", "operation", "store", "temp", "haul", "chunk_size", "containerd"},
        "acquire_rcc": {"format_version", "operation", "archive", "metadata", "workspace", "robot_file"},
        "local_images": {"format_version", "operation", "images", "all_images"},
        "validate_brew_archive": {"format_version", "operation", "archive"},
    }[operation]
    if operation == "sync_files" and "images" in request:
        required = required | {"images"}
    if set(request) != required:
        raise ManagedHaulerError("managed Hauler request is invalid")
    if operation == "local_images":
        _validate_strings(request["images"], MAX_ITEMS)
        if type(request["all_images"]) is not bool or request["images"] and request["all_images"]:
            raise ManagedHaulerError("local image selection is invalid")
        return request
    path_fields = (
        ("archive", "metadata", "workspace", "robot_file")
        if operation == "acquire_rcc"
        else ("archive",) if operation == "validate_brew_archive" else ("store", "temp")
    )
    for field in path_fields:
        if not isinstance(request[field], str) or not request[field] or len(request[field]) > 4096:
            raise ManagedHaulerError("managed Hauler request is invalid")
    if operation in {"acquire_rcc", "validate_brew_archive"}:
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
        _validate_strings(request.get("images", []), MAX_ITEMS)
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
    if operation == "manifest_inputs":
        from .room_store_manifest_inputs import prepare_manifest_inputs

        return prepare_manifest_inputs(request["manifests"], request["staging"],
                                       lambda images: _worker_local_images({"images": images, "all_images": False}))
    if operation == "verify_local_images":
        _verify_local_image_configs(Path(request["store"]), request["images"])
        return None
    if operation == "acquire_rcc":
        return _worker_acquire_rcc(request)
    if operation == "local_images":
        return _worker_local_images(request)
    if operation == "validate_brew_archive":
        return _worker_validate_brew_archive(request)
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
        images = request.get("images", [])
        if request["files"] or os.name == "nt":
            adapter.sync_files(
                store, temp, [(Path(path), name) for path, name in request["files"]],
                retries=request["retries"], exclude_extras=request["exclude_extras"],
                **({"images": images} if images and os.name == "nt" else {}),
            )
        if images and os.name != "nt":
            manifest = temp / "local-images.json"
            manifest.write_text(json.dumps({"apiVersion": "content.hauler.cattle.io/v1", "kind": "Images",
                                           "metadata": {"name": "room-local-images"},
                                           "spec": {"images": [{"name": name, "local": True} for name in images]}}))
            private_paths.protect_private_file(manifest)
            adapter.sync(store, temp, str(manifest), retries=request["retries"], exclude_extras=request["exclude_extras"])
        return None
    if operation == "inventory":
        return adapter.inventory(store, temp, check=request["check"])
    _bounded_hauler_save(adapter, store, temp, Path(request["haul"]),
                         chunk_size=request["chunk_size"], containerd=request["containerd"])
    return None


def _bounded_hauler_save(adapter, store, temp, haul, **options):
    import shutil
    import subprocess
    import threading

    from .cancellation import terminate_owned_process

    estimate = 0
    for directory, dirs, files in os.walk(store, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ManagedHaulerError("Hauler output source is unsafe")
        for name in files:
            metadata = (Path(directory) / name).lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ManagedHaulerError("Hauler output source is unsafe")
            estimate += metadata.st_size + 4096
    estimate += estimate // 100 + 64 * 1024 * 1024
    if estimate > MAX_HAUL_OUTPUT_BYTES:
        raise ManagedHaulerError("Hauler output exceeds its archive budget")
    if shutil.disk_usage(haul.parent).free < estimate + 256 * 1024 * 1024:
        raise ManagedHaulerError("Hauler output requires more free disk space")
    finished = threading.Event()
    exceeded = threading.Event()
    owned = []
    native_popen = subprocess.Popen

    def start_owned(*args, **kwargs):
        process = native_popen(*args, **kwargs)
        owned.append(process)
        return process

    def monitor():
        while not finished.wait(0.05):
            try:
                unsafe = haul.exists() and haul.stat().st_size > MAX_HAUL_OUTPUT_BYTES
                unsafe |= shutil.disk_usage(haul.parent).free < 64 * 1024 * 1024
            except OSError:
                unsafe = True
            if unsafe:
                exceeded.set()
                for process in tuple(owned):
                    if process.poll() is None:
                        terminate_owned_process(process)
                return

    resource = None
    previous = None
    if os.name == "posix":
        import resource

        previous = resource.getrlimit(resource.RLIMIT_FSIZE)
        soft = MAX_HAUL_OUTPUT_BYTES if previous[0] == resource.RLIM_INFINITY else min(previous[0], MAX_HAUL_OUTPUT_BYTES)
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, previous[1]))
    watcher = threading.Thread(target=monitor, name="room-hauler-output-limit")
    subprocess.Popen = start_owned
    watcher.start()
    try:
        adapter.save(store, temp, haul, **options)
        if exceeded.is_set() or not haul.is_file() or haul.stat().st_size > MAX_HAUL_OUTPUT_BYTES:
            raise ManagedHaulerError("Hauler output exceeded its archive budget")
    finally:
        finished.set()
        watcher.join()
        subprocess.Popen = native_popen
        if resource is not None and previous is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, previous)


def _worker_local_images(request: Mapping[str, Any]) -> list[list[str]]:
    import shutil

    from jat.process import ProcessRunner

    from .room_store_hauler import _validate_image_reference

    docker = shutil.which("docker")
    if not docker:
        raise ManagedHaulerError("local Docker image capture requires Docker")
    runner = ProcessRunner()
    ready = runner.run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if not ready.success:
        raise ManagedHaulerError("local Docker image capture requires a reachable Docker daemon")
    images = request["images"]
    if request["all_images"]:
        listed = runner.run([docker, "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
        if not listed.success:
            raise ManagedHaulerError("local Docker image inventory is unavailable")
        images = [name for name in listed.stdout.splitlines() if "<none>" not in name]
    if len(images) > MAX_ITEMS:
        raise ManagedHaulerError("local Docker image inventory exceeds its limit")
    result = []
    for name in sorted(set(images)):
        name = _validate_image_reference(name)
        if name.startswith("-"):
            raise ManagedHaulerError("local image selection is invalid")
        observed = runner.run([docker, "image", "inspect", "--format", "{{.Id}}", name], timeout=30)
        identity = observed.stdout.strip()
        if not observed.success or re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
            raise ManagedHaulerError("a selected local image is unavailable")
        result.append([name, identity])
    return result


def _verify_local_image_configs(store: Path, images) -> None:
    def canonical(value):
        name = value.removeprefix("docker.io/")
        if "/" not in name:
            name = "index.docker.io/library/" + name
        elif "." not in name.split("/", 1)[0] and ":" not in name.split("/", 1)[0] and not name.startswith("localhost/"):
            name = "index.docker.io/" + name
        if ":" not in name.rsplit("/", 1)[-1] and "@" not in name:
            name += ":latest"
        return name

    def read_json(path, expected_digest=None):
        if any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent)):
            raise ManagedHaulerError("local image evidence is unsafe")
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_RESULT_BYTES:
            raise ManagedHaulerError("local image evidence is invalid")
        raw = path.read_bytes()
        if expected_digest is not None and hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ManagedHaulerError("local image manifest digest does not match")
        return json.loads(raw)

    try:
        index = read_json(store / "index.json")
        descriptors = index["manifests"]
        for name, identity in images:
            matches = [row for row in descriptors if canonical(row.get("annotations", {}).get("org.opencontainers.image.ref.name", "")) == canonical(name)]
            if len(matches) != 1:
                raise ManagedHaulerError("local image evidence is ambiguous or missing")
            digest = matches[0]["digest"]
            if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise ManagedHaulerError("local image manifest digest is invalid")
            manifest = read_json(store / "blobs" / "sha256" / digest[7:], digest[7:])
            if manifest.get("config", {}).get("digest") != identity:
                raise ManagedHaulerError("captured local image does not match its selected identity")
            read_json(store / "blobs" / "sha256" / identity[7:], identity[7:])
    except (OSError, KeyError, TypeError, ValueError):
        raise ManagedHaulerError("local image identity could not be verified") from None


@contextmanager
def _brew_decoded_stream(archive: Path):
    try:
        import zstandard
    except ModuleNotFoundError:
        import shutil
        import subprocess

        from .cancellation import terminate_owned_process

        executable = shutil.which("zstd")
        if executable is None:
            raise ManagedHaulerError("contained Homebrew decompressor is unavailable")
        process = subprocess.Popen([executable, "-dc", "--memory=128MB", "--", str(archive)],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt")

        class DecodedReader:
            def read(self, size):
                block = process.stdout.read(size)
                if not block and process.wait(timeout=30) != 0:
                    raise ManagedHaulerError("Homebrew archive decompression failed")
                return block

        try:
            yield DecodedReader()
        finally:
            if process.poll() is None:
                terminate_owned_process(process)
            process.stdout.close()
        return
    with archive.open("rb") as raw, zstandard.ZstdDecompressor(max_window_size=128 * 1024).stream_reader(raw) as decoded:
        yield decoded


def _preflight_brew_archive(archive: Path) -> None:
    import tarfile

    limit = 8 * 1024 * 1024 * 1024
    expanded = 0
    # Check extension-header sizes before tarfile can allocate their bodies.
    with _brew_decoded_stream(archive) as headers:
        scanned = 0
        header_count = 0
        while header := headers.read(512):
            scanned += len(header)
            if header == b"\0" * 512:
                break
            member = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
            header_count += 1
            if (header_count > 100_000 or member.size > limit
                    or member.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}
                    and member.size > 4 * 1024 * 1024):
                raise ManagedHaulerError("expanded Homebrew archive exceeds its validation limit")
            remaining = (member.size + 511) // 512 * 512
            while remaining:
                block = headers.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ManagedHaulerError("Homebrew archive is incomplete")
                remaining -= len(block)
                scanned += len(block)
                if scanned > limit:
                    raise ManagedHaulerError("expanded Homebrew archive exceeds its validation limit")

    class BoundedReader:
        def __init__(self, source):
            self.source = source
            self.read_bytes = 0

        def read(self, size):
            block = self.source.read(min(size, 1024 * 1024))
            self.read_bytes += len(block)
            if self.read_bytes > limit:
                raise ManagedHaulerError("expanded Homebrew archive exceeds its validation limit")
            return block

    with _brew_decoded_stream(archive) as decoded:
        reader = BoundedReader(decoded)
        count = 0
        with tarfile.open(fileobj=reader, mode="r|") as stream:
            for member in stream:
                count += 1
                expanded += member.size
                if count > 100_000 or expanded > limit:
                    raise ManagedHaulerError("expanded Homebrew archive exceeds its validation limit")
        while reader.read(1024 * 1024):
            pass
    import shutil

    if shutil.disk_usage(tempfile.gettempdir()).free < expanded + 64 * 1024 * 1024:
        raise ManagedHaulerError("Homebrew validation requires more free disk space")


def _worker_validate_brew_archive(request: Mapping[str, Any]) -> None:
    from jat.archive import ArchiveAdapter
    from jat.process import ProcessRunner
    from jat.safety import validate_archive_members
    from jat.services import _validate_brew_recovery

    archive = Path(request["archive"])
    metadata = archive.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 8 * 1024 * 1024 * 1024:
        raise ManagedHaulerError("Homebrew archive is not a bounded regular file")
    _preflight_brew_archive(archive)
    adapter = ArchiveAdapter(ProcessRunner())
    validate_archive_members(adapter.members(archive))
    with tempfile.TemporaryDirectory(prefix="josh-room-brew-validate-") as temporary:
        destination = Path(temporary) / "brew"
        destination.mkdir(mode=0o700)
        adapter.extract(archive, destination, strip_components=1)
        _validate_brew_recovery(destination)


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
