"""Bounded ordinary-session metadata inventory, never capture authority."""

from __future__ import annotations

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


def _home(variable, default):
    path = Path(os.environ.get(variable, str(Path.home() / default)))
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


def _codex(workspace):
    result = _result()
    deadline = time.monotonic() + MAX_SECONDS
    visited = read_bytes = 0
    identities = set()

    def check():
        if visited > MAX_ENTRIES or read_bytes > MAX_READ_BYTES or time.monotonic() > deadline:
            raise _Limit

    def walk(directory, depth=0):
        nonlocal visited, read_bytes
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                visited += 1
                check()
                entries.append(entry)
        # Recent date directories/rollouts first when a large home reaches a bound.
        for entry in sorted(entries, key=lambda item: item.name, reverse=True):
            check()
            if entry.is_symlink():
                result.update(state="partial", reason="unsafe-entry")
            elif entry.is_dir(follow_symlinks=False):
                if depth >= 4:
                    raise _Limit
                try:
                    with _directory(entry.name, parent=directory) as child:
                        walk(child, depth + 1)
                except FileNotFoundError:
                    result.update(state="partial", reason="source-changed")
            elif entry.name.endswith(".jsonl"):
                try:
                    fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    result.update(state="partial", reason="source-changed")
                    continue
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                        result.update(state="partial", reason="unsafe-entry")
                        continue
                    data = stream.readline(min(MAX_HEADER_BYTES, MAX_READ_BYTES - read_bytes) + 1)
                    read_bytes += len(data)
                    check()
                    if len(data) > MAX_HEADER_BYTES:
                        raise _Limit
                try:
                    header = json.loads(data)
                    payload = header.get("payload") if isinstance(header, dict) else None
                    if header.get("type") != "session_meta" or not isinstance(payload, dict):
                        raise ValueError
                    identity = payload.get("id")
                    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                        raise ValueError
                except (ValueError, AttributeError, UnicodeError, RecursionError):
                    result.update(state="partial", reason="unsupported-format")
                    continue
                if identity not in identities:
                    identities.add(identity)
                    _match(result, payload.get("cwd"), workspace)
            elif entry.name.endswith(".jsonl.zst"):
                result.update(state="partial", reason="unsupported-format")

    try:
        with _directory(_home("CODEX_HOME", ".codex")) as home:
            for name in ("sessions", "archived_sessions"):
                try:
                    with _directory(name, parent=home) as directory:
                        walk(directory)
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        pass
    except _Limit:
        result.update(state="partial", reason="inventory-limit")
    except (OSError, ValueError):
        result.update(state="unavailable", reason="discovery-failed")
    return result


def _t3(workspace):
    result = _result()
    deadline = time.monotonic() + MAX_SECONDS
    opened = False
    try:
        with _directory(_home("T3CODE_HOME", ".t3")) as home, _directory("userdata", parent=home) as directory:
            info = os.stat("statev2.sqlite", dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError("unsafe-database")
            path = Path(f"/proc/self/fd/{directory}/statev2.sqlite")
            opened = True
            wal = path.with_name("statev2.sqlite-wal")
            journal = path.with_name("statev2.sqlite-journal")
            if journal.exists():
                raise ValueError("database-busy")
            stable = not wal.exists()
            # Closed WAL-mode databases otherwise gain new WAL/SHM files merely
            # from opening mode=ro. Fence a main-file-only immutable observation.
            uri = path.as_uri() + ("?mode=ro&immutable=1" if stable else "?mode=ro")
            with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                connection.execute("BEGIN")
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                projects = {row[1] for row in connection.execute("PRAGMA table_info(projection_projects)")}
                thread_tables = [table for table in ("orchestration_v2_projection_threads", "projection_threads") if table in tables]
                if not thread_tables or not {"project_id", "workspace_root", "deleted_at"} <= projects:
                    return {**result, "state": "unsupported", "reason": "unsupported-format"}
                seen = 0
                for table in thread_tables:
                    v2 = table.startswith("orchestration_")
                    required = {"thread_id", "project_id", "deleted_at", "payload_json" if v2 else "worktree_path"}
                    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                    if not required <= columns:
                        return {**_result(), "state": "unsupported", "reason": "unsupported-format"}
                    cwd = "json_extract(t.payload_json, '$.worktreePath')" if v2 else "t.worktree_path"
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
                after = os.stat("statev2.sqlite", dir_fd=directory, follow_symlinks=False)
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                if any(getattr(info, key) != getattr(after, key) for key in fields) or journal.exists() or stable and wal.exists():
                    raise ValueError("database-changed")
    except FileNotFoundError:
        if opened:
            result.update(state="unavailable", reason="source-changed")
    except _Limit:
        result.update(state="partial", reason="inventory-limit")
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
