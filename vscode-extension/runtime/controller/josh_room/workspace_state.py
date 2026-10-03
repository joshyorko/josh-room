import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .workspace_policy import load_capture_policy

MARKER_NAME = ".josh-room.json"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_CONTEXT_MARKER_LIMIT = 64 * 1024


def _identifier(label: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"marker {label} is invalid")


def _digest(value: object, label: str) -> None:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError(f"marker {label} is invalid")


def canonical_workspace_path_sha256(workspace: Path) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()


def workspace_fingerprint(workspace: Path) -> str:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    runtime_value = os.environ.get("ROBOCORP_HOME")
    runtime = Path(runtime_value) if runtime_value else None
    capture_policy = load_capture_policy(
        root, active_runtime_root=runtime if runtime and runtime.is_dir() else None
    )
    digest = hashlib.sha256()
    def visit(directory: Path) -> None:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            if capture_policy.is_excluded(path.relative_to(root).as_posix()):
                continue
            relative = path.relative_to(root).as_posix().encode()
            metadata = entry.stat(follow_symlinks=False)
            mode = str(metadata.st_mode).encode()
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path).encode()
                fingerprint = b"link:" + mode + b":" + target
            elif stat.S_ISDIR(metadata.st_mode):
                fingerprint = b"directory:" + mode
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                content_digest = hashlib.sha256()
                content_digest.update(str(metadata.st_size).encode() + b":" + mode + b":")
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        content_digest.update(chunk)
                fingerprint = b"file:" + content_digest.hexdigest().encode()
            else:
                fingerprint = b"special:" + mode
            digest.update(relative + b"\0" + fingerprint + b"\n")

    visit(root)
    return digest.hexdigest()


def write_workspace_marker(
    workspace: Path,
    *,
    dimension_id: str,
    project_id: str,
    display_name: str,
    snapshot_id: str,
    workspace_fingerprint: str,
    path_binding: Path | None = None,
) -> dict:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for label, value in (("dimension_id", dimension_id), ("project_id", project_id), ("snapshot_id", snapshot_id)):
        _identifier(label, value)
    _digest(workspace_fingerprint, "workspace fingerprint")
    marker = {
        "format_version": 2,
        "dimension_id": dimension_id,
        "project_id": project_id,
        "display_name": display_name,
        "snapshot_id": snapshot_id,
        "workspace_fingerprint": workspace_fingerprint,
        "workspace_path_sha256": canonical_workspace_path_sha256(path_binding or workspace),
    }
    marker_path = workspace / MARKER_NAME
    fd, temp_name = tempfile.mkstemp(prefix=".josh-room.", dir=workspace)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(marker, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, marker_path)
    finally:
        temp.unlink(missing_ok=True)
    return marker


