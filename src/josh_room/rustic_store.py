"""Pinned Rustic adapter sharing the existing Room Store security/process contract.

Restic remains the default. Repository format, models and maintenance plans stay
compatible; Rustic-specific CLI and output handling live here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from .adapter_contract import CancellationToken
from .parent_inventory import read_inventory, write_inventory
from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)
from .restic_store import (
    _BASE_ENVIRONMENT,
    MAX_CAPTURE_BYTES,
    MAX_SNAPSHOT_INVENTORY,
    BackupProgress,
    BackupSummary,
    RepositoryInfo,
    ResticStore,
    ResticStoreError,
    ResticStoreErrorCode,
    SnapshotEntry,
    SnapshotInfo,
    SnapshotInventoryItem,
    _nonnegative_integer,
    _progress,
    _validate_snapshot_id,
    parse_backup_events,
    parse_repository_config,
)

RUSTIC_VERSION = "0.11.4"
_ID = re.compile(r"^[0-9a-f]{64}$")


class RusticStoreError(ResticStoreError):
    """Preserve callers' existing sanitized error and recovery contract."""

    def __init__(self, code: str | ResticStoreErrorCode, **kwargs: Any) -> None:
        super().__init__(ResticStoreErrorCode(code), **kwargs)
        self.args = (str(self).replace("restic", "rustic"),)


def _json(raw: bytes) -> Any:
    if len(raw) > MAX_CAPTURE_BYTES:
        raise RusticStoreError("invalid-output")
    try:
        return json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise RusticStoreError("invalid-output") from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("unsupported JSON constant")


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _required_id(value: object) -> str:
    if not _valid_id(value):
        raise RusticStoreError("invalid-output")
    return str(value)


def _count(summary: dict[str, Any], name: str) -> int:
    value = summary.get(name)
    if type(value) is not int or value < 0 or value > (1 << 63) - 1:
        raise RusticStoreError("invalid-output")
    return value


def parse_backup_output(
    raw: bytes,
    *,
    parent: str | None,
    parent_tree_id: str | None = None,
) -> BackupSummary:
    """Map Rustic's `backup --json` snapshot document to the used Restic fields."""
    value = _json(raw)
    if not isinstance(value, dict) or not isinstance(value.get("summary"), dict):
        raise RusticStoreError("invalid-output")
    summary = value["summary"]
    tree_id = _required_id(value.get("tree"))
    observed_parent = value.get("parent")
    if observed_parent is not None and not _valid_id(observed_parent):
        raise RusticStoreError("invalid-output")
    if parent is not None and observed_parent != parent:
        raise RusticStoreError("invalid-output")
    files_new = _count(summary, "files_new")
    files_changed = _count(summary, "files_changed")
    files_unmodified = _count(summary, "files_unmodified")
    data_added = _count(summary, "data_added")
    data_added_packed = _count(summary, "data_added_packed")
    bytes_processed = _count(summary, "total_bytes_processed")
    raw_snapshot_id = value.get("id")
    is_noop = bool(
        parent is not None
        and (raw_snapshot_id is None or raw_snapshot_id == parent)
        and files_new == 0
        and files_changed == 0
        and data_added == 0
        and data_added_packed == 0
    )
    if is_noop and (parent_tree_id is None or tree_id != parent_tree_id):
        raise RusticStoreError("invalid-output")
    snapshot_id = None if is_noop else _required_id(raw_snapshot_id)
    return BackupSummary(
        snapshot_id,
        files_new,
        files_changed,
        files_unmodified,
        data_added,
        data_added_packed,
        bytes_processed,
        0,
        force_scan=False,
        effective_parent_id=observed_parent,
    )


def _snapshot_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise RusticStoreError("invalid-output")
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RusticStoreError("invalid-output")
        if "snapshots" in item:
            if set(item) != {"group_key", "snapshots"} or not isinstance(
                item["snapshots"], list
            ):
                raise RusticStoreError("invalid-output")
            group = item["snapshots"]
            if any(not isinstance(snapshot, dict) for snapshot in group):
                raise RusticStoreError("invalid-output")
            records.extend(group)
        else:
            records.append(item)
    if len(records) > MAX_SNAPSHOT_INVENTORY:
        raise RusticStoreError("invalid-output")
    return records


