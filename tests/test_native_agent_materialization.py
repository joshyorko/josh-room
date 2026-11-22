"""Real Linux anonymous-inode publication and interruption behavior."""

import errno
import hashlib
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room import native_agent_materialization as materialization
from josh_room.cancellation import CLICancelled

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux anonymous inode contract only")
PAYLOAD = b'{"type":"synthetic-native-session","message":"synthetic history"}\n'


def test_materializes_one_complete_file_without_visible_plaintext_stage(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / "session.jsonl"
    seen = []

    def admission():
        seen.append(set(tmp_path.iterdir()))

    receipt = materialization.anonymous_file(target, PAYLOAD, before_commit=admission)
    assert seen == [set()]
    assert set(tmp_path.iterdir()) == {target}
    assert target.read_bytes() == PAYLOAD
    assert target.stat().st_mode & 0o777 == 0o600
    assert receipt == {"ok": True, "state": "history-materialized", "bytes": len(PAYLOAD), "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "native_resume_verified": False}
    assert str(tmp_path) not in str(receipt)


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "hardlink", "fifo"])
def test_existing_targets_never_overwritten(tmp_path, kind):
    tmp_path.chmod(0o700)
    target = tmp_path / "existing"
    original = tmp_path / "original"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        original.write_bytes(b"existing synthetic")
        os.link(original, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"existing synthetic")
    before = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(materialization.MaterializationError, match="collision"):
        materialization.anonymous_file(target, PAYLOAD, before_commit=lambda: None)
    assert {path.name for path in tmp_path.iterdir()} == before
    if kind in {"file", "hardlink"}:
        assert target.read_bytes() == b"existing synthetic"


