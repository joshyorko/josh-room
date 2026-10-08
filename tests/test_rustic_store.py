"""Focused Rustic adapter contracts; all authorities and paths are synthetic."""

from __future__ import annotations

import io
import json
import sys
import threading

import pytest

from josh_room.adapter_contract import CancellationToken
from josh_room.restic_store import (
    RepositoryInfo,
    ResticStoreError,
    ResticStoreErrorCode,
    SnapshotInfo,
)
from josh_room.rustic_store import (
    RusticStore,
    _json,
    _snapshot_record,
    _snapshot_records,
    parse_backup_output,
    parse_tree_node,
)

PARENT = "a" * 64
TREE = "b" * 64
CHANGED = "c" * 64


def document(**updates):
    value = {
        "id": CHANGED,
        "tree": TREE,
        "parent": PARENT,
        "summary": {
            "files_new": 0,
            "files_changed": 1,
            "files_unmodified": 4,
            "data_added": 100,
            "data_added_packed": 90,
            "total_bytes_processed": 200,
        },
    }
    value.update(updates)
    return json.dumps(value).encode()


def test_proven_document_summary_and_noop_identity():
    result = parse_backup_output(document(), parent=PARENT, parent_tree_id=TREE)
    assert result.snapshot_id == CHANGED and result.effective_parent_id == PARENT
    value = json.loads(document(id=PARENT))
    value["summary"].update(files_changed=0, data_added=0, data_added_packed=0)
    assert (
        parse_backup_output(
            json.dumps(value).encode(), parent=PARENT, parent_tree_id=TREE
        ).snapshot_id
        is None
    )
    with pytest.raises(ResticStoreError):
        parse_backup_output(
            json.dumps(value).encode(), parent=PARENT, parent_tree_id="d" * 64
        )
    with pytest.raises(ResticStoreError):
        parse_backup_output(document(parent="d" * 64), parent=PARENT)


@pytest.mark.parametrize(
    "raw",
    [
        b"[] trailing",
        b"\xff",
        b'{"id":1,"id":2}',
        b"NaN",
        b"[" * 1100,
        b"x" * (4 * 1024 * 1024 + 1),
    ],
)
def test_json_fails_closed(raw):
    with pytest.raises(ResticStoreError):
        _json(raw)


def test_grouped_snapshot_output_is_bounded_and_exact():
    record = {
        "id": PARENT,
        "tree": TREE,
        "time": "2026-10-06T00:00:00Z",
        "paths": ["."],
    }
    assert _snapshot_records([{"group_key": {}, "snapshots": [record]}]) == [record]
    assert _snapshot_record(record, PARENT).tree_id == TREE
    for bad in (
        {**record, "id": "short"},
        {**record, "time": "2026-10-06"},
        {**record, "paths": []},
        {**record, "parent": False},
    ):
        with pytest.raises(ResticStoreError):
            _snapshot_record(bad)
    with pytest.raises(ResticStoreError):
        _snapshot_records([{"group_key": {}, "snapshots": [record], "future": True}])


def test_native_tree_preserves_modes_special_entries_and_link_target():
    mode = 0o755 | (1 << 23)  # Restic/Go setuid representation, not lossy ls text.
    row, _ = parse_tree_node(
        {"name": "run", "type": "file", "mode": mode, "size": 20}, "dir"
    )
    assert (row.path, row.mode, row.size) == ("dir/run", mode, 20)
    row, _ = parse_tree_node(
        {
            "name": "link",
            "type": "symlink",
            "mode": 0o777,
            "linktarget": "space and\nnewline",
        },
        "",
    )
    assert row.link_target == "space and\nnewline"
    row, subtree = parse_tree_node({"name": "sub", "type": "dir", "subtree": TREE}, "")
    assert row.entry_type == "dir" and subtree == TREE
    assert (
        parse_tree_node({"name": "sock", "type": "socket"}, "")[0].entry_type
        == "socket"
    )