def _snapshot_record(
    record: dict[str, Any], expected_id: str | None = None
) -> SnapshotInfo:
    snapshot_id = _required_id(record.get("id"))
    tree_id = _required_id(record.get("tree"))
    parent_id = record.get("parent")
    when = record.get("time")
    paths = record.get("paths")
    try:
        timestamp = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        raise RusticStoreError("invalid-output") from None
    if (
        (expected_id is not None and snapshot_id != expected_id)
        or (parent_id is not None and not _valid_id(parent_id))
        or not isinstance(when, str)
        or not when
        or len(when) > 128
        or timestamp.tzinfo is None
        or not isinstance(paths, list)
        or not paths
        or len(paths) > 256
        or any(
            not isinstance(path, str) or not path or "\x00" in path for path in paths
        )
    ):
        raise RusticStoreError("invalid-output")
    return SnapshotInfo(snapshot_id, tree_id, parent_id, when, tuple(paths))


class _Diagnostics:
    """Drain diagnostics without retaining paths, credentials or raw messages."""

    def __init__(self, stream: Any) -> None:
        self.present = False
        self.missing = False
        self.failed = False
        self.thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self.thread.start()

    def _drain(self, stream: Any) -> None:
        # Only this pinned source fingerprint can authorize initialization.
        fingerprint = b"\nNo repository config file found for `"
        tail = b"\n"
        try:
            while chunk := stream.read(4096):
                self.present = True
                self.missing |= fingerprint in tail + chunk
                tail = chunk[-len(fingerprint) :]
        except (OSError, ValueError):
            self.failed = True