def write_stat_workspace_marker(
    workspace: Path,
    *,
    dimension_id: str,
    project_id: str,
    snapshot_id: str,
    encryption_domain_id: str,
    display_name: str,
    workspace_signature: str,
    signature_algorithm: str,
    capture_policy_sha256: str,
    path_binding: Path | None = None,
) -> dict:
    """Atomically write the local, non-authoritative stat baseline marker."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for label, value in (
        ("dimension_id", dimension_id),
        ("encryption_domain_id", encryption_domain_id),
        ("project_id", project_id),
        ("snapshot_id", snapshot_id),
    ):
        _identifier(label, value)
    if not isinstance(display_name, str) or not display_name:
        raise ValueError("marker display_name is invalid")
    _digest(workspace_signature, "workspace signature")
    _digest(capture_policy_sha256, "capture policy sha256")
    if signature_algorithm != "josh-room-stat-v1":
        raise ValueError("unsupported workspace signature algorithm")
    marker = {
        "format_version": 3,
        "dimension_id": dimension_id,
        "encryption_domain_id": encryption_domain_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "display_name": display_name,
        "workspace_path_sha256": canonical_workspace_path_sha256(path_binding or workspace),
        "workspace_signature": workspace_signature,
        "signature_algorithm": signature_algorithm,
        "capture_policy_sha256": capture_policy_sha256,
    }
    _write_marker_atomically(workspace, marker)
    return marker


def _write_marker_atomically(workspace: Path, marker: dict) -> None:
    marker_path = workspace / MARKER_NAME
    fd, temp_name = tempfile.mkstemp(prefix=".josh-room.", dir=workspace)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, marker_path)
    finally:
        temp.unlink(missing_ok=True)


def read_workspace_marker(workspace: Path) -> dict:
    path = Path(workspace) / MARKER_NAME
    if not path.is_file():
        raise ValueError("workspace marker is unavailable")
    try:
        marker = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("workspace marker is invalid") from error
    try:
        return _validate_workspace_marker(marker)
    except TypeError as error:
        raise ValueError(str(error)) from error


def _validate_workspace_marker(marker: object) -> dict:
    if not isinstance(marker, dict):
        raise TypeError("workspace marker is invalid")
    if marker.get("format_version") == 1:
        _identifier("project_id", marker.get("project_id"))
        if not isinstance(marker.get("display_name"), str) or not marker["display_name"]:
            raise ValueError("marker display_name is invalid")
        return marker
    if marker.get("format_version") != 2:
        if marker.get("format_version") != 3:
            raise ValueError("unsupported workspace marker format")
        required = {
            "format_version", "dimension_id", "encryption_domain_id",
            "project_id", "snapshot_id", "display_name",
            "workspace_path_sha256", "workspace_signature", "signature_algorithm",
            "capture_policy_sha256",
        }
        if set(marker) != required:
            raise ValueError("workspace marker v3 fields are invalid")
        for label in (
            "dimension_id", "encryption_domain_id", "project_id", "snapshot_id",
        ):
            _identifier(label, marker.get(label))
        for label in ("workspace_path_sha256", "workspace_signature", "capture_policy_sha256"):
            _digest(marker.get(label), label)
        if marker.get("signature_algorithm") != "josh-room-stat-v1":
            raise ValueError("unsupported workspace signature algorithm")
        if not isinstance(marker.get("display_name"), str) or not marker["display_name"]:
            raise ValueError("marker display_name is invalid")
        return marker
    for label in ("dimension_id", "project_id", "snapshot_id"):
        _identifier(label, marker.get(label))
    _digest(marker.get("workspace_fingerprint"), "workspace fingerprint")
    _digest(marker.get("workspace_path_sha256"), "workspace path sha256")
    if not isinstance(marker.get("display_name"), str) or not marker["display_name"]:
        raise ValueError("marker display_name is invalid")
    return marker


def context_status(workspace: Path) -> dict:
    """Read cheap, path-bound workspace context without scanning workspace contents."""
    result = {
        "format_version": 1,
        "ok": False,
        "state": "invalid",
        "linked": False,
        "path_matches": False,
    }
    marker_path = Path(workspace) / MARKER_NAME
    try:
        metadata = marker_path.lstat()
    except FileNotFoundError:
        return {**result, "ok": True, "state": "unlinked"}
    except OSError:
        return result
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _CONTEXT_MARKER_LIMIT:
        return result
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(marker_path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_size > _CONTEXT_MARKER_LIMIT
            ):
                return result
            data = os.read(descriptor, _CONTEXT_MARKER_LIMIT + 1)
        finally:
            os.close(descriptor)
        if len(data) > _CONTEXT_MARKER_LIMIT:
            return result
        marker = _validate_workspace_marker(json.loads(data))
        if marker.get("format_version") not in {2, 3}:
            return result
        path_matches = marker["workspace_path_sha256"] == canonical_workspace_path_sha256(Path(workspace))
        if not path_matches:
            return result
        return {
            "format_version": 1,
            "ok": True,
            "state": "linked",
            "linked": True,
            "path_matches": True,
            "dimension_id": marker["dimension_id"],
            "project_id": marker["project_id"],
            "display_name": marker["display_name"],
            **({"snapshot_id": marker["snapshot_id"]} if marker["format_version"] == 2 else {
                "encryption_domain_id": marker["encryption_domain_id"],
                "snapshot_id": marker["snapshot_id"],
                "workspace_signature": marker["workspace_signature"],
                "signature_algorithm": marker["signature_algorithm"],
                "capture_policy_sha256": marker["capture_policy_sha256"],
            }),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return result


def local_status(workspace: Path) -> dict:
    workspace = Path(workspace)
    try:
        marker = read_workspace_marker(workspace)
    except ValueError as error:
        return {"ok": False, "state": "unlinked", "workspace": str(workspace), "error": str(error)}
    current_path = canonical_workspace_path_sha256(workspace)
    if marker.get("format_version") == 3:
        try:
            from .room_store_operations import scan_workspace_for_status

            scan = scan_workspace_for_status(workspace)
        except (OSError, RuntimeError, ValueError) as error:
            return {
                "ok": False,
                "state": "unknown",
                "workspace": str(workspace),
                "dimension_id": marker["dimension_id"],
                "project_id": marker["project_id"],
                "snapshot_id": marker["snapshot_id"],
                "signature_algorithm": marker["signature_algorithm"],
                "error": str(error),
            }
        path_matches = marker["workspace_path_sha256"] == current_path
        signature_matches = marker["workspace_signature"] == scan.signature
        policy_matches = marker["capture_policy_sha256"] == scan.capture_policy_sha256
        clean = path_matches and signature_matches and policy_matches
        return {
            "ok": clean,
            "state": "clean" if clean else "changed",
            "workspace": str(workspace),
            "dimension_id": marker["dimension_id"],
            "project_id": marker["project_id"],
            "snapshot_id": marker["snapshot_id"],
            "workspace_path_sha256": current_path,
            "workspace_signature": scan.signature,
            "signature_algorithm": scan.signature_algorithm,
            "capture_policy_sha256": scan.capture_policy_sha256,
            "path_matches": path_matches,
            "signature_matches": signature_matches,
            "policy_matches": policy_matches,
        }
    current_fingerprint = workspace_fingerprint(workspace)
    path_matches = marker.get("workspace_path_sha256") == current_path
    fingerprint_matches = marker.get("workspace_fingerprint") == current_fingerprint
    return {
        "ok": path_matches and fingerprint_matches,
        "state": "clean" if path_matches and fingerprint_matches else "changed",
        "workspace": str(workspace),
        "dimension_id": marker.get("dimension_id"),
        "project_id": marker.get("project_id"),
        "snapshot_id": marker.get("snapshot_id"),
        "workspace_path_sha256": current_path,
        "workspace_fingerprint": current_fingerprint,
        "path_matches": path_matches,
        "fingerprint_matches": fingerprint_matches,
    }
