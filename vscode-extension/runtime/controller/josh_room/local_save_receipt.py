"""Private, non-secret evidence for local Save preflight.

Fresh processes still verify current metadata. A persisted signature alone is
never evidence of event continuity or current remote snapshot availability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)
from .workspace_state import _validate_workspace_marker

_LIMIT = 128 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current_producer() -> dict:
    release = os.environ.get("JOSH_ROOM_EXTENSION_VERSION") or version("josh-room")
    if os.name == "nt":
        platform = "win32-x64"
    elif os.name == "posix" and os.uname().machine in {"x86_64", "amd64"}:
        platform = "linux-x64"
    else:
        raise ValueError("unsupported local Save metadata")
    return {
        "josh_room_version": release,
        "restic_version": "0.19.1",
        "source_platform": platform,
        "restore_platforms": [platform],
    }


def receipt_path(instance: Path, source: Path) -> Path:
    canonical = str(Path(source).resolve(strict=True))
    return (
        Path(instance)
        / "save-receipts"
        / (hashlib.sha256(canonical.encode()).hexdigest() + ".json")
    )


def _outside_workspace(path: Path, source: Path) -> None:
    if path.resolve().is_relative_to(source.resolve(strict=True)):
        raise ValueError("local Save receipt must be outside the workspace")


def _read_json(path: Path, *, private: bool) -> dict:
    if private:
        verify_private_path(path.parent, directory=True)
        verify_private_path(path, directory=False)
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > _LIMIT
        or getattr(metadata, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("unsafe local Save evidence")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("local Save evidence changed")
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError("local Save evidence is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("invalid local Save evidence")
    return value


def _marker(source: Path) -> dict:
    marker = _validate_workspace_marker(
        _read_json(source / ".josh-room.json", private=False)
    )
    if marker.get("format_version") != 3:
        raise ValueError("local Save evidence requires a v3 marker")
    if (
        marker["workspace_path_sha256"]
        != hashlib.sha256(str(source.resolve(strict=True)).encode()).hexdigest()
    ):
        raise ValueError("local Save evidence belongs to another workspace")
    return marker


def _binding(instance: Path, dimension: Any) -> str:
    return _digest(
        {
            "instance": hashlib.sha256(
                str(Path(instance).resolve()).encode()
            ).hexdigest(),
            "dimension": dimension.to_private(),
            "dimension_id": dimension.dimension_id,
        }
    )


def _rcc_input(source: Path, components: dict) -> dict | None:
    if not isinstance(components, dict) or set(components) != {
        "rcc_environment",
        "homebrew_recovery",
        "hauler_content",
    }:
        raise TypeError("invalid local component evidence")
    if (
        components.get("homebrew_recovery") is not None
        or components.get("hauler_content") is not None
    ):
        raise ValueError("external component input requires live verification")
    rcc = components.get("rcc_environment")
    if rcc is None:
        if (source / "robot.yaml").exists():
            raise ValueError("RCC component selection changed")
        return None
    from .room_store_components import RCC_VERSION, source_input_sha256

    source_hash = source_input_sha256(source)
    if (
        rcc.get("rcc_version") != RCC_VERSION
        or rcc.get("source_input_sha256") != source_hash
    ):
        raise ValueError("RCC component input changed")
    executable = os.environ.get("JOSH_ROOM_RCC_EXE")
    if not executable:
        raise ValueError("RCC runtime identity is unavailable")
    metadata = Path(executable).stat()
    change_time = metadata.st_ctime_ns
    if os.name == "nt":
        from .windows_file_metadata import change_time_ns

        change_time = change_time_ns(Path(executable), metadata)
    return {
        "source_input_sha256": source_hash,
        "runtime_metadata": [
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            change_time,
        ],
    }


def invalidate(instance: Path, source: Path) -> None:
    """Invalidate only safe application-owned evidence; never touch workspace data."""
    try:
        path = receipt_path(instance, source)
        _outside_workspace(path.parent, source)
        verify_private_path(path.parent, directory=True)
        verify_private_path(path, directory=False)
        path.unlink()
    except (OSError, RuntimeError, ValueError):
        return


def write_verified_receipt(
    instance: Path,
    source: Path,
    dimension: Any,
    descriptor: dict,
    result: dict,
    *,
    restored: bool = False,
) -> bool:
    """Called only after clean committed Save or verified promoted Hydrate."""
    temporary = None
    try:
        if (
            result.get("ok") is not True
            or (not restored and result.get("status") != "saved")
            or result.get("status") in {"saved-but-dirty", "cancelled", "unknown"}
            or result.get("cancelled")
            or result.get("saved_but_dirty")
            or result.get("publication_state")
            in {"uncertain", "committed-verification-unknown", "committed-marker-stale"}
        ):
            return False
        source = Path(source)
        path = receipt_path(instance, source)
        _outside_workspace(path.parent, source)
        marker = _marker(source)
        producer = current_producer()
        if (
            dimension.provider != "minio"
            or descriptor["producer"] != producer
            or marker["project_id"] != descriptor["room_id"]
            or descriptor["dimension_id"] != marker["dimension_id"]
            or descriptor["encryption_domain_id"] != marker["encryption_domain_id"]
            or marker["dimension_id"] != dimension.dimension_id
            or marker["encryption_domain_id"] != dimension.encryption_domain_id
            or marker["snapshot_id"] != descriptor["logical_jat_id"]
            or descriptor["source"] != {}
            or descriptor.get("origin_room_id") is not None
            or any(
                result.get(field) != marker[field]
                for field in (
                    "workspace_signature",
                    "signature_algorithm",
                    "capture_policy_sha256",
                    "snapshot_id",
                    "project_id",
                    "dimension_id",
                    "encryption_domain_id",
                )
            )
        ):
            return False
        components = descriptor["components"]
        rcc = _rcc_input(source, components)
        workspace = descriptor["workspace"]
        if result.get("workspace_snapshot_id") != workspace.get("snapshot_id"):
            return False
        if any(
            not isinstance(workspace.get(field), str)
            or _DIGEST.fullmatch(workspace[field]) is None
            for field in ("repository_id", "snapshot_id", "tree_id")
        ):
            return False
        receipt = {
            "format_version": 1,
            "binding_sha256": _binding(instance, dimension),
            "marker": marker,
            "producer": producer,
            "components": components,
            "rcc_input": rcc,
            "source_sha256": _digest(descriptor["source"]),
            "workspace": {
                field: workspace[field]
                for field in ("repository_id", "snapshot_id", "tree_id")
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_directory(path.parent)
        fd, name = tempfile.mkstemp(prefix=".save.", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        protect_private_file(temporary)
        os.replace(temporary, path)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_noop(
    instance: Path,
    source: Path,
    dimension: Any,
    project_id: str,
    *,
    scan_sink: dict | None = None,
) -> dict | None:
    """Verify current local metadata before any auth/provider/runtime work."""
    try:
        source = Path(source)
        path = receipt_path(instance, source)
        _outside_workspace(path.parent, source)
        receipt = _read_json(path, private=True)
        marker = _marker(source)
        if (
            dimension.provider != "minio"
            or receipt.get("format_version") != 1
            or receipt.get("binding_sha256") != _binding(instance, dimension)
            or receipt.get("marker") != marker
            or receipt.get("producer") != current_producer()
            or receipt.get("source_sha256") != _digest({})
            or marker["project_id"] != project_id
            or marker["dimension_id"] != dimension.dimension_id
            or marker["encryption_domain_id"] != dimension.encryption_domain_id
            or os.environ.get(
                "JOSH_ROOM_SELECTED_DOMAIN", dimension.encryption_domain_id
            )
            != dimension.encryption_domain_id
            or _rcc_input(source, receipt["components"]) != receipt["rcc_input"]
        ):
            return None
        workspace = receipt["workspace"]
        if not isinstance(workspace, dict) or any(
            not isinstance(workspace.get(field), str)
            or _DIGEST.fullmatch(workspace[field]) is None
            for field in ("repository_id", "snapshot_id", "tree_id")
        ):
            return None
        from .room_store_operations import scan_workspace_for_status

        # A fresh process has no event continuity. Recompute metadata, never
        # trust a persisted signature or a directory mtime as current evidence.
        scan = scan_workspace_for_status(source)
        unchanged = (
            scan.signature,
            scan.signature_algorithm,
            scan.capture_policy_sha256,
        ) == (
            marker["workspace_signature"],
            marker["signature_algorithm"],
            marker["capture_policy_sha256"],
        )
        if _marker(source) != marker:
            return None
        from .workspace_policy import load_capture_policy

        runtime = (
            Path(os.environ["ROBOCORP_HOME"])
            if os.environ.get("ROBOCORP_HOME")
            else None
        )
        policy = load_capture_policy(
            source,
            active_runtime_root=runtime if runtime and runtime.is_dir() else None,
        )
        if (
            policy.sha256 != scan.capture_policy_sha256
            or _rcc_input(source, receipt["components"]) != receipt["rcc_input"]
        ):
            return None
        if not unchanged:
            if scan_sink is not None:
                scan_sink["workspace_evidence"] = (
                    marker["workspace_path_sha256"],
                    scan,
                )
            return None
        return {
            "ok": True,
            "status": "already-saved",
            "dimension_id": marker["dimension_id"],
            "encryption_domain_id": marker["encryption_domain_id"],
            "project_id": project_id,
            "snapshot_id": marker["snapshot_id"],
            "workspace_snapshot_id": receipt["workspace"]["snapshot_id"],
            "workspace_signature": scan.signature,
            "signature_algorithm": scan.signature_algorithm,
            "capture_policy_sha256": scan.capture_policy_sha256,
            "display_name": marker["display_name"],
            "data_added_bytes": 0,
            "scanned_bytes": scan.logical_bytes,
            "metadata_entries_visited": len(scan.paths),
            "local_verification": "current-metadata",
            "provider_verification": "last-verified-save",
            "has_external_components": False,
        }
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, PrivatePathError):
        return None