@pytest.mark.parametrize(
    "node",
    [
        {"name": "../escape", "type": "file"},
        {"name": "C:drive", "type": "file"},
        {"name": "a\\b", "type": "file"},
        {"name": "nul\x00", "type": "file"},
        {"name": "f", "type": "future"},
        {"name": "f", "type": "file", "size": True},
        {"name": "f", "type": "file", "mode": 2**32},
        {"name": "dir", "type": "dir", "subtree": "short"},
        {
            "name": "link",
            "type": "symlink",
            "linktarget": "a",
            "linktarget_raw": "Yg==",
        },
    ],
)
def test_native_tree_fails_closed(node):
    with pytest.raises(ResticStoreError):
        parse_tree_node(node, "")


@pytest.fixture
def authority(tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-contract-password")
    password.chmod(0o600)
    return {
        "repository": tmp_path / "repo",
        "cache_dir": tmp_path / "cache",
        "password_file": password,
    }


def test_environment_profile_and_secret_isolation(authority, monkeypatch):
    monkeypatch.setenv("RUSTIC_PASSWORD_COMMAND", "do-not-run")
    monkeypatch.setenv("RUSTIC_USE_PROFILE", "host-config")
    monkeypatch.setenv("AWS_PROFILE", "host-authority")
    monkeypatch.setenv("SSL_CERT_FILE", "host-tls-override")
    credentials = {
        "AWS_ACCESS_KEY_ID": "synthetic-key",
        "AWS_SECRET_ACCESS_KEY": 'synthetic-"\\\n-value',
    }
    authority["repository"] = "s3:https://example.invalid:9443/synthetic-bucket/prefix"
    with RusticStore(**authority, provider_env=credentials) as store:
        env = store._environment()
        profile = store._profile
        assert profile.is_absolute()
        text = profile.read_text()
        assert all(value not in text for value in credentials.values())
        assert (
            'disable_config_load = "true"' in text
            and 'disable_ec2_metadata = "true"' in text
        )
        assert 'endpoint = "https://example.invalid:9443"' in text
        assert env["RUSTIC_REPOSITORY"] == "opendal:s3"
        assert env["JOSH_ROOM_S3_SECRET_JSON"] == json.dumps(
            credentials["AWS_SECRET_ACCESS_KEY"]
        )
        assert not any(
            key in env
            for key in ("AWS_PROFILE", "RUSTIC_PASSWORD_COMMAND", "SSL_CERT_FILE")
        )
    assert not profile.exists()


@pytest.mark.parametrize(
    "repository",
    [
        "s3:http://example.invalid/bucket",
        "s3:https://user:password@example.invalid/bucket",
        "s3:https://example.invalid/bucket?secret=value",
    ],
)
def test_unsafe_s3_locators_rejected(authority, repository):
    authority["repository"] = repository
    with pytest.raises(ResticStoreError):
        RusticStore(**authority)


def test_windows_custom_ca_is_explicitly_unsupported(authority, tmp_path, monkeypatch):
    ca = tmp_path / "ca.pem"
    ca.write_text("synthetic")
    monkeypatch.setattr(sys, "platform", "win32")
    with (
        pytest.raises(ResticStoreError) as error,
        RusticStore(**authority, ca_bundle=ca),
    ):
        pytest.fail("silently ignored CA")
    assert error.value.code == ResticStoreErrorCode.INVALID_CONFIGURATION


class FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", code=0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = code

    def poll(self):
        return self.returncode

    def wait(self, **_kwargs):
        return self.returncode


def test_native_progress_windows_force_and_parent_semantics(
    authority, tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    summary = {
        "message_type": "summary",
        "snapshot_id": CHANGED,
        "files_new": 1,
        "files_changed": 0,
        "files_unmodified": 0,
        "data_added": 10,
        "data_added_packed": 9,
        "total_bytes_processed": 10,
    }
    calls = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess(
            json.dumps({"message_type": "status", "bytes_done": 4}).encode()
            + b"\n"
            + json.dumps(summary).encode()
            + b"\n"
        )

    with RusticStore(
        **authority, executable="synthetic-rustic", process_factory=spawn
    ) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        store._snapshot_info[PARENT] = SnapshotInfo(
            PARENT, TREE, None, "2026-10-06T00:00:00Z", (".",)
        )
        store._snapshot_info[CHANGED] = SnapshotInfo(
            CHANGED, TREE, None, "2026-10-06T00:00:00Z", (".",)
        )
        monkeypatch.setattr(sys, "platform", "win32")
        progress = []
        result = store.backup(workspace, parent=PARENT, on_progress=progress.append)
    assert result.force_scan and result.effective_parent_id is None
    assert progress[0].bytes_done == 4
    assert "--force" in calls[0][0] and "--parent" not in calls[0][0]
    assert calls[0][0][-2:] == ["--", "."]
    assert calls[0][1]["stdin"] is not None and calls[0][1]["cwd"] == workspace


def test_success_with_source_warning_cannot_publish(authority, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    summary = {
        "message_type": "summary",
        "snapshot_id": CHANGED,
        "files_new": 1,
        "files_changed": 0,
        "files_unmodified": 0,
        "data_added": 10,
        "data_added_packed": 9,
        "total_bytes_processed": 10,
    }

    def spawn(*_args, **_kwargs):
        return FakeProcess(
            json.dumps(summary).encode() + b"\n",
            b"[WARN] synthetic unreadable source\n",
        )

    with RusticStore(**authority, process_factory=spawn) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        with pytest.raises(ResticStoreError) as error:
            store.backup(workspace)
        assert error.value.code == ResticStoreErrorCode.BACKUP_ERRORS
        assert error.value.orphan_snapshot_id == CHANGED
        assert store.data_added_bytes == 0
        assert "unreadable" not in str(error.value)


def test_running_child_cancellation_and_timeout(authority, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # A silent actual process exercises pipe polling and owned process cleanup.
    script = tmp_path / "slow-child"
    script.write_text("#!/bin/sh\nexec sleep 30\n")
    script.chmod(0o700)
    token = CancellationToken()
    with RusticStore(**authority, executable=str(script), command_timeout=3) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        timer = threading.Timer(0.15, token.cancel)
        timer.start()
        try:
            with pytest.raises(ResticStoreError) as error:
                store.backup(workspace, cancellation=token)
            assert error.value.code == ResticStoreErrorCode.CANCELLED
            assert store._active_process is None
        finally:
            timer.cancel()
    with RusticStore(**authority, executable=str(script), command_timeout=0.1) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        with pytest.raises(ResticStoreError) as error:
            store.backup(workspace)
        assert error.value.code == ResticStoreErrorCode.TIMED_OUT


def test_authenticated_tree_cache_reuses_and_rejects_tampering(authority):
    import hashlib

    raw = b'{"nodes":[]}'
    tree_id = hashlib.sha256(raw).hexdigest()
    calls = []

    def spawn(argv, **_kwargs):
        calls.append(argv)
        return FakeProcess(raw + b"\n")

    with RusticStore(**authority, process_factory=spawn) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        assert store._tree_blob(tree_id) == raw
        assert store._tree_blob(tree_id) == raw
        assert len(calls) == 1
        cache = store._cache_dir / "rustic-trees-v1" / ("e" * 64) / tree_id
        cache.write_bytes(b'{"nodes":["forged"]}')
        assert store._tree_blob(tree_id) == raw
        assert len(calls) == 2


def test_unauthenticated_tree_blob_fails_closed(authority):
    def spawn(*_args, **_kwargs):
        return FakeProcess(b'{"nodes":[]}\n')

    with RusticStore(**authority, process_factory=spawn) as store:
        store._repository_info = RepositoryInfo("e" * 64, 2)
        with pytest.raises(ResticStoreError):
            store._tree_blob(TREE)


def test_missing_repository_may_initialize_but_auth_failure_never_does(authority):
    calls = []
    results = [
        FakeProcess(b"rustic v0.11.4\n"),
        FakeProcess(stderr=b"synthetic auth failure", code=1),
    ]

    def spawn(argv, **_kwargs):
        calls.append(argv)
        return results.pop(0)

    with (
        RusticStore(**authority, process_factory=spawn) as store,
        pytest.raises(ResticStoreError),
    ):
        store.initialize()
    assert not any("init" in argv for argv in calls)
