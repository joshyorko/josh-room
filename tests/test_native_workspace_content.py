"""Stable native workspace content authority without pathname-following reads."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import native_workspace_content as content
from josh_room.room_store_export import _semantic_workspace
from josh_room.workspace_policy import load_capture_policy

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux descriptor-bound content contract")


def policy(root):
    runtime = Path(os.environ["ROBOCORP_HOME"]) if os.environ.get("ROBOCORP_HOME") else None
    return load_capture_policy(root, active_runtime_root=runtime if runtime and runtime.is_dir() else None)


def digest(root):
    return content.fingerprint(root, expected_policy_sha256=policy(root).sha256)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBOCORP_HOME", raising=False)
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    (root / "src").mkdir()
    (root / "src/main.py").write_text("synthetic original\n")
    (root / "src/main.py").chmod(0o640)
    return root


def test_matches_existing_semantic_tuple_contract(workspace):
    (workspace / "alias").symlink_to("src/main.py")
    capture_policy = policy(workspace)
    expected = {"format": content.FORMAT, "capture_policy_sha256": capture_policy.sha256,
                "capture_mode": capture_policy.capture_mode,
                "entries": _semantic_workspace(workspace, capture_policy)}
    assert digest(workspace) == hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_cold_copy_matches_despite_new_inodes_and_times(workspace, tmp_path):
    before = digest(workspace)
    copied = tmp_path / "hydrated"
    shutil.copytree(workspace, copied, copy_function=shutil.copy)
    source = workspace / "src/main.py"
    target = copied / "src/main.py"
    assert source.stat().st_ino != target.stat().st_ino
    os.utime(target, ns=(1_000_000_000, 2_000_000_000))
    assert digest(copied) == before


@pytest.mark.parametrize("change", ["content", "path", "file-mode", "directory-mode", "link-target"])
def test_semantic_changes_change_digest(workspace, change):
    (workspace / "alias").symlink_to("src/main.py")
    before = digest(workspace)
    target = workspace / "src/main.py"
    if change == "content":
        target.write_text("synthetic changed\n")
    elif change == "path":
        target.rename(workspace / "src/other.py")
        (workspace / "alias").unlink()
        (workspace / "alias").symlink_to("src/other.py")
    elif change == "file-mode":
        target.chmod(0o600)
    elif change == "directory-mode":
        (workspace / "src").chmod(0o700)
    else:
        (workspace / "alias").unlink()
        (workspace / "alias").symlink_to("src")
    assert digest(workspace) != before


def test_defaults_and_generated_marker_are_excluded(workspace):
    before = digest(workspace)
    (workspace / ".josh-room.json").write_text("synthetic generated marker")
    (workspace / ".venv").mkdir()
    (workspace / ".venv/private.txt").write_text("synthetic excluded runtime")
    assert digest(workspace) == before


@pytest.mark.parametrize("inside", [False, True])
def test_active_runtime_policy_matches_canonical_loader_without_reading_contents(workspace, tmp_path, monkeypatch, inside):
    runtime = (workspace if inside else tmp_path) / "rcc-runtime"
    runtime.mkdir()
    (runtime / "generated").write_text("synthetic excluded runtime")
    monkeypatch.setenv("ROBOCORP_HOME", str(runtime))
    original_read = content.os.read
    read_paths = []

    def tracked_read(descriptor, maximum):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        read_paths.append(path)
        assert not path.is_relative_to(runtime)
        return original_read(descriptor, maximum)

    monkeypatch.setattr(content.os, "read", tracked_read)
    before = digest(workspace)
    (runtime / "generated").write_text("different excluded runtime")
    assert digest(workspace) == before
    assert workspace / "src/main.py" in read_paths


def test_source_only_policy_and_git_bytes_follow_canonical_semantics(workspace):
    (workspace / ".git").mkdir()
    (workspace / ".git/HEAD").write_text("synthetic metadata")
    full = digest(workspace)
    (workspace / ".git/HEAD").write_text("synthetic changed metadata")
    assert digest(workspace) != full
    (workspace / ".josh-roomignore").write_text("# josh-room capture-mode: source-only\n")
    source_only = digest(workspace)
    (workspace / ".git/HEAD").write_text("ignored metadata")
    assert digest(workspace) == source_only


def test_wrong_policy_is_refused(workspace):
    with pytest.raises(content.NativeWorkspaceContentError, match="policy-mismatch"):
        content.fingerprint(workspace, expected_policy_sha256="0" * 64)


@pytest.mark.parametrize("workspace", [None, "relative-private-path"])
def test_invalid_workspace_input_has_bounded_error(workspace):
    with pytest.raises(content.NativeWorkspaceContentError) as caught:
        content.fingerprint(workspace, expected_policy_sha256="0" * 64)
    assert "relative-private-path" not in str(caught.value)


@pytest.mark.parametrize("kind", ["absolute", "external-relative", "dangling", "cycle", "fifo"])
def test_unsafe_entries_fail_closed_without_private_values(workspace, tmp_path, kind):
    target = workspace / "unsafe"
    if kind == "absolute":
        target.symlink_to(tmp_path / "outside-private-value")
    elif kind == "external-relative":
        target.symlink_to("../outside-private-value")
    elif kind == "dangling":
        target.symlink_to("missing-private-value")
    elif kind == "cycle":
        target.symlink_to("unsafe")
    else:
        os.mkfifo(target)
    with pytest.raises(content.NativeWorkspaceContentError) as caught:
        digest(workspace)
    assert "private-value" not in str(caught.value) and str(workspace) not in str(caught.value)


@pytest.mark.parametrize("entry", ["parent", "file"])
def test_transient_symlink_swap_never_reads_outside(workspace, tmp_path, monkeypatch, entry):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.py").write_text("synthetic outside must never be read")
    (outside / ".josh-roomignore").write_text("synthetic outside ignore must never be read")
    original_open = content.os.open
    original_read = content.os.read
    swapped = False

    def swapped_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and kwargs.get("dir_fd") is not None and str(path) == ("src" if entry == "parent" else "main.py"):
            swapped = True
            victim = workspace / "src" if entry == "parent" else workspace / "src/main.py"
            saved = victim.with_name(victim.name + "-old")
            victim.rename(saved)
            victim.symlink_to(outside if entry == "parent" else outside / "main.py")
            try:
                return original_open(path, flags, *args, **kwargs)
            finally:
                victim.unlink()
                saved.rename(victim)
        return original_open(path, flags, *args, **kwargs)

    def safe_read(descriptor, maximum):
        assert not Path(os.readlink(f"/proc/self/fd/{descriptor}")).is_relative_to(outside)
        return original_read(descriptor, maximum)

    monkeypatch.setattr(content.os, "open", swapped_open)
    monkeypatch.setattr(content.os, "read", safe_read)
    with pytest.raises(content.NativeWorkspaceContentError):
        digest(workspace)
    assert swapped


def test_root_ancestor_symlink_is_never_followed(workspace, tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.symlink_to(workspace.parent, target_is_directory=True)
    original_read = content.os.read

    def no_read(descriptor, maximum):
        pytest.fail("no workspace bytes may be read through a root ancestor alias")
        return original_read(descriptor, maximum)

    monkeypatch.setattr(content.os, "read", no_read)
    with pytest.raises(content.NativeWorkspaceContentError):
        content.fingerprint(alias / "workspace", expected_policy_sha256=policy(workspace).sha256)


@pytest.mark.parametrize("change", ["file", "structure", "runtime-attachment", "root-attachment"])
def test_changes_during_hash_are_refused(workspace, tmp_path, monkeypatch, change):
    runtime = workspace / "rcc-runtime"
    if change == "runtime-attachment":
        runtime.mkdir()
        monkeypatch.setenv("ROBOCORP_HOME", str(runtime))
    original_read = content.os.read
    changed = False

    def changed_read(descriptor, maximum):
        nonlocal changed
        data = original_read(descriptor, maximum)
        if not changed and Path(os.readlink(f"/proc/self/fd/{descriptor}")) == workspace / "src/main.py":
            changed = True
            if change == "file":
                (workspace / "src/main.py").write_text("same-sized change\n")
            elif change == "structure":
                (workspace / "new.txt").write_text("new entry")
            elif change == "runtime-attachment":
                runtime.rename(tmp_path / "old-runtime")
                runtime.mkdir()
            else:
                workspace.rename(tmp_path / "old-root")
                workspace.mkdir()
        return data

    monkeypatch.setattr(content.os, "read", changed_read)
    with pytest.raises(content.NativeWorkspaceContentError):
        digest(workspace)
    assert changed


def test_policy_mutation_and_entry_limit_fail_closed(workspace, monkeypatch):
    before = policy(workspace).sha256
    (workspace / ".josh-roomignore").write_text("cache\n")
    with pytest.raises(content.NativeWorkspaceContentError, match="policy-mismatch"):
        content.fingerprint(workspace, expected_policy_sha256=before)
    monkeypatch.setattr(content, "MAX_ENTRIES", 1)
    with pytest.raises(content.NativeWorkspaceContentError, match="limit"):
        digest(workspace)


def test_compiled_policy_and_descriptor_reread_must_match(workspace, monkeypatch):
    (workspace / ".josh-roomignore").write_text("cache\n")
    expected = policy(workspace).sha256
    original = content.workspace_policy._read_ignore
    count = 0

    def changed_ignore(*args, **kwargs):
        nonlocal count
        count += 1
        data = original(*args, **kwargs)
        return b"other\n" if count == 2 else data

    monkeypatch.setattr(content.workspace_policy, "_read_ignore", changed_ignore)
    with pytest.raises(content.NativeWorkspaceContentError, match="changed"):
        content.fingerprint(workspace, expected_policy_sha256=expected)


def test_second_pass_detects_change_to_an_already_hashed_file(workspace, monkeypatch):
    (workspace / "z.txt").write_text("later file")
    original_read = content.os.read
    changed = False

    def late_change(descriptor, maximum):
        nonlocal changed
        data = original_read(descriptor, maximum)
        if not changed and Path(os.readlink(f"/proc/self/fd/{descriptor}")) == workspace / "z.txt":
            changed = True
            (workspace / "src/main.py").write_text("later mutation")
        return data

    monkeypatch.setattr(content.os, "read", late_change)
    with pytest.raises(content.NativeWorkspaceContentError, match="changed"):
        digest(workspace)
    assert changed


def test_filesystem_boundary_is_refused_before_open(workspace, monkeypatch):
    original_stat = content.os.stat
    original_open = content.os.open

    def other_device(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if str(path) == "main.py" and kwargs.get("dir_fd") is not None:
            return SimpleNamespace(st_dev=info.st_dev + 1)
        return info

    def no_file_open(path, flags, *args, **kwargs):
        assert str(path) != "main.py"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(content.os, "stat", other_device)
    monkeypatch.setattr(content.os, "open", no_file_open)
    with pytest.raises(content.NativeWorkspaceContentError, match="filesystem-boundary"):
        digest(workspace)


def test_read_failure_closes_all_opened_descriptors(workspace, monkeypatch):
    original_open = content.os.open
    descriptors = []

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def failed_read(*_args):
        raise OSError("synthetic private path must not leak")

    monkeypatch.setattr(content.os, "open", tracked_open)
    monkeypatch.setattr(content.os, "read", failed_read)
    with pytest.raises(content.NativeWorkspaceContentError) as caught:
        digest(workspace)
    assert str(caught.value) == "native-workspace-content-unavailable"
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_nonregular_swap_cannot_block_file_open(workspace, monkeypatch):
    original_open = content.os.open
    swapped = False

    def swapped_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path) == "main.py" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            target = workspace / "src/main.py"
            target.unlink()
            os.mkfifo(target)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(content.os, "open", swapped_open)
    with pytest.raises(content.NativeWorkspaceContentError):
        digest(workspace)
    assert swapped
