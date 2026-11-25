"""Inspect only the held Git index, without loading repository configuration."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

from .workspace_onboarding import _directory


def validate_child_index(store, object_format, *, child_root):
    directory = _directory(store)
    try:
        try:
            index = os.open("index", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(index).st_mode):
            raise ValueError("Git index must be a regular file")
        with tempfile.TemporaryDirectory(prefix="josh-room-index-") as directory, ExitStack() as handles:
            proof = Path(directory)
            (proof / "HEAD").write_text("ref: refs/heads/main\n")
            (proof / "refs").mkdir()
            (proof / "objects").mkdir()
            if object_format == "sha256":
                (proof / "config").write_text(
                    "[core]\nrepositoryformatversion = 1\n[extensions]\nobjectformat = sha256\n")
            git = shutil.which("git")
            if not git:
                raise ValueError("Git is required to verify a Catalog child index")
            (proof / "index").symlink_to(f"/proc/self/fd/{index}")
            store_fd = _directory(store)
            handles.callback(os.close, store_fd)
            for name in os.listdir(store_fd):
                if not re.fullmatch(r"sharedindex\.(?:[a-f0-9]{40}|[a-f0-9]{64})", name):
                    continue
                shared = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=store_fd)
                handles.callback(os.close, shared)
                if not stat.S_ISREG(os.fstat(shared).st_mode):
                    raise ValueError("Git split-index base must be a regular file")
                with os.fdopen(os.dup(shared), "rb") as source, (proof / name).open("xb") as output:
                    shutil.copyfileobj(source, output)
            env = {"PATH": os.defpath, "GIT_INDEX_FILE": str(proof / "index"),
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_OPTIONAL_LOCKS": "0", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}
            # Plain ls-files quotes tabs/newlines in paths, so each row has a
            # single tab separator even for hostile or unusual tracked names.
            with subprocess.Popen([git, "--git-dir=.", "--work-tree=.", "ls-files", "--stage", "--sparse"],
                                  env=env, cwd=proof, pass_fds=(index,), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                gitlinks = submodules = False
                for entry in process.stdout:
                    header, _, path = entry.rstrip(b"\n").partition(b"\t")
                    gitlinks |= header.startswith(b"160000 ")
                    submodules |= path == b".gitmodules"
                    if child_root and path in {b".josh-roomignore", b".josh-room.json", b".josh-room-split.json"}:
                        process.kill()
                        raise ValueError("Catalog child tracks generated Room metadata; save the repository as one ordinary Room")
                if process.wait():
                    raise ValueError("Catalog child index could not be verified; use a standalone index or save one ordinary Room")
                if gitlinks and submodules:
                    raise ValueError("Catalog child submodules are unsupported; save the repository as one ordinary Room")
    finally:
        os.close(index)