def test_unsupported_filesystem_has_no_named_fallback(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    actual = os.open

    def unsupported(path, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, "synthetic unsupported filesystem")
        return actual(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", unsupported)
    with pytest.raises(materialization.MaterializationError, match="unavailable"):
        materialization.anonymous_file(tmp_path / "session.jsonl", PAYLOAD, before_commit=lambda: None)
    assert not list(tmp_path.iterdir())


def test_callback_denial_leaves_no_plaintext_file(tmp_path):
    tmp_path.chmod(0o700)

    def denied():
        raise ValueError("synthetic private diagnostic")

    with pytest.raises(materialization.MaterializationError, match="^materialization-admission-denied$"):
        materialization.anonymous_file(tmp_path / "session.jsonl", PAYLOAD, before_commit=denied)
    assert not list(tmp_path.iterdir())


def test_callback_target_collision_preserves_concurrent_file(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / "session.jsonl"

    def occupied():
        target.write_bytes(b"concurrent synthetic state")

    with pytest.raises(materialization.MaterializationError, match="collision"):
        materialization.anonymous_file(target, PAYLOAD, before_commit=occupied)
    assert target.read_bytes() == b"concurrent synthetic state"


def test_replaced_parent_is_refused_before_link(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    moved = tmp_path / "moved"

    def replaced():
        parent.rename(moved)
        parent.mkdir(mode=0o700)

    with pytest.raises(materialization.MaterializationError, match="parent-changed"):
        materialization.anonymous_file(parent / "session.jsonl", PAYLOAD, before_commit=replaced)
    assert not list(parent.iterdir()) and not list(moved.iterdir())


def test_parent_symlink_and_broad_permissions_are_refused(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(materialization.MaterializationError):
        materialization.anonymous_file(link / "session.jsonl", PAYLOAD, before_commit=lambda: None)
    parent.chmod(0o755)
    with pytest.raises(materialization.MaterializationError, match="unsafe-parent"):
        materialization.anonymous_file(parent / "session.jsonl", PAYLOAD, before_commit=lambda: None)
    assert not list(parent.iterdir())


@pytest.mark.parametrize("phase", ["before", "after"])
def test_sigkill_before_or_after_publication_has_only_absent_or_complete_file(tmp_path, phase):
    tmp_path.chmod(0o700)
    target = tmp_path / "session.jsonl"
    script = """import signal,sys
from pathlib import Path
from josh_room.native_agent_materialization import anonymous_file
payload=b'{"type":"synthetic-native-session","message":"synthetic history"}\\n'
def ready():
    print('ready',flush=True)
    signal.pause()
callback=ready if sys.argv[2]=='before' else lambda:None
anonymous_file(Path(sys.argv[1]),payload,before_commit=callback)
if sys.argv[2]=='after': ready()
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen([sys.executable, "-c", script, str(target), phase], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        assert select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline().strip() == b"ready"
        if phase == "before":
            assert not list(tmp_path.iterdir())
        child.kill()
        child.wait(timeout=5)
        if phase == "before":
            assert not list(tmp_path.iterdir())
            materialization.anonymous_file(target, PAYLOAD, before_commit=lambda: None)
        assert target.read_bytes() == PAYLOAD
        assert list(tmp_path.iterdir()) == [target]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        child.stderr.close()


def test_short_writes_are_completed_before_publication(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    actual = os.write

    def partial(descriptor, data):
        return actual(descriptor, data[:3])

    monkeypatch.setattr(os, "write", partial)
    target = tmp_path / "session.jsonl"
    materialization.anonymous_file(target, PAYLOAD, before_commit=lambda: None)
    assert target.read_bytes() == PAYLOAD


def test_parent_fsync_failure_preserves_complete_final_file_as_uncertain(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    actual = os.fsync
    calls = []

    def fail_after_link(descriptor):
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError(errno.EIO, "synthetic directory fsync failure")
        return actual(descriptor)

    monkeypatch.setattr(os, "fsync", fail_after_link)
    target = tmp_path / "session.jsonl"
    with pytest.raises(materialization.MaterializationError, match="commit-uncertain") as caught:
        materialization.anonymous_file(target, PAYLOAD, before_commit=lambda: None)
    assert caught.value.committed is True
    assert target.read_bytes() == PAYLOAD
    assert set(tmp_path.iterdir()) == {target}


def test_callback_cancellation_reclaims_anonymous_inode(tmp_path):
    tmp_path.chmod(0o700)

    def cancelled():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        materialization.anonymous_file(tmp_path / "session.jsonl", PAYLOAD, before_commit=cancelled)
    assert not list(tmp_path.iterdir())


def test_parent_move_during_commit_never_reports_success(tmp_path, monkeypatch):
    from josh_room import native_agent_materialization as materialization

    parent = tmp_path / "destination"
    parent.mkdir(mode=0o700)
    moved = tmp_path / "moved"
    original = materialization._link_anonymous

    def move_before_link(source, directory, name):
        parent.rename(moved)
        parent.mkdir(mode=0o700)
        original(source, directory, name)

    monkeypatch.setattr(materialization, "_link_anonymous", move_before_link)
    with pytest.raises(materialization.MaterializationError, match="commit-uncertain") as failure:
        materialization.anonymous_file(parent / "session.jsonl", b"synthetic complete native file", before_commit=lambda: None)
    assert failure.value.committed is True
    assert not (parent / "session.jsonl").exists()
    assert (moved / "session.jsonl").read_bytes() == b"synthetic complete native file"


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, CLICancelled])
def test_cancel_immediately_after_link_reports_committed_uncertainty(tmp_path, monkeypatch, interruption):
    from josh_room import native_agent_materialization as materialization

    original = materialization._link_anonymous

    def linked_then_interrupted(*args):
        original(*args)
        raise interruption

    monkeypatch.setattr(materialization, "_link_anonymous", linked_then_interrupted)
    target = tmp_path / "session.jsonl"
    with pytest.raises(materialization.MaterializationError, match="commit-uncertain") as failure:
        materialization.anonymous_file(target, b"synthetic complete session", before_commit=lambda: None)
    assert failure.value.committed is True
    assert target.read_bytes() == b"synthetic complete session"
