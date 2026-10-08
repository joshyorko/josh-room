"""Synthetic Git-fidelity vertical for the installed managed controller.

Uses only an owned temporary workspace and an encrypted local Restic store.
The caller supplies the managed JAT runtime for real portable export/restore.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from josh_room import jat
from josh_room.private_paths import (
    protect_private_directory,
    secure_private_file,
    validate_private_directory,
)
from josh_room.room_store_export import export_portable_jat
from josh_room.room_store_operations import RoomStoreOperations


def git(root: Path, *args: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
                       GIT_AUTHOR_NAME="Synthetic Developer", GIT_AUTHOR_EMAIL="dev@example.invalid",
                       GIT_COMMITTER_NAME="Synthetic Developer", GIT_COMMITTER_EMAIL="dev@example.invalid")
    result = subprocess.run(["git", "-C", str(root), *args], env=environment,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("synthetic Git fixture command failed: " + args[0] + ": " + jat._diagnostic(result.stderr))
    return result.stdout


def repository(root: Path) -> None:
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    if os.name == "nt":
        # RCC's private Windows temp root is long; configure only this owned
        # fixture and verify the setting survives with the rest of its config.
        git(root, "config", "core.longpaths", "true")
    for name in ("staged.txt", "unstaged.txt", "history.txt"):
        (root / name).write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "synthetic initial")
    git(root, "branch", "local-only")
    git(root, "branch", "venv")
    git(root, "remote", "add", "origin", "https://example.invalid/synthetic/project.git")
    (root / "history.txt").write_text("local-only history\n")
    git(root, "commit", "-am", "unpublished local commit")
    (root / "stashed.txt").write_text("unique stashed content\n")
    git(root, "stash", "push", "--include-untracked", "-m", "synthetic stash")
    git(root, "pack-refs", "--all")
    git(root, "gc")
    (root / "staged.txt").write_text("staged\n")
    git(root, "add", "staged.txt")
    (root / "unstaged.txt").write_text("unstaged\n")
    (root / "untracked.txt").write_text("untracked\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)


def identity(root: Path) -> dict:
    return {command[0] + " ".join(command[1:]): git(root, *command) for command in (
        ("status", "--porcelain=v1", "--untracked-files=all"),
        ("rev-parse", "HEAD"), ("show-ref",), ("branch", "--list"),
        ("remote", "-v"), ("ls-files", "--stage"), ("stash", "list"),
        ("log", "--all", "--format=%H %s"), ("fsck", "--full"),
    )}


def contents(root: Path) -> dict:
    return {path.relative_to(root).as_posix(): (stat.S_IMODE(path.stat().st_mode),
            hashlib.sha256(path.read_bytes()).hexdigest())
            for path in root.rglob("*") if path.is_file()}


def operations(root: Path, workspace: Path):
    state = {"latest": None, "signature": None}
    secret = SimpleNamespace(secret=base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("="),
                             generation=1, repository_id=None)
    keyset = SimpleNamespace(room_store=secret)
    for name in ("private", "cache"):
        (root / name).mkdir(mode=0o700)
        protect_private_directory(root / name)

    def bind(_dimension, _backend, repository_id, **_kwargs):
        secret.repository_id = repository_id
        return keyset

    def publish(descriptor, *, workspace_signature, signature_algorithm, **_kwargs):
        state.update(latest=descriptor, signature=(workspace_signature, signature_algorithm,
                     descriptor.to_dict()["capture_policy_sha256"]))

    result = RoomStoreOperations(
        workspace=workspace, repository=root / "encrypted-restic", cache_dir=root / "cache",
        password_dir=root / "private", dimension=object(), backend=object(),
        ensure_keyset=lambda *_args: keyset, bind_repository=bind,
        read_latest=lambda: (state["latest"], None), read_catalog_signature=lambda: state["signature"],
        publish_descriptor=publish, write_marker=lambda *_args, **_kwargs: None,
        descriptor_metadata={"dimension_id": "synthetic", "encryption_domain_id": "synthetic",
            "room_id": "synthetic", "components": dict.fromkeys(("rcc_environment", "homebrew_recovery", "hauler_content")),
            "source": {}, "producer": {"josh_room_version": "0.1.31", "restic_version": "0.19.1",
            "source_platform": "linux-x64" if os.name != "nt" else "win32-x64",
            "restore_platforms": ["linux-x64", "win32-x64"]}},
        secure_private_file=secure_private_file, validate_private_directory=validate_private_directory,
    )
    return result


def exercise(root: Path, jat_root: Path | None = None) -> dict:
    workspace = root / "workspace"
    repos = [Path("services/one"), Path("services/deep/two"), Path("library")]
    for relative in repos:
        repository(workspace / relative)
    parent = workspace / repos[0]
    git(parent, "-c", "core.longpaths=true", "-c", "protocol.file.allow=always", "submodule", "add", str(workspace / "library"), "modules/library")
    if os.name == "nt":
        git(parent / "modules/library", "-c", "core.longpaths=true", "config", "core.longpaths", "true")
    git(parent, "config", "-f", ".gitmodules", "submodule.modules/library.url", "https://example.invalid/synthetic/library.git")
    git(parent / "modules/library", "remote", "set-url", "origin", "https://example.invalid/synthetic/library.git")
    repos.append(repos[0] / "modules/library")
    # A generated name in Git storage must survive, while real caches do not.
    git(parent, "branch", "node_modules")
    (parent / "node_modules").mkdir()
    (parent / "node_modules" / "generated.js").write_text("generated\n")
    (parent / ".gitignore").write_text("node_modules/\n")
    (workspace / ".josh-roomignore").write_text("**/.git\n**/.git/objects\n")
    expected = {str(relative): identity(workspace / relative) for relative in repos}
    before = contents(workspace)
    op = operations(root, workspace)
    first = op.save()
    started = time.monotonic()
    noop = op.save()
    noop_seconds = time.monotonic() - started
    assert first.status == "saved" and noop.status == "already-saved"
    assert noop.data_added_bytes == 0
    assert contents(workspace) == before
    restored = root / "entered"
    op.restore(first.descriptor, restored, write_restore_marker=lambda *_args: None)
    restored_files = contents(restored)
    assert restored_files == {name: value for name, value in before.items()
                              if name != "services/one/node_modules/generated.js"}
    for relative in repos:
        assert identity(restored / relative) == expected[str(relative)]
        if (workspace / relative / ".git").is_dir():
            assert contents(restored / relative / ".git") == contents(workspace / relative / ".git")
        else:
            assert (restored / relative / ".git").read_bytes() == (workspace / relative / ".git").read_bytes()
    assert not (restored / repos[0] / "node_modules").exists()
    assert contents(workspace) == before
    # Only Git metadata changes: never allow the already-saved shortcut.
    git(parent, "branch", "after-save")
    second = op.save()
    assert second.status == "saved"
    expected_parent = None if os.name == "nt" else first.snapshot_id
    assert second.descriptor.to_dict()["workspace"].get("parent_snapshot_id") == expected_parent
    assert second.descriptor.to_dict()["parent_logical_jat_id"] == first.descriptor.to_dict()["logical_jat_id"]
    assert second.data_added_bytes < first.data_added_bytes
    assert op.save().status == "already-saved"
    portable = "not-run"
    if jat_root is not None:
        def build(*args, **kwargs):
            try:
                return jat.run_build(*args, **kwargs)
            except jat.JATError as error:
                # This harness handles only synthetic fixtures; retain the
                # bounded sanitized nested diagnostic instead of losing it at
                # the generic export boundary.
                raise RuntimeError("synthetic managed JAT Build failed: " + jat._diagnostic(json.dumps(error.result))) from None

        _keyset, password_file, store = op._open_store()
        try:
            with store as restic:
                restic.open_existing()
                output = root / "portable.haul.tar.zst"
                export_portable_jat(descriptor=first.descriptor, restic=restic,
                    staging_parent=root / "private", output=output, jat_root=jat_root, run_build_fn=build)
            destination = root / "portable-restored"
            receipt = jat.run_restore(jat_root, output, destination)
            assert receipt["success"] is True
            clean = destination / "workspace" / "workspace"
            assert contents(clean) == restored_files
            for relative in repos:
                assert identity(clean / relative) == expected[str(relative)]
                if (workspace / relative / ".git").is_dir():
                    assert contents(clean / relative / ".git") == contents(restored / relative / ".git")
                else:
                    assert (clean / relative / ".git").read_bytes() == (restored / relative / ".git").read_bytes()
            portable = "passed"
        finally:
            password_file.unlink(missing_ok=True)
    return {"git_repositories": len(repos), "save_enter": "passed", "git_metadata_incremental": "passed",
            "noop": "already-saved", "noop_added_bytes": 0, "noop_seconds": noop_seconds,
            "git_metadata_added_bytes": second.data_added_bytes, "initial_added_bytes": first.data_added_bytes,
            "parent_reuse": "passed" if os.name != "nt" else "existing-windows-forced-content-scan",
            "source_unchanged": "passed", "portable_jat": portable}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jat-root", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="josh-room-git-acceptance-") as name:
        result = exercise(Path(name), args.jat_root)
    args.result_file.write_text(json.dumps(result, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
