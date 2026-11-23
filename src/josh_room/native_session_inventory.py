"""Bounded ordinary-session metadata inventory, never capture authority."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

MAX_ENTRIES = 4096
MAX_HEADER_BYTES = 256 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 1.0


class _Limit(Exception):
    pass


def _result():
    return {"state": "complete", "exact": 0, "descendant": 0}


def _match(result, cwd, workspace):
    if not isinstance(cwd, str) or not cwd or len(cwd) > 4096 or "\0" in cwd:
        result.update(state="partial", reason="unsupported-format")
        return
    path = Path(cwd)
    if not path.is_absolute() or ".." in path.parts:
        result.update(state="partial", reason="unsupported-format")
        return
    try:
        path = path.resolve()
    except (OSError, RuntimeError, ValueError):
        result.update(state="partial", reason="discovery-failed")
        return
    if path == workspace:
        result["exact"] += 1
    elif path.is_relative_to(workspace):
        result["descendant"] += 1


def _home(value, default):
    path = Path(value) if value else Path.home() / default
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid-home")
    # Permit OS aliases above the home, but never a linked session root.
    return path.parent.resolve() / path.name


@contextmanager
def _directory(path, *, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _inspect_sidecar(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("unsafe-sidecar")
    return info


def _codex(workspace):
    result = _result()
    deadline = time.monotonic() + MAX_SECONDS
    visited = read_bytes = 0
    entries_exhausted = False
    identities = set()

    def check():
        if time.monotonic() > deadline:
            raise _Limit

    def mark_partial(reason):
        result.update(state="partial", reason=reason)

    def walk(directory, depth=0):
        nonlocal visited, read_bytes, entries_exhausted
        entries = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    check()
                    if visited == MAX_ENTRIES:
                        entries_exhausted = True
                        break
                    visited += 1
                    entries.append(entry)
        except FileNotFoundError:
            mark_partial("source-changed")
            return
        except OSError:
            mark_partial("discovery-failed")
            return
        # Within each collected batch, inspect recent date directories/rollouts first.
        for entry in sorted(entries, key=lambda item: item.name, reverse=True):
            check()
            try:
                is_link = entry.is_symlink()
                is_dir = False if is_link else entry.is_dir(follow_symlinks=False)
            except FileNotFoundError:
                mark_partial("source-changed")
                continue
            except OSError:
                mark_partial("discovery-failed")
                continue
            if is_link:
                mark_partial("unsafe-entry")
            elif is_dir:
                if entries_exhausted:
                    continue
                if depth >= 4:
                    mark_partial("inventory-limit")
                    continue
                try:
                    with _directory(entry.name, parent=directory) as child:
                        walk(child, depth + 1)
                except FileNotFoundError:
                    mark_partial("source-changed")
                except OSError as error:
                    mark_partial("unsafe-entry" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "discovery-failed")
            elif entry.name.endswith(".jsonl"):
                try:
                    fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    mark_partial("source-changed")
                    continue
                except OSError as error:
                    mark_partial("unsafe-entry" if error.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR} else "discovery-failed")
                    continue
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                        mark_partial("unsafe-entry")
                        continue
                    remaining = MAX_READ_BYTES - read_bytes
                    if not remaining:
                        raise _Limit
                    data = bytearray()
                    # Stop exactly at the newline without reading transcript bytes ahead.
                    for offset in range(min(MAX_HEADER_BYTES, remaining)):
                        if offset % 256 == 0:
                            check()
                        byte = os.read(fd, 1)
                        if not byte:
                            break
                        read_bytes += 1
                        data.extend(byte)
                        if byte == b"\n":
                            break
                    else:
                        mark_partial("inventory-limit")
                        continue
                    check()
                except OSError:
                    mark_partial("discovery-failed")
                    continue
                finally:
                    os.close(fd)
                try:
                    header = json.loads(data)
                    payload = header.get("payload") if isinstance(header, dict) else None
                    if header.get("type") != "session_meta" or not isinstance(payload, dict):
                        raise ValueError
                    identity = payload.get("id")
                    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                        raise ValueError
                except (ValueError, AttributeError, UnicodeError, RecursionError):
                    mark_partial("unsupported-format")
                    continue
                if identity not in identities:
                    identities.add(identity)
                    _match(result, payload.get("cwd"), workspace)
            elif entry.name.endswith(".jsonl.zst"):
                mark_partial("unsupported-format")

    try:
        with _directory(_home(os.environ.get("CODEX_HOME"), ".codex")) as home:
            for name in ("sessions", "archived_sessions"):
                if entries_exhausted:
                    break
                try:
                    directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
                except FileNotFoundError:
                    continue
                except OSError as error:
                    mark_partial("unsafe-entry" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "discovery-failed")
                    continue
                try:
                    walk(directory)
                finally:
                    os.close(directory)
            if entries_exhausted:
                mark_partial("inventory-limit")
    except FileNotFoundError:
        pass
    except _Limit:
        mark_partial("inventory-limit")
    except (OSError, ValueError):
        result = {**_result(), "state": "unavailable", "reason": "discovery-failed"}
    return result


def _t3(workspace):
    result = _result()
    deadline = time.monotonic() + MAX_SECONDS
    opened = False
    try:
        home_value = os.environ.get("T3CODE_HOME", "").strip()
        state_dir = "dev" if not home_value and os.environ.get("VITE_DEV_SERVER_URL") else "userdata"
        if home_value == "~":
            home_value = str(Path.home())
        elif home_value.startswith(("~/", "~\\")):
            home_value = str(Path.home() / home_value[2:].lstrip("/"))
        with _directory(_home(home_value, ".t3")) as home, _directory(state_dir, parent=home) as directory:
            try:
                info = os.stat("statev2.sqlite", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                # Pre-v2 homes are not empty. Do not migrate/read their older
                # schema implicitly or include retained rollback databases.
                os.stat("state.sqlite", dir_fd=directory, follow_symlinks=False)
                return {**result, "state": "unsupported", "reason": "unsupported-format"}
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError("unsafe-database")
            path = Path(f"/proc/self/fd/{directory}/statev2.sqlite")
            opened = True
            if _inspect_sidecar(directory, "statev2.sqlite-journal") is not None:
                raise ValueError("database-busy")
            wal_info = _inspect_sidecar(directory, "statev2.sqlite-wal")
            shm_info = _inspect_sidecar(directory, "statev2.sqlite-shm")
            stable = wal_info is None and shm_info is None
            # Closed WAL-mode databases otherwise gain new WAL/SHM files merely
            # from opening mode=ro. Fence a main-file-only immutable observation.
            uri = path.as_uri() + ("?mode=ro&immutable=1" if stable else "?mode=ro")
            limited = False
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                connection.execute("BEGIN")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "projection_projects" not in tables:
                    return {**_result(), "state": "unsupported", "reason": "unsupported-format"}
                projects = {row[1] for row in connection.execute("PRAGMA table_info(projection_projects)")}
                thread_tables = [table for table in ("orchestration_v2_projection_threads", "projection_threads") if table in tables]
                if not thread_tables or not {"project_id", "workspace_root", "deleted_at"} <= projects:
                    return {**_result(), "state": "unsupported", "reason": "unsupported-format"}
                seen = 0
                try:
                    for table in thread_tables:
                        v2 = table.startswith("orchestration_")
                        required = {"thread_id", "project_id", "deleted_at", "payload_json" if v2 else "worktree_path"}
                        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                        if not required <= columns:
                            return {**_result(), "state": "unsupported", "reason": "unsupported-format"}
                        cwd = "nullif(json_extract(t.payload_json, '$.worktreePath'), '')" if v2 else "nullif(t.worktree_path, '')"
                        # A v2 row supersedes its legacy identity, including deletion.
                        superseded = "AND NOT EXISTS (SELECT 1 FROM orchestration_v2_projection_threads v WHERE v.thread_id=t.thread_id)" if not v2 and len(thread_tables) == 2 else ""
                        query = f"""SELECT coalesce({cwd}, p.workspace_root) FROM {table} t
                            JOIN projection_projects p ON p.project_id=t.project_id
                            WHERE t.deleted_at IS NULL AND p.deleted_at IS NULL {superseded} LIMIT ?"""
                        for (cwd,) in connection.execute(query, (MAX_ENTRIES + 1 - seen,)):
                            seen += 1
                            if seen > MAX_ENTRIES or time.monotonic() > deadline:
                                raise _Limit
                            _match(result, cwd, workspace)
                except _Limit:
                    limited = True
            after = os.stat("statev2.sqlite", dir_fd=directory, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            journal_after = _inspect_sidecar(directory, "statev2.sqlite-journal")
            wal_after = _inspect_sidecar(directory, "statev2.sqlite-wal")
            shm_after = _inspect_sidecar(directory, "statev2.sqlite-shm")
            if (
                any(getattr(info, key) != getattr(after, key) for key in fields)
                or journal_after is not None
                or (stable and (wal_after is not None or shm_after is not None))
            ):
                raise ValueError("database-changed")
            if limited:
                result.update(state="partial", reason="inventory-limit")
    except FileNotFoundError:
        if opened:
            result = {**_result(), "state": "unavailable", "reason": "source-changed"}
    except sqlite3.Error as error:
        result = {**_result(), "state": "unavailable", "reason": "inventory-limit" if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT else "discovery-failed"}
    except (OSError, ValueError):
        result = {**_result(), "state": "unavailable", "reason": "discovery-failed"}
    return result


def inventory(workspace):
    """Only counts/closed diagnostics leave this process; no paths or session IDs."""
    if sys.platform != "linux":
        tools = {tool: {**_result(), "state": "unsupported", "reason": "unsupported-platform"} for tool in ("codex", "t3")}
    else:
        workspace = Path(workspace).resolve(strict=True)
        tools = {"codex": _codex(workspace), "t3": _t3(workspace)}
    return {"contract_version": 1, "tools": tools}
