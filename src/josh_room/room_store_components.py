"""Native RCC environment component capture for Room Store saves."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from .cancellation import terminate_owned_process

RCC_VERSION = "v18.19.5"
MAX_SOURCE_FILE = 2 * 1024 * 1024
MAX_SOURCE_FILES = 64
MAX_JSON = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RoomStoreComponentError(RuntimeError):
    """Path-free component capture failure."""


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 256
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise RoomStoreComponentError("RCC environment source reference is invalid")
    return path.as_posix()


def _read_regular(root: Path, relative: str) -> bytes:
    relative = _safe_relative(relative)
    path = root.joinpath(*relative.split("/"))
    try:
        current = root
        for part in relative.split("/")[:-1]:
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise RoomStoreComponentError("RCC environment source is unsafe")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > MAX_SOURCE_FILE:
            raise RoomStoreComponentError("RCC environment source is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ):
                raise RoomStoreComponentError("RCC environment source changed during capture")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                data = source.read(MAX_SOURCE_FILE + 1)
            after = os.fstat(descriptor)
            if len(data) != before.st_size or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ):
                raise RoomStoreComponentError("RCC environment source changed during capture")
            return data
        finally:
            os.close(descriptor)
    except RoomStoreComponentError:
        raise
    except OSError:
        raise RoomStoreComponentError("RCC environment source is unavailable") from None


def _source_files(root: Path) -> dict[str, bytes]:
    """Read robot.yaml and its bounded RCC conda/pip file closure."""
    files: dict[str, bytes] = {}
    pending = ["robot.yaml"]
    while pending:
        relative = pending.pop()
        if relative in files:
            continue
        if len(files) >= MAX_SOURCE_FILES:
            raise RoomStoreComponentError("RCC environment source closure is too large")
        data = _read_regular(root, relative)
        files[relative] = data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise RoomStoreComponentError("RCC environment source is invalid") from None
        # These are RCC's file-valued robot environment keys and pip's -r
        # includes. Other source is embedded in the files already hashed.
        for match in re.finditer(r"(?m)^\s*(?:condaConfigFile|pipConfigFile)\s*:\s*(['\"]?)([^\s'\"]+)\1\s*(?:#.*)?$", text):
            child = match.group(2)
            if child.startswith(("http://", "https://", "file://")):
                raise RoomStoreComponentError("RCC environment source reference is unsupported")
            pending.append(_safe_relative(child))
        for match in re.finditer(r"(?m)^\s*-\s*(?:-requirement\s+|-r\s+)([^\s#]+)", text):
            child = match.group(1)
            if child.startswith(("http://", "https://", "file://")):
                raise RoomStoreComponentError("RCC environment source reference is unsupported")
            parent = PurePosixPath(relative).parent
            pending.append(_safe_relative((parent / child).as_posix()))
    return files


def source_input_sha256(root: Path, rcc_version: str = RCC_VERSION) -> str:
    """Hash only canonical RCC source inputs; never expose source paths/content."""
    if rcc_version != RCC_VERSION:
        raise RoomStoreComponentError("selected RCC version is unsupported")
    files = _source_files(Path(root).resolve(strict=True))
    digest = hashlib.sha256()
    digest.update(b"josh-room-rcc-inputs-v1\0")
    digest.update(rcc_version.encode("ascii") + b"\0")
    digest.update(_host_platform().encode("ascii") + b"\0")
    for relative in sorted(files):
        name = relative.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(files[relative]).to_bytes(8, "big"))
        digest.update(files[relative])
    return digest.hexdigest()


def _host_platform() -> str:
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        raise RoomStoreComponentError("selected RCC platform is unsupported")
    if sys.platform == "linux":
        return "linux-x64"
    if sys.platform == "win32":
        return "win32-x64"
    raise RoomStoreComponentError("selected RCC platform is unsupported")


def _run(argv: list[str], *, cwd: Path, cancellation: Any, timeout: float = 1800) -> bytes:
    if cancellation is not None and cancellation.cancelled:
        raise RoomStoreComponentError("RCC environment capture was cancelled")
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
        except OSError:
            raise RoomStoreComponentError("RCC environment capture is unavailable") from None
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancellation is not None and cancellation.cancelled:
                    terminate_owned_process(process)
                    raise RoomStoreComponentError("RCC environment capture was cancelled")
                if time.monotonic() >= deadline:
                    terminate_owned_process(process)
                    raise RoomStoreComponentError("RCC environment capture timed out")
                if output.tell() > MAX_JSON:
                    terminate_owned_process(process)
                    raise RoomStoreComponentError("RCC returned oversized environment metadata")
                time.sleep(0.02)
            output.seek(0)
            raw = output.read(MAX_JSON + 1)
            if process.returncode != 0 or len(raw) > MAX_JSON:
                raise RoomStoreComponentError("RCC environment capture failed")
            return raw
        except BaseException:
            if process.poll() is None:
                terminate_owned_process(process)
            raise


def _rcc_value(value: Mapping[str, Any], *names: str) -> str:
    for name in names:
        item = value.get(name)
        if isinstance(item, str) and item:
            return item
    raise RoomStoreComponentError("RCC returned incomplete environment metadata")


def _publish_metadata(raw: bytes) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RoomStoreComponentError("RCC returned invalid environment metadata") from None
    if not isinstance(value, dict):
        raise RoomStoreComponentError("RCC returned invalid environment metadata")
    artifact = _rcc_value(value, "artifactDigest", "artifact_digest", "artifact")
    specification = _rcc_value(value, "specificationDigest", "specification_digest")
    artifact = artifact if artifact.startswith("sha256:") else f"sha256:{artifact}"
    specification = specification if specification.startswith("sha256:") else f"sha256:{specification}"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact) or not re.fullmatch(r"sha256:[0-9a-f]{64}", specification):
        raise RoomStoreComponentError("RCC returned invalid environment metadata")
    platform_value = _rcc_value(value, "platform", "platformId", "platform_id")
    legacy_key = _rcc_value(value, "legacyBlueprintKey", "legacy_blueprint_key")
    return {
        "artifact_digest": artifact,
        "specification_digest": specification,
        "platform": platform_value,
        "legacy_blueprint_key": legacy_key,
    }


def capture_rcc_component(
    *,
    workspace: Path,
    prior_component: Mapping[str, Any] | None,
    repository_id: str,
    repository_format: int,
    restic: Any,
    cancellation: Any = None,
    rcc_runtime: Path | str | None = None,
) -> dict[str, Any] | None:
    """Reuse or capture the root robot's RCCA into the opened Room Store."""
    root = Path(workspace).resolve(strict=True)
    robot = root / "robot.yaml"
    if not robot.exists():
        return None
    if repository_format != 2 or not _SHA256.fullmatch(repository_id):
        raise RoomStoreComponentError("Room Store repository identity is invalid")
    executable = str(rcc_runtime) if rcc_runtime is not None else shutil.which("rcc")
    if not executable:
        raise RoomStoreComponentError("selected RCC runtime is unavailable")
    current_input = source_input_sha256(root)
    with tempfile.TemporaryDirectory(prefix="josh-room-rcc-") as temporary:
        private = Path(temporary)
        stage = private / "component"
        stage.mkdir(mode=0o700)
        if prior_component is not None:
            snapshot = prior_component.get("snapshot")
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("repository_id") != repository_id
                or snapshot.get("repository_format") != repository_format
                or not _SHA256.fullmatch(str(snapshot.get("snapshot_id", "")))
            ):
                raise RoomStoreComponentError("prior RCC component belongs to another Room Store")
            if (
                prior_component.get("source_input_sha256") == current_input
                and prior_component.get("rcc_version") == RCC_VERSION
                and prior_component.get("platform") == _host_platform()
            ):
                return dict(prior_component)
        version_output = _run([executable, "--version"], cwd=root, cancellation=cancellation, timeout=20)
        if re.search(r"(?<![A-Za-z0-9.+-])v18\.19\.5(?![A-Za-z0-9.+-])", version_output.decode("utf-8", "replace")) is None:
            raise RoomStoreComponentError("selected RCC version is unsupported")
        published = _run([executable, "env", "publish", "--robot", str(robot), "--json"], cwd=root, cancellation=cancellation)
        native = _publish_metadata(published)
        if native["platform"] != _host_platform():
            raise RoomStoreComponentError("RCC returned a mismatched environment platform")
        archive = stage / "rcc-environment.rcca"
        _run([executable, "env", "export", "--artifact", native["artifact_digest"], "--output", str(archive)], cwd=root, cancellation=cancellation)
        try:
            archive_stat = archive.lstat()
            if not stat.S_ISREG(archive_stat.st_mode) or stat.S_ISLNK(archive_stat.st_mode) or archive_stat.st_size <= 0:
                raise RoomStoreComponentError("RCC environment export is invalid")
            archive_digest = hashlib.sha256()
            with archive.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    archive_digest.update(chunk)
            if source_input_sha256(root) != current_input:
                raise RoomStoreComponentError("RCC environment source changed during capture")
            metadata_value = {
                "format_version": 1,
                "source_input_sha256": current_input,
                **native,
                "rcc_version": RCC_VERSION,
                "robot_relative_path": "robot.yaml",
                "archive_sha256": archive_digest.hexdigest(),
                "archive_size": archive_stat.st_size,
            }
            metadata_file = stage / "metadata.json"
            metadata_file.write_text(json.dumps(metadata_value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            metadata_file.chmod(0o600)
            if prior_component is None:
                parent = None
            else:
                parent = prior_component["snapshot"]["snapshot_id"]
            summary = restic.backup(stage, parent=parent, cancellation=cancellation)
            snapshot_id = getattr(summary, "snapshot_id", None)
            if not snapshot_id:
                raise RoomStoreComponentError("Restic did not capture the RCC component")
            saved = restic.snapshot(snapshot_id)
            return {
                "kind": "rcca",
                "snapshot": {
                    "repository_id": repository_id,
                    "repository_format": repository_format,
                    "snapshot_id": snapshot_id,
                    "tree_id": saved.tree_id,
                },
                "archive_sha256": archive_digest.hexdigest(),
                "archive_size": archive_stat.st_size,
                "member_basename": "rcc-environment.rcca",
                "artifact_digest": native["artifact_digest"],
                "specification_digest": native["specification_digest"],
                "platform": native["platform"],
                "rcc_version": RCC_VERSION,
                "robot_relative_path": "robot.yaml",
                "source_input_sha256": current_input,
            }
        except OSError:
            raise RoomStoreComponentError("RCC environment capture failed") from None
