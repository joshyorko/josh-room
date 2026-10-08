"""Read-only checks for self-contained, relocatable Git storage.

Never invoke Git, hooks, filters, or repository-supplied commands here. Git
configuration and pointers remain unchanged, including during staged restore.
"""

from __future__ import annotations

import configparser
import os
import re
import stat
from pathlib import Path


class GitWorkspaceError(ValueError):
    """Actionable diagnostics which never disclose repository data or paths."""


_POINTER_ERROR = (
    "Git storage is not self-contained and portable; use an ordinary checkout "
    "or relative in-workspace submodule storage before saving (external or "
    "absolute Git pointers and linked worktrees are unsupported)"
)


def _check_export_credentials(text: str) -> None:
    # Inspect raw occurrences as well as parsed keys: Git permits repeated
    # values and URL subsections, which ConfigParser intentionally collapses.
    joined = re.sub(r"\\\r?\n\s*", "", text)
    if (re.search(r"(?i)[a-z][a-z0-9+.-]*://[^/\s]*@|[?&](?:token|key|password)=", joined)
            or re.search(r"(?im)^\s*\[(?:credential|http)(?:\s|\])", joined)
            or re.search(r"(?im)^\s*[^=\n]*(?:password|token|secret|extraheader)[^=\n]*=", joined)):
        raise GitWorkspaceError("Portable JAT is plaintext and Git configuration may contain credentials; export was refused without stripping metadata")


def _text(path: Path, limit: int = 1024 * 1024) -> str:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise ValueError
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError
        return data.decode("utf-8")
    except (OSError, UnicodeError, ValueError):
        raise GitWorkspaceError("Git metadata cannot be read safely; Save or export was refused") from None


def _pointer(root: Path, base: Path, value: str, paths: set[str] | frozenset[str]) -> Path:
    if (not value or value.startswith(("/", "\\", "~"))
            or "\\" in value or re.match(r"^[A-Za-z]:", value)
            or any(char in value for char in "\x00\r\n")):
        raise GitWorkspaceError(_POINTER_ERROR)
    try:
        target = (base / value).resolve(strict=True)
        relative = target.relative_to(root).as_posix()
        if relative != "." and relative not in paths:
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise GitWorkspaceError(_POINTER_ERROR) from None
    return target


def validate_git_storage(root: Path, paths: set[str] | frozenset[str], *, portable_export: bool = False) -> None:
    """Reject dangling/external Git dependencies and excluded metadata.

    Ordinary repositories and relative submodule gitfiles are copied verbatim.
    Linked worktrees normally contain absolute administrative back-pointers and
    are refused, rather than rewriting user history or producing broken trees.
    """
    root = root.resolve(strict=True)
    stores: set[Path] = set()
    for relative in sorted(paths):
        if portable_export and relative.split("/")[-1] == ".gitmodules":
            _check_export_credentials(_text(root / relative))
        if relative.split("/")[-1] == "HEAD":
            candidate = (root / relative).parent
            if (candidate / "objects").is_dir() and (candidate / "config").is_file():
                stores.add(candidate)
        if relative.split("/")[-1] != ".git":
            continue
        entry = root / relative
        metadata = entry.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            stores.add(entry)
        elif stat.S_ISREG(metadata.st_mode):
            value = _text(entry, 64 * 1024).rstrip("\n")
            if not value.startswith("gitdir: "):
                raise GitWorkspaceError(_POINTER_ERROR)
            stores.add(_pointer(root, entry.parent, value[8:], paths))
        else:
            raise GitWorkspaceError(_POINTER_ERROR)

    checked: set[Path] = set()
    while stores - checked:
        store = next(iter(stores - checked))
        checked.add(store)
        if not store.is_dir() or not (store / "HEAD").is_file():
            raise GitWorkspaceError("Git storage is incomplete; Save or export was refused")
        if not (store / "commondir").exists() and (
            not (store / "objects").is_dir() or not (store / "refs").is_dir()
        ):
            raise GitWorkspaceError("Git storage is incomplete; Save or export was refused")
        # Includes every descendant, even when a user ignore rule would prune
        # storage reached through a .git file outside a .git directory.
        for directory, directories, files in os.walk(store, followlinks=False):
            for name in (*directories, *files):
                path = Path(directory) / name
                if path.is_symlink() or path.relative_to(root).as_posix() not in paths:
                    raise GitWorkspaceError("Git metadata is excluded or linked; include all Git storage before saving")
                if name in {"commondir", "gitdir"}:
                    target = _pointer(root, path.parent, _text(path, 64 * 1024).rstrip("\n"), paths)
                    if name == "commondir":
                        stores.add(target)
                if name in {"alternates", "http-alternates"} and path.parent.name == "info" and path.parent.parent.name == "objects":
                    # Quoted and network alternates have additional semantics;
                    # do not produce a capsule depending on borrowed objects.
                    raise GitWorkspaceError("Git object alternates are unsupported; make the object database self-contained before saving")
                if name not in {"config", "config.worktree"}:
                    continue
                # Only administrative configs, not arbitrary hooks named config.
                if path.parent != store and not (path.parent / "HEAD").is_file():
                    continue
                config = configparser.RawConfigParser(strict=False, allow_no_value=True)
                body = _text(path)
                if portable_export:
                    _check_export_credentials(body)
                try:
                    config.read_string(body)
                except configparser.Error:
                    raise GitWorkspaceError("Git configuration cannot be verified portably; Save or export was refused") from None
                for section in config.sections():
                    lowered = section.lower()
                    if lowered == "include" or lowered.startswith("includeif "):
                        raise GitWorkspaceError("Git config includes are unsupported; make configuration self-contained before saving")
                    for key, value in config.items(section):
                        if lowered == "core" and key == "worktree":
                            _pointer(root, path.parent, (value or "").strip('"'), paths)
                        if lowered == "core" and key in {"hookspath", "attributesfile", "excludesfile"}:
                            raise GitWorkspaceError("Custom Git hooks, attributes, or excludes paths are unsupported; use self-contained default Git storage before saving")
                        if portable_export and (
                            lowered.startswith(("credential", "http"))
                            or re.search(r"(?i)password|token|secret|extraheader", key)
                            or (value and re.search(r"(?i)[a-z][a-z0-9+.-]*://[^/\s]*@|[?&](?:token|key|password)=", value))
                        ):
                            raise GitWorkspaceError("Portable JAT is plaintext and Git configuration may contain credentials; export was refused without stripping metadata")