class RusticStore(ResticStore):
    """Small specialization of the existing adapter, not a provider framework."""

    engine = "rustic"
    engine_version = RUSTIC_VERSION
    _stderr = subprocess.PIPE

    def __init__(self, *, executable: str = "rustic", **kwargs: Any) -> None:
        super().__init__(executable=executable, **kwargs)
        self._scratch: tempfile.TemporaryDirectory | None = None
        self._profile: Path | None = None
        self._diagnostics: _Diagnostics | None = None
        self._orphan: str | None = None

    def __enter__(self) -> Self:
        super().__enter__()
        try:
            self._password_file = self._password_file.resolve(strict=True)
            self._cache_dir = self._cache_dir.resolve(strict=True)
            # The pinned Windows TLS verifier uses the OS trust store and does
            # not honor SSL_CERT_FILE. Never silently ignore a Dimension CA.
            if self._ca_bundle is not None and sys.platform.startswith("win"):
                raise RusticStoreError("invalid-configuration")
            self._scratch = tempfile.TemporaryDirectory(
                prefix=".rustic-", dir=self._cache_dir
            )
            scratch = Path(self._scratch.name)
            protect_private_directory(scratch)
            self._profile = scratch / "profile.toml"
            profile = "[global]\n"
            if self._repository.startswith("s3:"):
                if "AWS_ACCESS_KEY_ID" not in self._provider_env:
                    raise RusticStoreError("invalid-configuration")
                # Quote the substituted values before substitution, so even a
                # credential containing TOML syntax remains a single string.
                remote = urlsplit(self._repository[3:])
                parts = remote.path.lstrip("/").split("/", 1)
                options = {
                    "endpoint": f"{remote.scheme}://{remote.netloc}",
                    "bucket": parts[0],
                    "root": parts[1] if len(parts) == 2 else "",
                    "region": self._provider_env.get("AWS_DEFAULT_REGION", "us-east-1"),
                    "disable_config_load": "true",
                    "disable_ec2_metadata": "true",
                    "access_key_id": "${JOSH_ROOM_S3_ACCESS_JSON}",
                    "secret_access_key": "${JOSH_ROOM_S3_SECRET_JSON}",
                }
                if "AWS_SESSION_TOKEN" in self._provider_env:
                    options["session_token"] = "${JOSH_ROOM_S3_TOKEN_JSON}"
                profile += "[repository.options]\n"
                profile += (
                    "\n".join(
                        f"{key} = {value if value.startswith('${') else json.dumps(value)}"
                        for key, value in options.items()
                    )
                    + "\n"
                )
            self._profile.write_text(profile, encoding="utf-8")
            protect_private_file(self._profile)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args: object) -> None:
        try:
            super().__exit__(*args)
        finally:
            self._repository_info = None
            if self._scratch is not None:
                self._scratch.cleanup()
            self._scratch = None
            self._profile = None

    def _environment(self) -> dict[str, str]:
        self._ensure_open()
        assert self._scratch is not None and self._profile is not None
        # Do not inherit TLS overrides, AWS/Rustic config, hooks, telemetry,
        # password commands, or HOME/XDG authority from the invoking process.
        env = {
            key: os.environ[key]
            for key in _BASE_ENVIRONMENT
            if key in os.environ and "CA_" not in key and not key.startswith("SSL_")
        }
        env.update(
            {
                "LC_ALL": "C",
                "TZ": "UTC",
                "HOME": self._scratch.name,
                "USERPROFILE": self._scratch.name,
                "APPDATA": self._scratch.name,
                "XDG_CONFIG_HOME": self._scratch.name,
                "RUSTIC_USE_PROFILE": str(self._profile),
                "RUSTIC_PROFILE_SUBSTITUTE_ENV": "true",
                "RUSTIC_REPOSITORY": "opendal:s3"
                if self._repository.startswith("s3:")
                else self._repository,
                "RUSTIC_PASSWORD_FILE": str(self._password_file),
                "RUSTIC_CACHE_DIR": str(self._cache_dir),
                "RUSTIC_LOG_LEVEL": "warn",
                "RUSTIC_LOG_LEVEL_DRYRUN": "warn",
                # OpenDAL warns about expected 404 probes during init. Keep
                # dependency errors and all Rustic/core/backend warnings.
                "RUSTIC_LOG_LEVEL_DEPENDENCIES": "error",
            }
        )
        if self._ca_bundle is not None:
            env["SSL_CERT_FILE"] = str(self._ca_bundle)
            env["SSL_CERT_DIR"] = self._scratch.name
        for original, scoped in (
            ("AWS_ACCESS_KEY_ID", "JOSH_ROOM_S3_ACCESS_JSON"),
            ("AWS_SECRET_ACCESS_KEY", "JOSH_ROOM_S3_SECRET_JSON"),
            ("AWS_SESSION_TOKEN", "JOSH_ROOM_S3_TOKEN_JSON"),
        ):
            if original in self._provider_env:
                env[scoped] = json.dumps(self._provider_env[original])
        return env

    @contextmanager
    def _process(self, args: list[str], *, cwd: Path | None = None) -> Iterator[Any]:
        self._orphan = None
        if "--json-progress" not in args:
            args = ["--no-progress", *args]
        with super()._process(args, cwd=cwd or Path(self._scratch.name)) as process:
            diagnostics = _Diagnostics(process.stderr)
            self._diagnostics = diagnostics
            try:
                yield process
                diagnostics.thread.join(timeout=1)
                if diagnostics.thread.is_alive() or diagnostics.failed:
                    raise RusticStoreError(
                        "invalid-output", orphan_snapshot_id=self._orphan
                    )
                if process.poll() == 0 and diagnostics.present:
                    raise RusticStoreError(
                        "backup-errors" if "backup" in args else "command-failed",
                        orphan_snapshot_id=self._orphan,
                    )
            finally:
                self._terminate(process)
                diagnostics.thread.join(timeout=1)
                process.stderr.close()
                self._orphan = None

    def _ensure_version(self) -> None:
        _, output = self._capture(["version"])
        if output.strip() != f"rustic v{RUSTIC_VERSION}".encode():
            raise RusticStoreError("version-mismatch")

    def _capture(self, args: list[str], **kwargs: Any) -> tuple[int, bytes]:
        if args and args[0] == "check" and "--read-data-subset" in args:
            args = [*args, "--read-data"]
        return super()._capture(args, **kwargs)

    def _read_repository_info(
        self, *, allow_missing: bool = False
    ) -> RepositoryInfo | None:
        code, output = self._capture(
            ["cat", "config"], allowed_exit_codes=frozenset({0, 1})
        )
        if code:
            if (
                allow_missing
                and self._diagnostics is not None
                and self._diagnostics.missing
            ):
                return None
            self._raise_exit(code)
        info = parse_repository_config(json.dumps(_json(output)))
        if info.repository_format != 2:
            raise RusticStoreError("repository-format-mismatch")
        return info

    def initialize(self) -> RepositoryInfo:
        self._ensure_open()
        self._snapshot_info.clear()
        self._repository_info = None
        self._ensure_version()
        info = self._read_repository_info(allow_missing=True)
        if info is None:
            code, _ = self._capture(
                ["init", "--set-version", "2"], allowed_exit_codes=frozenset({0, 1})
            )
            # A failed init may be a creation race, but only a validated read
            # back can turn it into success. Never migrate or repair.
            info = self._read_repository_info(allow_missing=True)
            if info is None:
                raise RusticStoreError("initialize-failed", exit_code=code)
        self._repository_info = info
        return info

    def open_existing(self) -> RepositoryInfo:
        self._repository_info = None
        return super().open_existing()

    def _exclude_file(self, excludes: Path) -> Path:
        try:
            with Path(excludes).open("rb") as stream:
                raw = stream.read(self._max_capture_bytes + 1)
            if len(raw) > self._max_capture_bytes:
                raise RusticStoreError("invalid-configuration")
            patterns = raw.decode("utf-8").splitlines()
        except (OSError, UnicodeError):
            raise RusticStoreError("invalid-configuration") from None
        # Room's policy renders only this restricted, deterministic syntax.
        # Rustic's override globs INCLUDE by default; ! makes them exclusions.
        lines = []
        for pattern in patterns:
            if not pattern or pattern.startswith("#"):
                continue
            if pattern.startswith(("!", "/")) or any(
                c in pattern for c in "\x00\\[]{}"
            ):
                raise RusticStoreError("invalid-configuration")
            lines.append("!" + pattern)
        target = Path(self._scratch.name) / "excludes"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        protect_private_file(target)
        return target

    def backup(
        self,
        workspace: Path,
        parent: str | None = None,
        excludes: Path | None = None,
        on_progress: Callable[[BackupProgress], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> BackupSummary:
        self._require_initialized()
        root = self._validate_workspace(workspace, excludes)
        if on_progress is not None and not callable(on_progress):
            raise RusticStoreError("invalid-configuration")
        if cancellation is not None and cancellation.cancelled:
            raise RusticStoreError("cancelled")
        if parent is not None:
            _validate_snapshot_id(parent)
            self.snapshot(parent)
        force_scan = sys.platform.startswith("win")
        args = [
            "--json-progress",
            "--progress-interval",
            "500ms",
            "backup",
            "--no-scan",
            "--skip-if-unchanged",
        ]
        effective_parent = None if force_scan else parent
        if force_scan:
            args.append("--force")
        elif parent is not None:
            args.extend(["--parent", parent])
        if excludes is not None:
            args.extend(["--glob-file", str(self._exclude_file(excludes))])
        args.extend(["--", "."])
        with self._process(args, cwd=root) as process:

            def events() -> Iterator[bytes]:
                for raw in self._read_lines(process, cancellation):
                    value = _json(raw)
                    if not isinstance(value, dict):
                        raise RusticStoreError("invalid-output")
                    if value.get("message_type") not in {"status", "summary"}:
                        raise RusticStoreError("invalid-output")
                    if value["message_type"] == "status":
                        _progress(value)
                    if value.get("message_type") == "summary":
                        if "errors" in value:
                            raise RusticStoreError("invalid-output")
                        value["errors"] = 0  # Diagnostics are checked independently.
                        snapshot_id = value.get("snapshot_id")
                        if snapshot_id is None or snapshot_id == effective_parent:
                            if effective_parent is None or any(
                                _count(value, key) != 0
                                for key in (
                                    "files_new",
                                    "files_changed",
                                    "dirs_new",
                                    "dirs_changed",
                                    "data_added",
                                    "data_added_packed",
                                )
                            ):
                                raise RusticStoreError("invalid-output")
                            value["snapshot_id"] = None
                        if value.get("snapshot_id") is not None:
                            self._orphan = _required_id(value["snapshot_id"])
                    yield json.dumps(value).encode()

            summary = parse_backup_events(
                events(),
                on_progress=on_progress,
                max_event_bytes=self._max_json_event_bytes,
            )
            self._raise_exit(
                self._wait(process), orphan_snapshot_id=summary.snapshot_id
            )
            if cancellation is not None and cancellation.cancelled:
                raise RusticStoreError(
                    "cancelled", orphan_snapshot_id=summary.snapshot_id
                )
        if summary.snapshot_id is not None:
            observed = self.snapshot(summary.snapshot_id)
            if (
                effective_parent is not None
                and observed.parent_snapshot_id != effective_parent
            ):
                raise RusticStoreError(
                    "invalid-output", orphan_snapshot_id=summary.snapshot_id
                )
        self.data_added_bytes += summary.data_added
        return replace(
            summary, force_scan=force_scan, effective_parent_id=effective_parent
        )

    def snapshot(self, snapshot_id: str) -> SnapshotInfo:
        self._require_initialized()
        _validate_snapshot_id(snapshot_id)
        if snapshot_id in self._snapshot_info:
            return self._snapshot_info[snapshot_id]
        _, raw = self._capture(["--group-by", "", "snapshots", "--json", snapshot_id])
        records = _snapshot_records(_json(raw))
        if len(records) != 1:
            raise RusticStoreError("invalid-output")
        result = _snapshot_record(records[0], snapshot_id)
        self._snapshot_info[snapshot_id] = result
        return result

    def snapshots(self) -> tuple[SnapshotInventoryItem, ...]:
        self._require_initialized()
        _, raw = self._capture(["--group-by", "", "snapshots", "--json"])
        result = [_snapshot_record(record) for record in _snapshot_records(_json(raw))]
        if len({item.snapshot_id for item in result}) != len(result):
            raise RusticStoreError("invalid-output")
        return tuple(
            SnapshotInventoryItem(
                item.snapshot_id, item.tree_id, item.time, item.parent_snapshot_id
            )
            for item in result
        )

    def _tree_blob(self, tree_id: str) -> bytes:
        """Reuse only exact SHA-256 authenticated metadata blobs across saves."""
        directory = (
            self._cache_dir / "rustic-trees-v1" / self.repository_info.repository_id
        )
        path = directory / tree_id
        try:
            verify_private_path(directory.parent, directory=True)
            verify_private_path(directory, directory=True)
            verify_private_path(path, directory=False)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("unsafe cached tree")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("cached tree changed")
                raw = source.read(self._max_capture_bytes + 1)
            if (
                len(raw) <= self._max_capture_bytes
                and hashlib.sha256(raw).hexdigest() == tree_id
            ):
                return raw
        except (OSError, PrivatePathError, ValueError):
            pass  # Uncertain cache evidence is a miss, never repository truth.
        _, output = self._capture(["cat", "tree-blob", tree_id])
        raw = output[:-1]  # cat adds exactly one newline to the original bytes.
        if not output.endswith(b"\n") or hashlib.sha256(raw).hexdigest() != tree_id:
            raise RusticStoreError("invalid-output")
        temporary = None
        try:
            for parent in (directory.parent, directory):
                parent.mkdir(mode=0o700, exist_ok=True)
                protect_private_directory(parent)
            fd, name = tempfile.mkstemp(prefix=".tree-", dir=directory)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            protect_private_file(temporary)
            os.replace(temporary, path)
        except (OSError, PrivatePathError):
            pass  # Cache writes are optional; the authenticated blob is usable.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return raw

    def entries(
        self, snapshot_id: str, *, expected_tree_id: str | None = None
    ) -> Iterator[SnapshotEntry]:
        self._require_initialized()
        snapshot = self.snapshot(snapshot_id)
        if expected_tree_id is not None and expected_tree_id != snapshot.tree_id:
            raise RusticStoreError("invalid-output")
        # Separate exact-metadata caches from the experimental long-ls cache.
        cache = self._cache_dir / "rustic-inventory-v1"
        if expected_tree_id is not None:
            cached = read_inventory(
                cache,
                self._repository,
                self.repository_info.repository_id,
                snapshot_id,
                expected_tree_id,
            )
            if cached is not None:
                yield from cached
                return
        # v0.11.4 ls --json gives names only; ls --long loses special mode bits
        # and raw link targets. Authenticate exact tree blobs instead.
        stack = [(snapshot.tree_id, "", frozenset())]
        rows: list[SnapshotEntry] = []
        seen: set[str] = set()
        deadline = time.monotonic() + self._command_timeout
        while stack:
            tree_id, prefix, ancestors = stack.pop()
            if tree_id in ancestors or len(ancestors) > 256:
                raise RusticStoreError("invalid-output")
            if time.monotonic() >= deadline:
                raise RusticStoreError("timed-out")
            raw = self._tree_blob(tree_id)
            tree = _json(raw)
            if not isinstance(tree, dict) or not isinstance(tree.get("nodes"), list):
                raise RusticStoreError("invalid-output")
            for node in tree["nodes"]:
                row, subtree = parse_tree_node(node, prefix)
                if row.path in seen or len(rows) >= self._max_entries:
                    raise RusticStoreError("invalid-output")
                seen.add(row.path)
                rows.append(row)
                if subtree is not None:
                    stack.append((subtree, row.path, ancestors | {tree_id}))
                yield row
        write_inventory(
            cache,
            self._repository,
            self.repository_info.repository_id,
            snapshot_id,
            snapshot.tree_id,
            rows,
        )

    def restore(self, snapshot_id: str, destination: Path) -> None:
        self._require_initialized()
        _validate_snapshot_id(snapshot_id)
        target = Path(destination)
        try:
            parent = target.parent.resolve(strict=True)
        except OSError:
            raise RusticStoreError("invalid-configuration") from None
        if target.exists() or target.is_symlink() or not parent.is_dir():
            raise RusticStoreError("invalid-configuration")
        self._capture(["restore", snapshot_id, str(parent / target.name)])


def parse_tree_node(node: Any, prefix: str) -> tuple[SnapshotEntry, str | None]:
    """Decode repository-native metadata; unsupported names fail closed."""
    if not isinstance(node, dict):
        raise RusticStoreError("invalid-output")
    name = node.get("name")
    # Non-Unicode/escaped names require an explicit cross-platform policy.
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or any(c in name for c in "/\\:\x00")
        or len(name.encode("utf-8", errors="surrogatepass")) > 1024 * 1024
    ):
        raise RusticStoreError("invalid-output")
    kind = node.get("type")
    if kind not in {"file", "dir", "symlink", "socket", "fifo", "dev", "chardev"}:
        raise RusticStoreError("invalid-output")
    size = _nonnegative_integer(node.get("size", 0))
    mode = _nonnegative_integer(node.get("mode"), optional=True)
    if mode is not None and mode > 0xFFFFFFFF:
        raise RusticStoreError("invalid-output")
    link = node.get("linktarget")
    if node.get("linktarget_raw") is not None:
        raise RusticStoreError("invalid-output")
    if kind == "symlink":
        if (
            not isinstance(link, str)
            or not link
            or "\x00" in link
            or len(link.encode("utf-8")) > 1024 * 1024
        ):
            raise RusticStoreError("invalid-output")
    elif link is not None:
        raise RusticStoreError("invalid-output")
    subtree = _required_id(node.get("subtree")) if kind == "dir" else None
    if kind != "dir" and node.get("subtree") is not None:
        raise RusticStoreError("invalid-output")
    path = f"{prefix}/{name}" if prefix else name
    if len(path.encode("utf-8")) > 1024 * 1024:
        raise RusticStoreError("invalid-output")
    return SnapshotEntry(path, kind, size, mode, link), subtree
