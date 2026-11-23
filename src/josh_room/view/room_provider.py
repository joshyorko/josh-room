"""Native Josh Room-backed ViewStateProviders for live workspaces and saved capsule manifests.

Architectural separation (Issue #202):
- (A) Live Room workspace (`RoomWorkspaceProvider`): attaches directly to Josh Room's
  existing safe-root (`workspace_onboarding._directory`), capture policy
  (`workspace_policy.load_capture_policy`), Room identity (`workspace_state.context_status`),
  and Linux mount/descriptor/alias invariants (`directory_inventory`, `composition`).
  It provides bounded, root-scoped directory/file enumeration and sanitized
  read-only previews — never an arbitrary host file server, never capture authority, and
  never a claim of an atomic filesystem snapshot.
- (B) Saved JAT/Hauler capsule artifacts (`SavedCapsuleManifestProvider`): validates an
  immutable `LogicalJat` descriptor via `josh_room.logical_jat.LogicalJat.from_json` and
  projects its verified workspace & component anchors without invoking Hauler's broad
  `serve(mode=files)` (`0.0.0.0:8080`), which exposes top-level `tar.zst` capsule artifacts
  rather than nested live workspace paths.
- (C) Localhost HTTP viewer transport (`server.py`): serves only viewer UI, sanitized state,
  and allowlisted `/content/<id>/<slot>` + `/attachments/<id>/<name>` responses on 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..composition import MANIFEST as COMPOSITION_MANIFEST
from ..composition import SPLIT_GUARD as COMPOSITION_SPLIT_GUARD
from ..composition import portable_path
from ..directory_inventory import mount_id, recommendation, stamp
from ..logical_jat import MAX_LOGICAL_JAT_BYTES, LogicalJat
from ..workspace_onboarding import _directory
from ..workspace_policy import CapturePolicy, load_capture_policy
from ..workspace_state import context_status
from .contract import (
    ALLOWED_EMBED_MEDIA_TYPES,
    MAX_PREVIEW_BODY_CHARS,
    SENSITIVITY_LEVELS,
    Attachment,
    AttachmentRef,
    GroupState,
    ItemState,
    LegendEntry,
    MetricDescriptor,
    NodeState,
    ObjectLink,
    PreviewContent,
    SqlPreset,
    StageDescriptor,
    ViewDescriptor,
    ViewState,
    ViewValidationError,
)
from .sanitizer import (
    DEFAULT_SANITIZATION_POLICY,
    _check_json_structure_safe,
    _check_safe_text,
)

_HAS_DIR_FD = os.open in os.supports_dir_fd and os.scandir in os.supports_fd
_IS_LINUX = sys.platform.startswith("linux")

_SECRET_BASENAME_EXACT = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        ".pgpass",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "shadow",
        "master.key",
    }
)
_SECRET_BASENAME_PREFIXES = (".env.", "id_rsa.", "id_ed25519.", "id_ecdsa.")
_SECRET_EXTENSIONS = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".age", ".kdbx", ".ovpn", ".crt"}
)
_SECRET_NAME_SUBSTRINGS = (
    "secret",
    "token",
    "credential",
    "passwd",
    "password",
    "private_key",
    "private-key",
    "apikey",
    "api_key",
    "api-key",
)

_SOURCE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".css": "css",
    ".html": "html",
    ".xml": "xml",
    ".tla": "tla",
    ".robot": "robot",
}
_CONFIG_EXTENSIONS: dict[str, str] = {
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".properties": "ini",
}
_DOC_EXTENSIONS: dict[str, str] = {
    ".md": "markdown",
    ".rst": "text",
    ".txt": "text",
    ".log": "log",
    ".csv": "text",
}
_DIFF_EXTENSIONS = frozenset({".diff", ".patch"})
_EMBED_EXTENSION_MEDIA: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
_MANIFEST_NAMES = frozenset(
    {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "robot.yaml", "conda.yaml", ".josh-roomignore"}
)
_DOC_ANCHOR_NAMES = frozenset({"README.md", "AGENTS.md", "CONTRIBUTING.md", "ARCHITECTURE.md"})


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_digest(hex_digest: str) -> str:
    """Format an 8-hex-char prefix as xxxx-xxxx so numeric hashes never trigger long-digit-run."""
    raw = hex_digest.removeprefix("sha256:")[:8]
    return f"{raw[:4]}-{raw[4:8]}" if len(raw) >= 8 else raw


def _safe_id(prefix: str, relative: str) -> str:
    digest = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:8]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", relative).strip("-")[:36]
    if not slug:
        slug = "entry"
    candidate = f"{prefix}-{slug}-{digest}"
    if not candidate[0].isalnum():
        candidate = f"n-{candidate}"
    return candidate[:64]


def _safe_display_label(name: str, fallback_key: str) -> str:
    cleaned = name.strip()
    if cleaned:
        try:
            return _check_safe_text(
                cleaned,
                "workspace.label",
                max_length=DEFAULT_SANITIZATION_POLICY.max_label_length,
                max_digit_run=DEFAULT_SANITIZATION_POLICY.max_digit_run,
            )
        except ViewValidationError:
            pass
    digest = hashlib.sha256(fallback_key.encode("utf-8")).hexdigest()[:8]
    return f"entry-{digest}"


def _safe_attachment_name(name: str, fallback_key: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if cleaned and ".." not in cleaned and cleaned[0].isalnum():
        return cleaned[:96]
    digest = hashlib.sha256(fallback_key.encode("utf-8")).hexdigest()[:8]
    return f"asset-{digest}.bin"


def _is_secret_filename(name: str) -> bool:
    lowered = name.lower()
    if lowered in _SECRET_BASENAME_EXACT:
        return True
    if any(lowered.startswith(prefix) for prefix in _SECRET_BASENAME_PREFIXES):
        return True
    suffix = Path(lowered).suffix
    if suffix in _SECRET_EXTENSIONS:
        return True
    return any(marker in lowered for marker in _SECRET_NAME_SUBSTRINGS)


def _alias_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.rstrip(" .").casefold())


def _safe_mount_id(fd: int) -> int | None:
    if not _IS_LINUX:
        return None
    return mount_id(fd)


@dataclass(frozen=True, slots=True)
class _AttachmentRecord:
    rel_parts: tuple[str, ...]
    sha256: str
    size: int
    media_type: str
    policy_sha256: str
    root_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _FileClassification:
    stage: str
    legend_kind: str
    node_kind: str
    slot: str
    media_type: str
    language: str | None
    embed_media_type: str | None
    is_secret: bool


def _classify_file(name: str) -> _FileClassification:
    if _is_secret_filename(name):
        return _FileClassification(
            stage="restricted",
            legend_kind="redacted",
            node_kind="file",
            slot="source",
            media_type="text/plain",
            language=None,
            embed_media_type=None,
            is_secret=True,
        )
    lowered = name.lower()
    suffix = Path(lowered).suffix
    if suffix == ".json":
        return _FileClassification(
            stage="config",
            legend_kind="config",
            node_kind="file",
            slot="json",
            media_type="application/json",
            language="json",
            embed_media_type=None,
            is_secret=False,
        )
    if suffix in _DIFF_EXTENSIONS:
        return _FileClassification(
            stage="source",
            legend_kind="source",
            node_kind="file",
            slot="diff",
            media_type="text/x-diff",
            language="diff",
            embed_media_type=None,
            is_secret=False,
        )
    if suffix in _SOURCE_EXTENSIONS:
        return _FileClassification(
            stage="source",
            legend_kind="source",
            node_kind="file",
            slot="source",
            media_type="text/plain",
            language=_SOURCE_EXTENSIONS[suffix],
            embed_media_type=None,
            is_secret=False,
        )
    if suffix in _CONFIG_EXTENSIONS or lowered in {"dockerfile", "makefile", ".josh-roomignore", ".gitignore"}:
        return _FileClassification(
            stage="config",
            legend_kind="config",
            node_kind="file",
            slot="config",
            media_type="text/plain",
            language=_CONFIG_EXTENSIONS.get(suffix, "text"),
            embed_media_type=None,
            is_secret=False,
        )
    if suffix in _DOC_EXTENSIONS:
        return _FileClassification(
            stage="source",
            legend_kind="doc",
            node_kind="file",
            slot="log" if suffix == ".log" else "source",
            media_type="text/plain",
            language=_DOC_EXTENSIONS[suffix],
            embed_media_type=None,
            is_secret=False,
        )
    if suffix in _EMBED_EXTENSION_MEDIA:
        embed_mt = _EMBED_EXTENSION_MEDIA[suffix]
        return _FileClassification(
            stage="artifact",
            legend_kind="artifact",
            node_kind="artifact",
            slot="source",
            media_type="text/plain",
            language=None,
            embed_media_type=embed_mt if embed_mt in ALLOWED_EMBED_MEDIA_TYPES else None,
            is_secret=False,
        )
    return _FileClassification(
        stage="artifact",
        legend_kind="artifact",
        node_kind="artifact",
        slot="source",
        media_type="text/plain",
        language=None,
        embed_media_type=None,
        is_secret=False,
    )


class RoomWorkspaceProvider:
    """Native Josh Room-backed read-only ViewStateProvider for a live workspace root.

    Reuses Josh Room's native root opening (`workspace_onboarding._directory`),
    capture policy (`workspace_policy.load_capture_policy`), Room marker check
    (`workspace_state.context_status`), and Linux mount/descriptor/alias guards
    (`directory_inventory`, `composition`).

    Security & scope boundaries:
    - Never follows symlinks or crosses filesystem/mount boundaries.
    - Never reads policy-excluded paths or secret-named files.
    - Fails closed per-file to `status="redacted"` whenever the Room View sanitizer
      detects secrets, host paths, private IPs, or long digit runs in text/JSON.
    - Never emits raw host paths anywhere in the descriptor, state, fields, or errors.
    - Never claims Save/capture authority or a consistent filesystem snapshot.
    """

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        max_nodes: int = 250,
        max_depth: int = 6,
        max_children_per_dir: int = 64,
        max_seconds: float = 5.0,
        max_preview_bytes: int = MAX_PREVIEW_BODY_CHARS,
        max_total_preview_bytes: int = 512 * 1024,
        max_attachment_bytes: int = 1024 * 1024,
        include_previews: bool = True,
        include_attachments: bool = True,
        sensitivity: str = "internal",
    ) -> None:
        if not _HAS_DIR_FD or not getattr(os, "O_NOFOLLOW", 0):
            raise ViewValidationError("workspace-platform-unsupported", "safe descriptor-relative workspace reads unavailable")
        raw_root = Path(workspace_root)
        if ".." in raw_root.parts:
            raise ViewValidationError("unsafe-workspace", "workspace root must not contain parent traversal")
        if max_nodes < 2 or max_nodes > 1000:
            raise ViewValidationError("invalid-budget", "max_nodes must be between 2 and 1000")
        if max_depth < 1 or max_depth > 16:
            raise ViewValidationError("invalid-budget", "max_depth must be between 1 and 16")
        if max_children_per_dir < 1 or max_children_per_dir > 256:
            raise ViewValidationError("invalid-budget", "max_children_per_dir must be between 1 and 256")
        if max_seconds <= 0 or max_seconds > 30:
            raise ViewValidationError("invalid-budget", "max_seconds must be between 0 and 30")
        if max_preview_bytes < 256 or max_preview_bytes > MAX_PREVIEW_BODY_CHARS:
            raise ViewValidationError("invalid-budget", f"max_preview_bytes must be <= {MAX_PREVIEW_BODY_CHARS}")
        level = sensitivity.lower().strip()
        if level not in SENSITIVITY_LEVELS:
            raise ViewValidationError("invalid-sensitivity", "unsupported sensitivity level")

        self._root = Path(os.path.abspath(raw_root))
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._max_children_per_dir = max_children_per_dir
        self._max_seconds = max_seconds
        self._max_preview_bytes = max_preview_bytes
        self._max_total_preview_bytes = max_total_preview_bytes
        self._max_attachment_bytes = max_attachment_bytes
        self._include_previews = bool(include_previews)
        self._include_attachments = bool(include_attachments)
        self._sensitivity = level
        self._lock = threading.RLock()
        self._attachment_index: dict[tuple[str, str], _AttachmentRecord] = {}

        # Verify root can be opened safely with O_NOFOLLOW before advertising a descriptor.
        try:
            fd = _directory(self._root)
        except OSError as error:
            raise ViewValidationError("workspace-unavailable", "workspace root is unavailable or unsafe") from error
        try:
            _ = self._capture_policy(fd)
        except ViewValidationError:
            raise
        except (OSError, ValueError) as error:
            raise ViewValidationError("workspace-policy-invalid", "workspace capture policy could not be loaded") from error
        finally:
            os.close(fd)

        self._descriptor = self._build_descriptor()

    def _capture_policy(self, directory_fd: int) -> CapturePolicy:
        # Managed RCC always sets ROBOCORP_HOME. Descriptor-bound policy cannot
        # resolve runtime paths; refuse overlap rather than weakening its guards.
        runtime_value = os.environ.get("ROBOCORP_HOME")
        if runtime_value:
            try:
                runtime = Path(runtime_value).resolve()
                root = self._root.resolve()
            except (OSError, RuntimeError) as error:
                raise ViewValidationError("workspace-policy-invalid", "runtime boundary is unavailable") from error
            if runtime == root or runtime in root.parents or root in runtime.parents:
                raise ViewValidationError("workspace-runtime-overlap", "Room View cannot observe the managed runtime directory")
        return load_capture_policy(self._root, directory_fd=directory_fd if _HAS_DIR_FD else None)

    def _build_descriptor(self) -> ViewDescriptor:
        ctx = context_status(self._root)
        title = "Room Workspace"
        if ctx.get("linked") and isinstance(ctx.get("display_name"), str):
            safe_name = _safe_display_label(ctx["display_name"], "room-display-name")
            if not safe_name.startswith("entry-"):
                candidate = f"Room: {safe_name}"
                if len(candidate) <= 96:
                    title = candidate

        return ViewDescriptor(
            title=title,
            stages=(
                StageDescriptor("dir", "Directory", 0),
                StageDescriptor("source", "Source", 1),
                StageDescriptor("config", "Config", 2),
                StageDescriptor("artifact", "Artifact", 3, terminal=True),
                StageDescriptor("restricted", "Restricted", 4, terminal=True),
            ),
            group_label="Directory",
            legend={
                "source": LegendEntry("Source", "#22C55E", "solid"),
                "config": LegendEntry("Config", "#38BDF8", "solid"),
                "doc": LegendEntry("Docs", "#14B8A6", "solid"),
                "artifact": LegendEntry("Artifact", "#A855F7", "notch"),
                "redacted": LegendEntry("Redacted", "#F59E0B", "hollow"),
                "excluded": LegendEntry("Omitted", "#EF4444", "hollow"),
            },
            metric=MetricDescriptor("File Size", min=0.0, max=64.0, unit="KiB"),
            view="3d",
            tool="josh-room",
            tool_version="0.1.0",
            sensitivity=self._sensitivity,
            sql_presets=(
                SqlPreset(
                    "largest_files",
                    "Largest workspace files",
                    "SELECT id, path, label, kind, metric FROM nodes WHERE node_kind IN ('file','artifact') ORDER BY metric DESC LIMIT 50",
                    "Show largest indexed files by metric size",
                ),
                SqlPreset(
                    "redacted_or_unavailable",
                    "Redacted or unavailable previews",
                    "SELECT p.node_id, n.path, p.slot, p.status, p.reason FROM previews p JOIN nodes n ON n.id = p.node_id WHERE p.status != 'available' ORDER BY n.path",
                    "Audit files whose previews were redacted, omitted, or unavailable",
                ),
                SqlPreset(
                    "workspace_dirs",
                    "Indexed directories",
                    "SELECT id, path, label FROM nodes WHERE node_kind = 'directory' ORDER BY path",
                    "List root-scoped directories admitted by capture policy",
                ),
                SqlPreset(
                    "workspace_links",
                    "Workspace relationships",
                    "SELECT source_id, relation, target_id, label FROM links ORDER BY source_id, relation",
                    "Inspect static manifest and documentation links",
                ),
            ),
        )

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def _open_relative_dir(
        self,
        root_fd: int,
        parts: tuple[str, ...],
        root_dev: int,
        root_mount: int | None,
    ) -> int:
        if not _HAS_DIR_FD:
            target = self._root.joinpath(*parts) if parts else self._root
            fd = _directory(target)
            try:
                info = os.fstat(fd)
                if info.st_dev != root_dev:
                    raise ValueError("filesystem-boundary")
                return fd
            except BaseException:
                os.close(fd)
                raise

        fd = os.dup(root_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in parts:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if info.st_dev != root_dev:
                    raise ValueError("filesystem-boundary")
                if root_mount is not None and _safe_mount_id(fd) != root_mount:
                    raise ValueError("filesystem-boundary")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _dir_boundary_kind(self, dir_fd: int, dir_path: Path, *, is_root: bool) -> str:
        kind = "folder"
        for name in (".git", ".josh-room.json", COMPOSITION_MANIFEST, COMPOSITION_SPLIT_GUARD):
            try:
                info = (
                    os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                    if _HAS_DIR_FD
                    else os.lstat(dir_path / name)
                )
            except FileNotFoundError:
                continue
            except OSError:
                return "inaccessible-boundary"
            if is_root and name == ".josh-room.json":
                continue
            if name != ".git":
                return "reserved-boundary"
            if stat.S_ISDIR(info.st_mode) and not (getattr(info, "st_file_attributes", 0) & 0x400):
                kind = "git-directory-unverified"
            else:
                kind = "git-pointer-unsupported"
        return kind

    def _read_regular_file_bytes(
        self,
        dir_fd: int,
        dir_path: Path,
        name: str,
        expected_info: os.stat_result,
        limit: int,
    ) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            if _HAS_DIR_FD:
                fd = os.open(name, flags, dir_fd=dir_fd)
            else:
                fd = os.open(dir_path / name, flags)
        except OSError:
            return None
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != expected_info.st_dev
                or opened.st_ino != expected_info.st_ino
                or opened.st_size > limit
            ):
                return None
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                block = os.read(fd, min(65536, limit - total + 1))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
            if total > limit:
                return None
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

    def _build_file_preview(
        self,
        dir_fd: int,
        dir_path: Path,
        name: str,
        info: os.stat_result,
        classification: _FileClassification,
        safe_label: str,
        remaining_preview_budget: int,
    ) -> tuple[PreviewContent, int, bool]:
        """Return (PreviewContent, bytes_consumed, was_redacted)."""
        if classification.is_secret:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title="Redacted by policy",
                    status="redacted",
                    reason="filename matches secret or credential policy",
                ),
                0,
                True,
            )
        if not self._include_previews:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title="Preview disabled",
                    status="omitted",
                    reason="previews disabled by provider configuration",
                ),
                0,
                False,
            )
        if classification.embed_media_type is not None:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title="Binary evidence",
                    status="unavailable",
                    reason=f"{classification.embed_media_type} payload inspected via Evidence tab",
                ),
                0,
                False,
            )
        if classification.node_kind == "artifact":
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title="Binary file",
                    status="unavailable",
                    reason="binary or non-allowlisted file type not previewed",
                ),
                0,
                False,
            )
        if info.st_size > self._max_preview_bytes or info.st_size > remaining_preview_budget:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title=safe_label,
                    status="omitted",
                    reason="file exceeds bounded preview byte budget",
                    truncated=True,
                ),
                0,
                False,
            )
        raw = self._read_regular_file_bytes(dir_fd, dir_path, name, info, self._max_preview_bytes)
        if raw is None:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title=safe_label,
                    status="unavailable",
                    reason="file changed or could not be read safely",
                ),
                0,
                False,
            )
        if b"\x00" in raw:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title=safe_label,
                    status="unavailable",
                    reason="binary content detected",
                ),
                0,
                False,
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title=safe_label,
                    status="unavailable",
                    reason="non-UTF-8 content not previewed",
                ),
                0,
                False,
            )

        # Mandatory fail-closed per-file sanitizer gate before returning any bytes.
        try:
            _check_safe_text(
                text,
                "workspace.preview",
                max_length=self._max_preview_bytes,
                max_digit_run=DEFAULT_SANITIZATION_POLICY.max_digit_run,
                allow_newlines=True,
            )
            media_type = classification.media_type
            if media_type == "application/json":
                try:
                    parsed = json.loads(text)
                except ValueError:
                    media_type = "text/plain"
                else:
                    _check_json_structure_safe(
                        parsed,
                        "workspace.preview.json",
                        policy=DEFAULT_SANITIZATION_POLICY,
                    )
        except ViewValidationError as err:
            return (
                PreviewContent(
                    slot=classification.slot,
                    media_type="text/plain",
                    title="Redacted by sanitizer",
                    status="redacted",
                    reason=f"content redacted by sanitizer ({err.code})",
                ),
                0,
                True,
            )

        lines = text.count("\n") + (1 if text else 0)
        return (
            PreviewContent(
                slot=classification.slot,
                media_type=media_type,
                title=safe_label,
                status="available",
                body=text,
                language=classification.language,
                summary=f"{lines} lines, {len(raw)} bytes",
            ),
            len(raw),
            False,
        )

    def state(self) -> ViewState:
        started = time.monotonic()
        deadline = started + self._max_seconds
        now_iso = _utc_now_iso()
        try:
            root_fd = _directory(self._root)
        except OSError as error:
            raise ViewValidationError("workspace-unavailable", "workspace root is unavailable or unsafe") from error

        omissions: set[str] = set()
        new_attachments: dict[tuple[str, str], _AttachmentRecord] = {}

        try:
            root_stat = os.fstat(root_fd)
            root_dev = root_stat.st_dev
            root_stamp = stamp(root_stat)
            root_mount = _safe_mount_id(root_fd)
            policy = self._capture_policy(root_fd)
            ctx = context_status(self._root)

            nodes_list: list[NodeState] = []
            items_list: list[ItemState] = []
            groups_map: dict[str, GroupState] = {"dir-root": GroupState("dir-root", "/")}
            dir_node_by_rel: dict[str, str] = {"": "dir-root"}
            dir_children_rel: dict[str, dict[str, str]] = {"": {}}
            node_rel_to_id: dict[str, str] = {"": "dir-root"}
            dir_meta: dict[str, dict[str, str | int | float | bool | None]] = {}

            entries_observed = 0
            excluded_observed = 0
            redacted_files = 0
            total_preview_bytes = 0
            emitted_nodes = 1  # dir-root

            root_kind = self._dir_boundary_kind(root_fd, self._root, is_root=True)
            dir_meta[""] = {
                "boundary": root_kind,
                "capture_mode": policy.capture_mode,
                "policy_digest": _short_digest(policy.sha256),
                "room_linked": bool(ctx.get("linked")),
                "freshness": "observed-live-workspace-not-snapshot",
            }
            if ctx.get("linked"):
                if isinstance(ctx.get("project_id"), str):
                    dir_meta[""]["room_id"] = ctx["project_id"]
                if isinstance(ctx.get("dimension_id"), str):
                    dir_meta[""]["dimension_id"] = ctx["dimension_id"]

            # BFS queue: (rel_path, rel_parts, parent_node_id, depth, top_group_id, cohesive)
            queue: deque[tuple[str, tuple[str, ...], str, int, str, bool]] = deque()
            queue.append(("", (), "dir-root", 0, "dir-root", root_kind == "git-directory-unverified"))

            while queue:
                if time.monotonic() >= deadline:
                    omissions.add("time-cap")
                    break
                rel_dir, rel_parts, dir_node_id, depth, top_group_id, cohesive = queue.popleft()
                dir_path = self._root.joinpath(*rel_parts) if rel_parts else self._root
                try:
                    dir_fd = self._open_relative_dir(root_fd, rel_parts, root_dev, root_mount)
                except (OSError, ValueError):
                    omissions.add("inaccessible-or-filesystem-boundary")
                    continue

                truncated_children = 0
                try:
                    kind = self._dir_boundary_kind(dir_fd, dir_path, is_root=not rel_dir)
                    if rel_dir and kind in ("reserved-boundary", "git-pointer-unsupported", "inaccessible-boundary"):
                        omissions.add(kind)
                        dir_meta.setdefault(rel_dir, {})["boundary"] = kind
                        continue
                    next_cohesive = bool(cohesive or kind == "git-directory-unverified")

                    scan_target = dir_fd if _HAS_DIR_FD else dir_path
                    raw_entries: list[os.DirEntry[str]] = []
                    scan_limit = max(self._max_children_per_dir * 4, 256)
                    scan_complete = True
                    try:
                        with os.scandir(scan_target) as iterator:
                            for idx, entry in enumerate(iterator):
                                entries_observed += 1
                                if idx >= scan_limit:
                                    scan_complete = False
                                    omissions.add("directory-scan-cap")
                                    truncated_children += 1
                                    break
                                raw_entries.append(entry)
                    except OSError:
                        omissions.add("inaccessible-entry")
                        continue

                    # Without the complete sibling set alias safety is unknown.
                    if not scan_complete:
                        dir_meta.setdefault(rel_dir, {})["truncated_children"] = truncated_children
                        continue

                    raw_entries.sort(key=lambda e: e.name)

                    # Sibling alias folding matching DirectoryInventory invariants.
                    alias_groups: dict[str, list[str]] = {}
                    for entry in raw_entries:
                        try:
                            entry.name.encode("utf-8")
                        except UnicodeEncodeError:
                            omissions.add("undecodable-name")
                            continue
                        alias_groups.setdefault(_alias_key(entry.name), []).append(entry.name)
                    colliding_names = {
                        nm for names in alias_groups.values() if len(names) > 1 for nm in names
                    }
                    if colliding_names:
                        omissions.add("path-collision")

                    emitted_in_dir = 0
                    for entry in raw_entries:
                        if time.monotonic() >= deadline:
                            omissions.add("time-cap")
                            break
                        name = entry.name
                        if name == ".git":
                            continue
                        if name in colliding_names:
                            continue
                        try:
                            name.encode("utf-8")
                        except UnicodeEncodeError:
                            continue
                        child_rel = f"{rel_dir}/{name}" if rel_dir else name
                        if policy.is_excluded(child_rel):
                            excluded_observed += 1
                            omissions.add("policy-excluded")
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            omissions.add("inaccessible-entry")
                            continue
                        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
                            omissions.add("linked-entry")
                            continue
                        if info.st_dev != root_dev:
                            omissions.add("filesystem-boundary")
                            continue

                        if stat.S_ISDIR(info.st_mode):
                            try:
                                portable_path(child_rel)
                            except ValueError:
                                omissions.add("nonportable-path")
                                continue
                            if depth >= self._max_depth:
                                omissions.add("depth-cap")
                                truncated_children += 1
                                continue
                            if emitted_in_dir >= self._max_children_per_dir:
                                omissions.add("dir-child-cap")
                                truncated_children += 1
                                continue
                            if emitted_nodes >= self._max_nodes:
                                omissions.add("node-cap")
                                truncated_children += 1
                                continue

                            child_parts = (*rel_parts, name)
                            child_id = _safe_id("dir", child_rel)
                            safe_label = _safe_display_label(name, child_rel)
                            child_group_id = child_id if depth == 0 else top_group_id
                            if depth == 0:
                                groups_map[child_id] = GroupState(child_id, f"/{safe_label}")

                            # Probe boundary recommendation cleanly without crossing mounts.
                            child_boundary = "folder"
                            try:
                                sub_fd = self._open_relative_dir(root_fd, child_parts, root_dev, root_mount)
                                try:
                                    child_boundary = self._dir_boundary_kind(
                                        sub_fd, self._root.joinpath(*child_parts), is_root=False
                                    )
                                finally:
                                    os.close(sub_fd)
                            except (OSError, ValueError):
                                child_boundary = "inaccessible-boundary"
                            _rank, ev_code, _reason, consequence = recommendation(
                                child_boundary, next_cohesive, False
                            )

                            dir_node_by_rel[child_rel] = child_id
                            node_rel_to_id[child_rel] = child_id
                            dir_children_rel.setdefault(rel_dir, {})[name] = child_id
                            dir_children_rel.setdefault(child_rel, {})
                            dir_meta[child_rel] = {
                                "boundary": child_boundary,
                                "evidence_code": ev_code,
                                "planned_consequence": consequence,
                                "rel_depth": depth + 1,
                            }
                            emitted_nodes += 1
                            emitted_in_dir += 1
                            queue.append((child_rel, child_parts, child_id, depth + 1, child_group_id, next_cohesive))

                        elif stat.S_ISREG(info.st_mode):
                            if emitted_in_dir >= self._max_children_per_dir:
                                omissions.add("dir-child-cap")
                                truncated_children += 1
                                continue
                            if emitted_nodes >= self._max_nodes:
                                omissions.add("node-cap")
                                truncated_children += 1
                                continue

                            child_id = _safe_id("file", child_rel)
                            safe_label = _safe_display_label(name, child_rel)
                            classification = _classify_file(name)
                            remaining_budget = max(0, self._max_total_preview_bytes - total_preview_bytes)
                            preview, consumed, was_redacted = self._build_file_preview(
                                dir_fd,
                                dir_path,
                                name,
                                info,
                                classification,
                                safe_label,
                                remaining_budget,
                            )
                            total_preview_bytes += consumed
                            if was_redacted:
                                redacted_files += 1

                            stage = "restricted" if was_redacted else classification.stage
                            legend_kind = "redacted" if was_redacted else classification.legend_kind

                            att_refs: list[AttachmentRef] = []
                            if (
                                self._include_attachments
                                and not was_redacted
                                and classification.embed_media_type is not None
                                and info.st_size <= self._max_attachment_bytes
                            ):
                                att_bytes = self._read_regular_file_bytes(
                                    dir_fd, dir_path, name, info, self._max_attachment_bytes
                                )
                                if att_bytes is not None:
                                    digest = hashlib.sha256(att_bytes).hexdigest()
                                    att_name = _safe_attachment_name(name, child_rel)
                                    new_attachments[(child_id, att_name)] = _AttachmentRecord(
                                        rel_parts=(*rel_parts, name),
                                        sha256=digest,
                                        size=len(att_bytes),
                                        media_type=classification.embed_media_type,
                                        policy_sha256=policy.sha256,
                                        root_identity=(root_stat.st_dev, root_stat.st_ino),
                                    )
                                    att_refs.append(
                                        AttachmentRef(
                                            name=att_name,
                                            media_type=classification.embed_media_type,
                                            size=len(att_bytes),
                                            digest=digest,
                                        )
                                    )

                            size_kib = round(min(64.0, max(0.25, info.st_size / 1024.0)), 2)
                            file_fields: dict[str, str | int | float | bool | None] = {
                                "entry_kind": classification.node_kind,
                                "file_bytes": int(info.st_size),
                                "preview_status": preview.status,
                                "rel_depth": depth + 1,
                            }
                            if classification.language:
                                file_fields["language"] = classification.language

                            node_rel_to_id[child_rel] = child_id
                            dir_children_rel.setdefault(rel_dir, {})[name] = child_id
                            nodes_list.append(
                                NodeState(
                                    id=child_id,
                                    parent_id=dir_node_id,
                                    label=safe_label,
                                    node_kind=classification.node_kind,
                                    updated_at=now_iso,
                                    kind=legend_kind,
                                    metric=size_kib,
                                    fields=file_fields,
                                    attachments=tuple(att_refs),
                                    previews=(preview,),
                                )
                            )
                            items_list.append(
                                ItemState(
                                    id=child_id,
                                    group=top_group_id,
                                    stage=stage,
                                    kind=legend_kind,
                                    metric=size_kib,
                                    label=safe_label,
                                    updated_at=now_iso,
                                    fields=file_fields,
                                    attachments=tuple(att_refs),
                                    previews=(preview,),
                                )
                            )
                            emitted_nodes += 1
                            emitted_in_dir += 1
                        else:
                            omissions.add("special-entry")
                finally:
                    os.close(dir_fd)

                if truncated_children > 0:
                    dir_meta.setdefault(rel_dir, {})["truncated_children"] = truncated_children

            # Post-scan integrity check: verify policy and root directory identity did not drift.
            policy_drifted = False
            try:
                after_policy = self._capture_policy(root_fd)
                after_stamp = stamp(os.fstat(root_fd))
                if after_policy.sha256 != policy.sha256 or after_stamp != root_stamp:
                    policy_drifted = True
                    omissions.add("workspace-changed-during-scan")
            except (OSError, ValueError):
                policy_drifted = True
                omissions.add("workspace-changed-during-scan")

            if policy_drifted:
                # Fail closed: strip preview bodies & attachments if policy/root drifted mid-scan.
                new_attachments.clear()
                stripped_nodes: list[NodeState] = []
                for node in nodes_list:
                    stripped_previews = tuple(
                        PreviewContent(
                            slot=p.slot,
                            media_type="text/plain",
                            title=p.title,
                            status="omitted",
                            reason="workspace or capture policy changed during scan",
                        )
                        for p in node.previews
                    )
                    stripped_nodes.append(
                        NodeState(
                            id=node.id,
                            parent_id=node.parent_id,
                            label=node.label,
                            node_kind=node.node_kind,
                            updated_at=node.updated_at,
                            kind=node.kind,
                            metric=node.metric,
                            fields=dict(node.fields),
                            attachments=(),
                            previews=stripped_previews,
                        )
                    )
                nodes_list = stripped_nodes
                stripped_items: list[ItemState] = []
                for item in items_list:
                    stripped_previews = tuple(
                        PreviewContent(
                            slot=p.slot,
                            media_type="text/plain",
                            title=p.title,
                            status="omitted",
                            reason="workspace or capture policy changed during scan",
                        )
                        for p in item.previews
                    )
                    stripped_items.append(
                        ItemState(
                            id=item.id,
                            group=item.group,
                            stage=item.stage,
                            kind=item.kind,
                            metric=item.metric,
                            label=item.label,
                            updated_at=item.updated_at,
                            fields=dict(item.fields),
                            attachments=(),
                            previews=stripped_previews,
                        )
                    )
                items_list = stripped_items

            # Construct directory NodeStates (root first, then sorted subdirectories).
            dir_nodes: list[NodeState] = []
            root_fields = dict(dir_meta.get("", {}))
            root_fields["entries_observed"] = entries_observed
            root_fields["excluded_entries"] = excluded_observed
            root_fields["redacted_files"] = redacted_files
            root_fields["nodes_emitted"] = emitted_nodes
            root_fields["truncated"] = bool(
                omissions.intersection({"time-cap", "directory-scan-cap", "depth-cap", "dir-child-cap", "node-cap"})
            )
            if omissions:
                root_fields["omissions"] = ",".join(sorted(omissions))[:140]

            dir_nodes.append(
                NodeState(
                    id="dir-root",
                    parent_id=None,
                    label="/",
                    node_kind="directory",
                    updated_at=now_iso,
                    fields=root_fields,
                )
            )
            for rel_dir in sorted(r for r in dir_node_by_rel if r):
                parent_rel = rel_dir.rsplit("/", 1)[0] if "/" in rel_dir else ""
                name = rel_dir.rsplit("/", 1)[-1]
                dir_nodes.append(
                    NodeState(
                        id=dir_node_by_rel[rel_dir],
                        parent_id=dir_node_by_rel[parent_rel],
                        label=_safe_display_label(name, rel_dir),
                        node_kind="directory",
                        updated_at=now_iso,
                        fields=dict(dir_meta.get(rel_dir, {})),
                    )
                )

            # Attach deterministic static relationships without executing code.
            links_by_id: dict[str, list[ObjectLink]] = {}
            for rel_dir, children in dir_children_rel.items():
                parent_id = dir_node_by_rel.get(rel_dir, "dir-root")
                src_dir_id = children.get("src")
                for name, child_id in children.items():
                    if name in _MANIFEST_NAMES:
                        links_by_id.setdefault(child_id, []).append(
                            ObjectLink("configures", parent_id, "Configures directory")
                        )
                        if src_dir_id and src_dir_id != child_id:
                            links_by_id[child_id].append(
                                ObjectLink("configures", src_dir_id, "Configures src/")
                            )
                    elif name in _DOC_ANCHOR_NAMES or name == ".josh-room.json":
                        links_by_id.setdefault(child_id, []).append(
                            ObjectLink("references", parent_id, "Documents scope")
                        )

            final_file_nodes = [
                NodeState(
                    id=n.id,
                    parent_id=n.parent_id,
                    label=n.label,
                    node_kind=n.node_kind,
                    updated_at=n.updated_at,
                    kind=n.kind,
                    metric=n.metric,
                    fields=dict(n.fields),
                    attachments=n.attachments,
                    previews=n.previews,
                    links=tuple(links_by_id.get(n.id, ())),
                )
                for n in nodes_list
            ]
            final_items = [
                ItemState(
                    id=it.id,
                    group=it.group,
                    stage=it.stage,
                    kind=it.kind,
                    metric=it.metric,
                    label=it.label,
                    updated_at=it.updated_at,
                    fields=dict(it.fields),
                    attachments=it.attachments,
                    previews=it.previews,
                    links=tuple(links_by_id.get(it.id, ())),
                )
                for it in items_list
            ]

            with self._lock:
                self._attachment_index = new_attachments

            summary: dict[str, str | int | float | bool | None] = {
                "provider_kind": "native-room-workspace",
                "freshness": "observed-live-workspace-not-snapshot",
                "capture_mode": policy.capture_mode,
                "policy_digest": _short_digest(policy.sha256),
                "room_linked": bool(ctx.get("linked")),
                "entries_observed": entries_observed,
                "nodes_emitted": len(dir_nodes) + len(final_file_nodes),
                "files_emitted": len(final_items),
                "excluded_entries": excluded_observed,
                "redacted_files": redacted_files,
                "truncated": bool(root_fields["truncated"]),
                "policy_drifted": policy_drifted,
            }
            if omissions:
                summary["omissions"] = ",".join(sorted(omissions))[:140]

            return ViewState(
                taken_at=now_iso,
                since=now_iso,
                groups=tuple(groups_map.values()),
                items=tuple(final_items),
                nodes=(*dir_nodes, *final_file_nodes),
                summary=summary,
            )
        finally:
            os.close(root_fd)

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        with self._lock:
            record = self._attachment_index.get((item_id, name))
        if record is None:
            return None
        try:
            root_fd = _directory(self._root)
        except OSError:
            return None
        try:
            root_stat = os.fstat(root_fd)
            if (root_stat.st_dev, root_stat.st_ino) != record.root_identity:
                return None
            policy = self._capture_policy(root_fd)
            if policy.sha256 != record.policy_sha256 or policy.is_excluded("/".join(record.rel_parts)):
                return None
            root_dev = root_stat.st_dev
            root_mount = _safe_mount_id(root_fd)
            dir_parts = record.rel_parts[:-1]
            filename = record.rel_parts[-1]
            dir_fd = self._open_relative_dir(root_fd, dir_parts, root_dev, root_mount)
            try:
                dir_path = self._root.joinpath(*dir_parts) if dir_parts else self._root
                expected_info = (
                    os.stat(filename, dir_fd=dir_fd, follow_symlinks=False)
                    if _HAS_DIR_FD
                    else os.lstat(dir_path / filename)
                )
                if not stat.S_ISREG(expected_info.st_mode) or expected_info.st_dev != root_dev:
                    return None
                data = self._read_regular_file_bytes(
                    dir_fd, dir_path, filename, expected_info, self._max_attachment_bytes
                )
            finally:
                os.close(dir_fd)
            if data is None or len(data) != record.size:
                return None
            if hashlib.sha256(data).hexdigest() != record.sha256:
                return None
            return Attachment(name=name, media_type=record.media_type, data=data)
        except (OSError, ValueError):
            return None
        finally:
            os.close(root_fd)


class SavedCapsuleManifestProvider:
    """Read-only ViewStateProvider for a verified immutable `LogicalJat` descriptor.

    Projects the canonical Room Store recovery point (`workspace` Restic snapshot +
    `rcc_environment`, `homebrew_recovery`, `hauler_content` verified component anchors)
    without invoking Hauler's broad `serve(mode=files)` (`0.0.0.0:8080`).
    """

    def __init__(
        self,
        manifest: Path | str | LogicalJat,
        *,
        sensitivity: str = "internal",
    ) -> None:
        level = sensitivity.lower().strip()
        if level not in SENSITIVITY_LEVELS:
            raise ViewValidationError("invalid-sensitivity", "unsupported sensitivity level")
        self._sensitivity = level
        if isinstance(manifest, LogicalJat):
            self._logical_jat = manifest
        else:
            self._logical_jat = self._read_logical_jat(Path(manifest))
        self._descriptor = self._build_descriptor()

    @staticmethod
    def _read_logical_jat(path: Path) -> LogicalJat:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_LOGICAL_JAT_BYTES:
                raise ViewValidationError("invalid-capsule-manifest", "capsule manifest file is unsafe or oversized")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != metadata.st_dev
                    or opened.st_ino != metadata.st_ino
                    or opened.st_size > MAX_LOGICAL_JAT_BYTES
                ):
                    raise ViewValidationError("invalid-capsule-manifest", "capsule manifest changed during read")
                raw = os.read(fd, MAX_LOGICAL_JAT_BYTES + 1)
            finally:
                os.close(fd)
            return LogicalJat.from_json(raw)
        except ViewValidationError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise ViewValidationError("invalid-capsule-manifest", "capsule manifest failed LogicalJat validation") from error

    def _build_descriptor(self) -> ViewDescriptor:
        doc = self._logical_jat.to_dict()
        safe_room = _safe_display_label(str(doc["room_id"]), "capsule-room-id")
        return ViewDescriptor(
            title=f"Saved Capsule: {safe_room}"[:96],
            stages=(
                StageDescriptor("workspace", "Workspace", 0),
                StageDescriptor("component", "Component", 1, terminal=True),
            ),
            group_label="Section",
            legend={
                "verified": LegendEntry("Recorded Anchor", "#22C55E", "solid"),
                "hauler": LegendEntry("Hauler Content", "#38BDF8", "notch"),
                "unavailable": LegendEntry("Archive Packed", "#F59E0B", "hollow"),
            },
            metric=MetricDescriptor("Archive Size", min=0.0, max=1024.0, unit="MiB"),
            view="3d",
            tool="josh-room",
            tool_version="0.1.0",
            sensitivity=self._sensitivity,
        )

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        doc = self._logical_jat.to_dict()
        workspace = doc["workspace"]
        components = doc["components"]
        created_at = str(doc["created_at"])

        groups = (
            GroupState("dir-workspace", "/workspace"),
            GroupState("dir-components", "/components"),
        )
        root_node = NodeState(
            id="dir-root",
            parent_id=None,
            label="/",
            node_kind="directory",
            updated_at=created_at,
            fields={
                "logical_jat_id": doc["logical_jat_id"],
                "room_id": doc["room_id"],
                "dimension_id": doc["dimension_id"],
                "capture_mode": doc.get("capture_mode", "full-repository"),
                "descriptor_sha": _short_digest(self._logical_jat.sha256),
                "freshness": "immutable-logical-jat-descriptor",
            },
        )
        ws_dir = NodeState(
            id="dir-workspace",
            parent_id="dir-root",
            label="workspace",
            node_kind="directory",
            updated_at=created_at,
            fields={"engine": workspace["engine"], "source_path": workspace["source_path"]},
        )
        comp_dir = NodeState(
            id="dir-components",
            parent_id="dir-root",
            label="components",
            node_kind="directory",
            updated_at=created_at,
            fields={"present_components": sum(1 for v in components.values() if v is not None)},
        )

        ws_preview_json = json.dumps(
            {
                "engine": workspace["engine"],
                "snapshot_prefix": _short_digest(workspace["snapshot_id"]),
                "tree_prefix": _short_digest(workspace["tree_id"]),
                "logical_bytes": workspace["logical_bytes"],
                "data_added": workspace["data_added"],
                "data_added_packed": workspace["data_added_packed"],
                "capture_mode": doc.get("capture_mode", "full-repository"),
                "source_platform": doc["producer"]["source_platform"],
            },
            indent=2,
            sort_keys=True,
        )
        ws_previews = (
            PreviewContent(
                slot="json",
                media_type="application/json",
                title="Recorded workspace snapshot",
                status="available",
                body=ws_preview_json,
                language="json",
            ),
            PreviewContent(
                slot="source",
                media_type="text/plain",
                title="Nested archive paths",
                status="unavailable",
                reason="saved capsule wraps workspace in a tar.zst/restic snapshot; stage and inspect via bounded restore",
            ),
        )
        ws_mib = round(min(1024.0, max(0.5, workspace["logical_bytes"] / (1024 * 1024))), 2)
        ws_item = ItemState(
            id="anchor-workspace",
            group="dir-workspace",
            stage="workspace",
            kind="verified",
            metric=ws_mib,
            label="workspace.snapshot",
            updated_at=created_at,
            fields={
                "engine": workspace["engine"],
                "snapshot_prefix": _short_digest(workspace["snapshot_id"]),
                "logical_bytes": workspace["logical_bytes"],
            },
            previews=ws_previews,
            links=(ObjectLink("artifact_of", "dir-root", "Capsule recovery point"),),
        )
        ws_node = NodeState(
            id=ws_item.id,
            parent_id="dir-workspace",
            label=ws_item.label,
            node_kind="artifact",
            updated_at=created_at,
            kind=ws_item.kind,
            metric=ws_item.metric,
            fields=dict(ws_item.fields),
            previews=ws_item.previews,
            links=ws_item.links,
        )

        items = [ws_item]
        nodes = [root_node, ws_dir, comp_dir, ws_node]

        for name in ("rcc_environment", "homebrew_recovery", "hauler_content"):
            comp = components.get(name)
            if comp is None:
                continue
            comp_id = f"comp-{name.replace('_', '-')}"
            comp_mib = round(min(1024.0, max(0.5, comp["archive_size"] / (1024 * 1024))), 2)
            payload: dict[str, object] = {
                "kind": comp["kind"],
                "member_basename": comp["member_basename"],
                "archive_size": comp["archive_size"],
                "archive_sha_prefix": _short_digest(comp["archive_sha256"]),
                "snapshot_prefix": _short_digest(comp["snapshot"]["snapshot_id"]),
            }
            if name == "hauler_content":
                payload["hauler_version"] = comp.get("hauler_version", "unrecorded")
                payload["reference_count"] = len(comp.get("references", ()))
                payload["references_sample"] = [
                    {
                        "kind": ref.get("kind", "unknown"),
                        "media_type": ref.get("media_type", "unknown"),
                        "digest_prefix": f"sha256:{_short_digest(ref['digest'])}",
                    }
                    for ref in comp.get("references", ())[:20]
                ]
            elif name == "rcc_environment":
                payload["platform"] = comp["platform"]
                payload["rcc_version"] = comp["rcc_version"]
                payload["robot_relative_path"] = comp["robot_relative_path"]

            preview = PreviewContent(
                slot="json",
                media_type="application/json",
                title=comp["member_basename"],
                status="available",
                body=json.dumps(payload, indent=2, sort_keys=True),
                language="json",
            )
            legend_kind = "hauler" if name == "hauler_content" else "verified"
            comp_fields: dict[str, str | int | float | bool | None] = {
                "component_kind": comp["kind"],
                "archive_bytes": comp["archive_size"],
                "archive_sha_prefix": _short_digest(comp["archive_sha256"]),
            }
            comp_links = (ObjectLink("artifact_of", "anchor-workspace", "Bound to workspace snapshot"),)
            item = ItemState(
                id=comp_id,
                group="dir-components",
                stage="component",
                kind=legend_kind,
                metric=comp_mib,
                label=comp["member_basename"],
                updated_at=created_at,
                fields=comp_fields,
                previews=(preview,),
                links=comp_links,
            )
            items.append(item)
            nodes.append(
                NodeState(
                    id=item.id,
                    parent_id="dir-components",
                    label=item.label,
                    node_kind="artifact",
                    updated_at=created_at,
                    kind=item.kind,
                    metric=item.metric,
                    fields=dict(item.fields),
                    previews=item.previews,
                    links=item.links,
                )
            )

        return ViewState(
            taken_at=created_at,
            since=created_at,
            groups=groups,
            items=tuple(items),
            nodes=tuple(nodes),
            summary={
                "provider_kind": "saved-capsule-manifest",
                "freshness": "immutable-logical-jat-descriptor",
                "logical_jat_id": doc["logical_jat_id"],
                "room_id": doc["room_id"],
                "capture_mode": doc.get("capture_mode", "full-repository"),
                "component_count": len(items) - 1,
            },
        )

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        return None
