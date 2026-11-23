"""Offline integrity checks for independently restorable Catalog Git children."""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .git_workspace import _text


def object_format(store):
    """Only admit standalone, complete object stores using supported formats."""
    if (store / "shallow").exists() or any((store / "objects/pack").glob("*.promisor")):
        raise ValueError("Catalog capture requires complete Git history; partial and shallow clones are unsupported")
    if (store / "info/grafts").exists():
        raise ValueError("Catalog capture requires complete Git history without grafts")
    config = configparser.RawConfigParser(strict=False, allow_no_value=True)
    if (store / "config").exists():
        config.read_string(_text(store / "config"))
    result = "sha1"
    for section in config.sections():
        name = section.lower()
        for key, value in config.items(section):
            if (name.startswith('remote ') and key in {"promisor", "partialclonefilter"}) or (name == "extensions" and key == "partialclone"):
                raise ValueError("Catalog capture requires complete Git history; partial clones are unsupported")
            if name == "extensions":
                if key == "objectformat" and value in {"sha1", "sha256"}:
                    result = value
                else:
                    raise ValueError("Catalog capture cannot verify this Git repository extension")
    return result


def verify_staged_git(root, paths, mode, private):
    """Check a private staged inventory without opening its repository config in Git."""
    if mode != "full-repository":
        return
    for path in paths:
        if Path(path).name != ".git":
            continue
        store = Path(root).resolve(strict=True) / path
        format_name = object_format(store)
        git = shutil.which("git")
        if not git:
            raise ValueError("Git is required to verify full-repository Catalog children")
        with tempfile.TemporaryDirectory(prefix="git-proof-", dir=private) as directory:
            # Git reads only the validated object, index, and reference files.
            # In particular, a source repository cannot configure fsck severity,
            # helpers, remotes, hooks, global includes or lazy object fetching.
            proof = Path(directory)
            shutil.copyfile(store / "HEAD", proof / "HEAD")
            for name in ("refs", "logs"):
                source = store / name
                if source.exists():
                    shutil.copytree(source, proof / name)
            if (store / "packed-refs").exists():
                shutil.copyfile(store / "packed-refs", proof / "packed-refs")
            for name in ("objects", "index"):
                source = store / name
                if source.exists():
                    (proof / name).symlink_to(source.absolute())
            for index in store.glob("sharedindex.*"):
                (proof / index.name).symlink_to(index.absolute())
            if format_name == "sha256":
                (proof / "config").write_text("[core]\nrepositoryformatversion = 1\n[extensions]\nobjectformat = sha256\n")
            env = {"PATH": os.defpath, "HOME": directory, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "",
                   "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
            check = subprocess.run([git, "--git-dir=.", "-c", "core.hooksPath=/dev/null",
                                    "fsck", "--full", "--no-dangling"], cwd=proof, env=env,
                                   check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode:
                raise ValueError("Git integrity verification failed; repair missing or invalid objects before saving or restoring")
