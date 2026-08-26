import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

MARKER_NAME = ".josh-room.json"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def _identifier(label: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"marker {label} is invalid")


def _digest(value: object, label: str) -> None:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError(f"marker {label} is invalid")


def canonical_workspace_path_sha256(workspace: Path) -> str:
    return hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()


def _ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in {".git", ".josh-room", ".venv", "__pycache__"} for part in relative.parts) or path.name == MARKER_NAME


def workspace_fingerprint(workspace: Path) -> str:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    digest = hashlib.sha256()
    entries = sorted(
        (
            path
            for path in root.rglob("*")
            if not path.is_symlink() and not _ignored(path, root)
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    for path in entries:
        relative = path.relative_to(root).as_posix().encode()
        metadata = path.stat()
        mode = str(stat.S_IMODE(metadata.st_mode)).encode()
        if path.is_dir():
            digest.update(b"d\0" + relative + b"\0" + mode + b"\n")
            continue
        if not path.is_file():
            continue
        content_digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                content_digest.update(chunk)
        digest.update(
            b"f\0"
            + relative
            + b"\0"
            + mode
            + b"\0"
            + str(metadata.st_size).encode()
            + b"\0"
            + content_digest.hexdigest().encode()
            + b"\n"
        )
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


def read_workspace_marker(workspace: Path) -> dict:
    path = Path(workspace) / MARKER_NAME
    if not path.is_file():
        raise ValueError("workspace marker is unavailable")
    try:
        marker = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("workspace marker is invalid") from error
    if marker.get("format_version") == 1:
        _identifier("project_id", marker.get("project_id"))
        if not isinstance(marker.get("display_name"), str) or not marker["display_name"]:
            raise ValueError("marker display_name is invalid")
        return marker
    if marker.get("format_version") != 2:
        raise ValueError("unsupported workspace marker format")
    for label in ("dimension_id", "project_id", "snapshot_id"):
        _identifier(label, marker.get(label))
    _digest(marker.get("workspace_fingerprint"), "workspace fingerprint")
    _digest(marker.get("workspace_path_sha256"), "workspace path sha256")
    if not isinstance(marker.get("display_name"), str) or not marker["display_name"]:
        raise ValueError("marker display_name is invalid")
    return marker


def local_status(workspace: Path) -> dict:
    workspace = Path(workspace)
    try:
        marker = read_workspace_marker(workspace)
    except ValueError as error:
        return {"ok": False, "state": "unlinked", "workspace": str(workspace), "error": str(error)}
    current_path = canonical_workspace_path_sha256(workspace)
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
