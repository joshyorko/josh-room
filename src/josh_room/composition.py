"""Offline composition contracts and bounded split proposals. Never publishes."""
from __future__ import annotations

import itertools
import json
import os
import re
import stat
import time
import unicodedata
from pathlib import Path

from .composition_git_index import validate_child_index
from .composition_git_integrity import object_format
from .workspace_onboarding import _directory
from .workspace_policy import _parts, load_capture_policy

MANIFEST = ".josh-room-composition.json"
SPLIT_GUARD = ".josh-room-split.json"
MAX_CHILDREN = 32
LIMITS = {"directories": 128, "entries_per_directory": 256, "depth": 3}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


def require_uncomposed_workspace(root: Path) -> None:
    """Refuse unsupported composed Save, even if metadata is user-excluded."""
    from .workspace_state import context_status
    if context_status(root).get('composition_namespace'):
        raise ValueError('composed Save is blocked; use the explicit namespace lifecycle')
    for name in (MANIFEST, SPLIT_GUARD):
        try:
            (Path(root) / name).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("composition admission cannot be checked safely") from None
        raise ValueError("composed Save is blocked until retention and publication are admitted")


def portable_path(value: str) -> tuple[str, ...]:
    """Conservative cross-platform names, also safe as literal capture rules."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError("invalid composition child path")
    parts = value.split("/")
    # CLI diagnostics normalize whitespace and redact key-shaped strings.
    # Such names cannot safely serve as round-trippable selection identities.
    if (" ".join(value.split()) != value
            or re.search(r"AGE-SECRET-KEY-[A-Za-z0-9_-]+|\bage1[0-9a-z]{20,}|(?i:bearer\s+)", value)):
        raise ValueError("composition path cannot round-trip safely")
    if any(not part or part in {".", ".."} or part.endswith((".", " "))
           or unicodedata.normalize("NFKC", part.casefold()).split(".")[0].rstrip() in _RESERVED
           or part.casefold().startswith(".josh-room") or part.casefold() == ".git"
           or any(unicodedata.category(c).startswith("C") or c in '\\:*?[]!<>|"#' for c in part)
           or part != part.strip() or unicodedata.normalize("NFC", part) != part
           for part in parts):
        raise ValueError("invalid composition child path")
    _parts(value + "/**")
    return tuple(unicodedata.normalize("NFC", p.casefold()) for p in parts)


def validate_payload_path(value: str, *, allow_nested_policy: bool = False) -> None:
    """Apply ordinary restore name checks, reserving composition control files."""
    from .room_store_operations import _relative_parts

    parts = _relative_parts(value)
    for index, part in enumerate(parts):
        if part == ".git":
            # Git refs and objects are ordinary payload, never selection identities.
            return
        if allow_nested_policy and index > 0 and index == len(parts) - 1 and part == ".josh-roomignore":
            # This is data belonging to an inner project. The restored Room
            # loads its capture policy only from its own root.
            continue
        if part.casefold() == ".git" or part.casefold().startswith(".josh-room"):
            raise ValueError("reserved composition payload path")


def validate_child_git(root: Path, paths, mode: str) -> None:
    """Qualify standalone repositories after the ordinary storage validator."""
    if mode != "full-repository":
        return
    for path in paths:
        parts = path.split("/")
        if ".git" not in parts and parts[-1] == "HEAD":
            candidate = (root / path).parent
            if (candidate / "objects").is_dir() and (candidate / "config").is_file():
                raise ValueError("bare Catalog children are unsupported; save the repository as one ordinary Room")
        if parts[-1] == ".git" and os.path.lexists((root / path).parent / ".gitmodules"):
            raise ValueError("Catalog child submodules are unsupported; save the repository as one ordinary Room")
        if ((parts[-1] == ".gitmodules" and os.path.lexists((root / path).parent / ".git"))
                or (parts[-1] == ".git" and not (root / path).is_dir())):
            raise ValueError("Catalog child submodules and gitfiles are unsupported; save the repository as one ordinary Room")
        if parts[-1] == ".git":
            validate_child_index(root / path, object_format(root / path), child_root=path == ".git")
        if ".git" in parts:
            index = parts.index(".git")
            if parts[index + 1:index + 2] in (["modules"], ["worktrees"]):
                raise ValueError("Catalog child submodules and linked worktrees are unsupported")
            if parts[-1].endswith(".lock"):
                raise ValueError("Git lock present; finish the Git operation before saving")



def validate_paths(paths) -> None:
    keys = []
    spellings = {}
    for path in paths:
        key = portable_path(path)
        parts = path.split("/")
        for length in range(1, len(key) + 1):
            prefix = key[:length]
            spelling = tuple(parts[:length])
            if prefix in spellings and spellings[prefix] != spelling:
                raise ValueError("composition child paths have aliased ancestors")
            spellings[prefix] = spelling
        if any(key[:len(other)] == other or other[:len(key)] == key for other in keys):
            raise ValueError("composition child paths collide or overlap")
        keys.append(key)


def validate_manifest(body: dict) -> dict:
    if (not isinstance(body, dict) or set(body) != {"format", "children"}
            or not isinstance(body["children"], list)
            or (body["format"] == "room-composition-v1" and not 1 <= len(body["children"]) <= MAX_CHILDREN)
            or (body["format"] == "room-composition-v2" and body["children"])
            or body["format"] not in ("room-composition-v1", "room-composition-v2")):
        raise ValueError("invalid composition manifest")
    identities = set()
    for row in body["children"]:
        if (not isinstance(row, dict) or set(row) != {"path", "provider", "connection_id", "dimension_id", "room_id", "snapshot_id", "hydration"}
                or row["provider"] not in ("r2", "minio", "s3")
                or row["hydration"] != "explicit"
                or any(not isinstance(row[k], str) or not _ID.fullmatch(row[k])
                       for k in ("connection_id", "dimension_id", "room_id", "snapshot_id"))
                or row["snapshot_id"].casefold() == "latest"):
            raise ValueError("invalid composition child reference")
        identity = tuple(row[k] for k in ("provider", "connection_id", "dimension_id", "room_id"))
        if identity in identities:
            raise ValueError("duplicate composition child identity")
        identities.add(identity)
    validate_paths(row["path"] for row in body["children"])
    return body


def parse_manifest(raw: bytes) -> dict:
    def closed_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate composition field")
            result[key] = value
        return result
    if not isinstance(raw, bytes) or len(raw) > 64 * 1024:
        raise ValueError("invalid composition manifest size")
    try:
        return validate_manifest(json.loads(raw, object_pairs_hook=closed_object))
    except (UnicodeError, RecursionError, TypeError, ValueError):
        raise ValueError("invalid composition manifest") from None


def discover(root: Path) -> dict:
    """Directory metadata only; no Git execution, payload or marker reads.

    Stop at Git/Room boundaries. Completeness means this bounded boundary pass,
    never a complete inventory or a capture/Git validation result.
    """
    if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
        raise ValueError("safe composition discovery is unavailable on this platform")
    root = Path(root)
    if ".." in root.parts:
        raise ValueError("composition source contains parent traversal")
    root = Path(os.path.abspath(root))
    root_fd = None
    try:
        root_fd = _directory(root)
        device = os.fstat(root_fd).st_dev
        policy = load_capture_policy(root, directory_fd=root_fd)
        candidates, omissions = [], set()
        incomplete_sibling_scopes = set()
        queue = [("", 0)]
        visited = observed = huge = 0
        parent_git = False
        deadline = time.monotonic() + 2
        while queue and visited < LIMITS["directories"]:
            if time.monotonic() >= deadline:
                omissions.add("time-cap")
                break
            relative, depth = queue.pop(0)
            fd = os.dup(root_fd)
            try:
                for component in relative.split("/") if relative else ():
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                if os.fstat(fd).st_dev != device:
                    omissions.add("filesystem-boundary")
                    continue
                visited += 1
                with os.scandir(fd) as entries:
                    sample = list(itertools.islice(entries, LIMITS["entries_per_directory"] + 1))
                observed += len(sample)
                if len(sample) > LIMITS["entries_per_directory"]:
                    omissions.add("entry-cap")
                    incomplete_sibling_scopes.add(relative)
                metadata = {}
                for entry in sample[:LIMITS["entries_per_directory"]]:
                    metadata[entry.name] = entry.stat(follow_symlinks=False)
                # Security boundaries must not depend on enumeration order.
                for name in (".git", ".josh-room.json", MANIFEST, SPLIT_GUARD):
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        metadata[name] = info
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            omissions.add("linked-entry")
                    except FileNotFoundError:
                        pass
                sibling_keys = {}
                for name in metadata:
                    key = unicodedata.normalize("NFC", name.rstrip(" .").casefold())
                    sibling_keys.setdefault(key, []).append(name)
                aliases = {name for group in sibling_keys.values() if len(group) > 1 for name in group}
                if aliases:
                    omissions.add("path-collision")
                git = metadata.get(".git")
                bound = ".josh-room.json" in metadata
                composed = MANIFEST in metadata or SPLIT_GUARD in metadata
                if not relative:
                    parent_git = git is not None
                else:
                    kind = "composition-metadata-unsupported" if composed else "room-marker-unverified" if bound else "git-directory-unverified" if git and stat.S_ISDIR(git.st_mode) else "git-pointer-unsupported" if git else "folder"
                    candidates.append({"path": relative, "kind": kind, "selectable": kind in {"folder", "git-directory-unverified"}})
                    if git or bound or composed:
                        continue
                for name, info in sorted(metadata.items()):
                    path = f"{relative}/{name}" if relative else name
                    if name == ".git" or policy.is_excluded(path):
                        continue
                    if name in aliases:
                        continue
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        omissions.add("linked-entry")
                    elif stat.S_ISDIR(info.st_mode):
                        try:
                            portable_path(path)
                        except ValueError:
                            omissions.add("nonportable-path")
                            continue
                        if depth >= LIMITS["depth"]:
                            omissions.add("depth-cap")
                        elif len(queue) + visited >= LIMITS["directories"]:
                            omissions.add("directory-cap")
                        else:
                            queue.append((path, depth + 1))
                    elif stat.S_ISREG(info.st_mode):
                        huge += info.st_size >= 1024 ** 3
                    else:
                        omissions.add("special-entry")
            except OSError:
                omissions.add("unavailable-directory")
            finally:
                os.close(fd)
        if queue and "time-cap" not in omissions:
            omissions.add("directory-cap")
        # Fold aliases even on case-sensitive hosts; never offer ambiguous paths.
        keys = [portable_path(row["path"]) for row in candidates]
        for index, row in enumerate(candidates):
            if any(not prefix or row["path"].startswith(prefix + "/") for prefix in incomplete_sibling_scopes):
                row["selectable"] = False
            if any(keys[index][:min(len(keys[index]), len(other))] == other[:min(len(keys[index]), len(other))]
                   and tuple(row["path"].split("/")[:min(len(keys[index]), len(other))])
                   != tuple(candidates[j]["path"].split("/")[:min(len(keys[index]), len(other))])
                   for j, other in enumerate(keys) if j != index):
                row["selectable"] = False
                omissions.add("path-collision")
        if load_capture_policy(root, directory_fd=root_fd).sha256 != policy.sha256:
            raise ValueError("capture policy changed during composition discovery")
        # Leave room for selected rows and envelope under the CLI's 64 KiB cap.
        while len(json.dumps(candidates).encode()) > 16 * 1024:
            candidates.pop()
            omissions.add("output-cap")
        return {"ok": True, "format_version": 1, "status": "preview-only", "can_apply": False,
                "capture_policy_sha256": policy.sha256, "capture_mode": policy.capture_mode,
                "candidates": sorted(candidates, key=lambda row: row["path"]),
                "coverage": {"complete": not omissions, "scope": "directory-boundaries-only",
                             "limits": LIMITS.copy(), "directories_visited": visited,
                             "entries_observed": observed, "payload_bytes_read": 0,
                             "large_files_observed": huge, "omissions": sorted(omissions),
                             "enumeration_order_dependent": "entry-cap" in omissions},
                "parent_git": "history-unknown" if parent_git else "not-observed",
                "blockers": ["pin-retention-not-integrated", "split-publication-not-implemented",
                             "git-history-isolation-unproven"]}
    except OSError:
        raise ValueError("composition source cannot be inspected safely") from None
    finally:
        if root_fd is not None:
            os.close(root_fd)


def plan_split(root: Path, selected: list[str]) -> dict:
    if len(selected) > MAX_CHILDREN:
        raise ValueError("too many selected composition children")
    validate_paths(selected)
    result = discover(root)
    available = {row["path"] for row in result["candidates"] if row["selectable"]}
    if not set(selected) <= available:
        raise ValueError("selected composition boundary is unavailable or unsupported")
    result["selected"] = [{"path": p, "proposed_exclusion": p + "/**"} for p in selected]
    result["unselected_behavior"] = "retain-current-parent-policy"
    result["writes_performed"] = False
    return result
