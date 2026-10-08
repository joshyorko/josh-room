"""Canonical workspace capture exclusions shared with the VS Code extension."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DEFAULTS_PATH = Path(__file__).with_name("workspace_capture_defaults.json")
_IGNORE_NAME = ".josh-roomignore"
_IGNORE_LIMIT = 64 * 1024
_MAX_RULES = 128
_MAX_RULE_LENGTH = 1024
_MAX_PATTERN_SEGMENTS = 128


def _parts(pattern: str, *, rule: str = "capture pattern") -> tuple[str, ...]:
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > _MAX_RULE_LENGTH
        or pattern.startswith("/")
        or (len(pattern) >= 3 and pattern[1] == ":" and pattern[2] == "/")
        or "\\" in pattern
    ):
        raise ValueError(f"{rule} is invalid")
    raw_segments = tuple(pattern.split("/"))
    if len(raw_segments) > _MAX_PATTERN_SEGMENTS:
        raise ValueError(f"{rule} has too many path components")
    segments = tuple(
        segment
        for index, segment in enumerate(raw_segments)
        if segment != "**" or index == 0 or raw_segments[index - 1] != "**"
    )
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"{rule} is invalid")
    for segment in segments:
        if any(char in segment for char in "![]"):
            raise ValueError(f"{rule} uses unsupported glob syntax")
        if "**" in segment and segment != "**":
            raise ValueError(f"{rule} uses unsupported glob syntax")
    if segments == ("**",):
        raise ValueError(f"{rule} cannot exclude the whole workspace")
    return segments


@lru_cache(maxsize=8192)
def _glob_matches(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _glob_matches(pattern[1:], path) or bool(
            path and _glob_matches(pattern, path[1:])
        )
    if not path:
        return False
    import fnmatch

    if not fnmatch.fnmatchcase(path[0], pattern[0]):
        return False
    return _glob_matches(pattern[1:], path[1:])


def _rule_matches(path_parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if (
        len(pattern) == 2
        and pattern[0] == "**"
        and "*" not in pattern[1]
        and "?" not in pattern[1]
        and "[" not in pattern[1]
    ):
        return pattern[1] in path_parts
    if len(pattern) == 1 and pattern[0] != "**":
        return any(_glob_matches(pattern, (part,)) for part in path_parts)
    return any(
        _glob_matches(pattern, path_parts[:length])
        for length in range(1, len(path_parts) + 1)
    )


def _read_defaults() -> tuple[bytes, dict]:
    try:
        raw = _DEFAULTS_PATH.read_bytes()
        body = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("workspace capture defaults are invalid") from error
    if (
        not isinstance(body, dict)
        or set(body) != {"version", "exclude"}
        or body.get("version") != 1
    ):
        raise ValueError("unsupported workspace capture defaults")
    if not isinstance(body.get("exclude"), list) or not body["exclude"]:
        raise ValueError("workspace capture defaults are invalid")
    return raw, body


def _read_ignore(root: Path) -> bytes | None:
    path = root / _IGNORE_NAME
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("workspace ignore file is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _IGNORE_LIMIT:
        raise ValueError(
            "workspace ignore file must be a regular file of at most 64 KiB"
        )
    descriptor = None
    try:
        flags = (
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size > _IGNORE_LIMIT
        ):
            raise ValueError("workspace ignore file changed while opening")
        chunks = bytearray()
        while len(chunks) <= _IGNORE_LIMIT:
            chunk = os.read(descriptor, min(8192, _IGNORE_LIMIT + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > _IGNORE_LIMIT:
            raise ValueError("workspace ignore file exceeds 64 KiB")
        data = bytes(chunks)
    except OSError as error:
        raise ValueError("workspace ignore file could not be read safely") from error
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("workspace ignore file must be valid UTF-8") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return data


def _user_patterns(contents: bytes | None) -> tuple[tuple[str, ...], ...]:
    if contents is None:
        return ()
    patterns: list[tuple[str, ...]] = []
    for line in contents.decode("utf-8").split("\n"):
        rule = line.strip()
        if not rule or rule.startswith("#"):
            continue
        if rule.startswith("!"):
            raise ValueError("workspace ignore rules cannot reinclude paths")
        if len(patterns) >= _MAX_RULES:
            raise ValueError("workspace ignore file has too many rules")
        patterns.append(_parts(rule, rule="workspace ignore rule"))
    return tuple(patterns)


def _normalized_relative(relative_path: str | Path) -> tuple[str, ...] | None:
    value = str(relative_path)
    if not value or value.startswith("/") or "\\" in value:
        return None
    parts = tuple(value.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


@dataclass(frozen=True)
class CapturePolicy:
    """Compiled exclusion policy for one workspace and optional active runtime."""

    sha256: str
    patterns: tuple[tuple[str, ...], ...]
    active_runtime_relative: str | None

    def is_excluded(self, relative_path: str | Path) -> bool:
        parts = _normalized_relative(relative_path)
        if parts is None:
            return False
        # Git storage is indivisible. A branch named venv, for example, is
        # history, not a generated environment. Ancestor exclusions still apply.
        if ".git" in parts:
            index = parts.index(".git")
            return bool(index and self.is_excluded("/".join(parts[:index])))
        if self.active_runtime_relative:
            runtime = tuple(self.active_runtime_relative.split("/"))
            if parts[: len(runtime)] == runtime:
                return True
        return any(_rule_matches(parts, pattern) for pattern in self.patterns)

    def resolved_restic_excludes(self, root: Path) -> tuple[str, ...]:
        """Resolve pruned paths so broad globs cannot discard Git internals."""
        values = []
        for directory, directories, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in (*directories, *files):
                relative = (parent / name).relative_to(root).as_posix()
                if self.is_excluded(relative):
                    if "\n" in relative or "\r" in relative:
                        raise ValueError("workspace exclusion contains an unsafe path")
                    # Restic uses Go path.Match; quote glob metacharacters.
                    absolute = (root.resolve() / relative).as_posix()
                    if "\n" in absolute or "\r" in absolute:
                        raise ValueError("workspace exclusion contains an unsafe path")
                    literal = "".join("\\" + char if char in "*?[]\\" else char for char in absolute)
                    values.append(literal)
            directories[:] = [name for name in directories
                              if not self.is_excluded((parent / name).relative_to(root).as_posix())]
        return tuple(sorted(values))

    def restic_excludes(self) -> tuple[str, ...]:
        """Return deterministic restic glob exclusions equivalent to this policy."""
        values = [
            rendered
            for pattern in self.patterns
            for rendered in _restic_patterns(pattern)
        ]
        if self.active_runtime_relative:
            values.extend(
                (self.active_runtime_relative, f"{self.active_runtime_relative}/**")
            )
        return tuple(dict.fromkeys(values))


def _restic_pattern(pattern: tuple[str, ...]) -> str:
    rendered = "/".join(pattern)
    if len(pattern) == 1 and pattern[0] != "**":
        return f"**/{rendered}"
    return rendered


def _restic_patterns(pattern: tuple[str, ...]) -> tuple[str, ...]:
    rendered = _restic_pattern(pattern)
    values = [rendered]
    if pattern[-1] == "**":
        base = "/".join(pattern[:-1])
        if base:
            values.append(base)
    else:
        values.append(f"{rendered}/**")
    return tuple(values)


def load_capture_policy(
    workspace_root: Path,
    *,
    active_runtime_root: Path | None = None,
) -> CapturePolicy:
    """Load shared defaults and exclusion-only workspace rules."""
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ValueError("workspace must be a directory")
    defaults_raw, defaults = _read_defaults()
    ignore_raw = _read_ignore(root)
    patterns = tuple(
        _parts(item, rule="workspace capture default") for item in defaults["exclude"]
    )
    patterns += _user_patterns(ignore_raw)

    runtime_relative = None
    if active_runtime_root is not None:
        runtime = Path(active_runtime_root).resolve(strict=True)
        if not runtime.is_dir():
            raise ValueError("active runtime root must be a directory")
        try:
            relative = runtime.relative_to(root)
        except ValueError:
            pass
        else:
            if not relative.parts:
                raise ValueError("active runtime root cannot be the workspace root")
            if any(char in relative.as_posix() for char in "*?[]"):
                raise ValueError(
                    "active runtime path contains unsupported glob characters"
                )
            runtime_relative = relative.as_posix()

    digest = hashlib.sha256()
    digest.update(defaults_raw)
    digest.update(
        b"\0ignore-present\0" if ignore_raw is not None else b"\0ignore-absent\0"
    )
    if ignore_raw is not None:
        digest.update(ignore_raw)
    digest.update(b"\0active-runtime\0")
    digest.update((runtime_relative or "").encode("utf-8"))
    return CapturePolicy(digest.hexdigest(), patterns, runtime_relative)
