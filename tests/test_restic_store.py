import importlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path

import pytest


def api():
    spec = importlib.util.find_spec("josh_room.restic_store")
    assert spec is not None, "the ResticStore adapter module has not been implemented"
    return importlib.import_module("josh_room.restic_store")


def test_repository_config_rejects_bool_format_and_invalid_repository_id():
    module = api()
    repository_id = "a" * 64

    with pytest.raises(module.ResticStoreError):
        module.parse_repository_config(json.dumps({"id": repository_id, "version": True}))
    with pytest.raises(module.ResticStoreError):
        module.parse_repository_config(json.dumps({"id": "not-a-repository-id", "version": 2}))


def test_backup_stream_rejects_bool_counters_unknown_type_and_oversized_line():
    module = api()
    events = [
        json.dumps({"message_type": "summary", "snapshot_id": None, "errors": 0,
                    "files_new": True, "files_changed": 0, "files_unmodified": 0,
                    "data_added": 0, "data_added_packed": 0, "total_bytes_processed": 0})
    ]
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events(events)
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events(['{"message_type":"future_event"}'])
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events(["not-json"])
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events(["x" * 33], max_event_bytes=32)


def test_backup_stream_requires_one_zero_error_terminal_summary():
    module = api()
    snapshot_id = "c" * 64
    failed_summary = summary(snapshot_id, errors=1).decode()
    with pytest.raises(module.ResticStoreError) as failure:
        module.parse_backup_events(
            [json.dumps({"message_type": "error", "message": "/private/workspace/file"}), failed_summary]
        )
    assert failure.value.code == module.ResticStoreErrorCode.BACKUP_ERRORS
    assert failure.value.orphan_snapshot_id == snapshot_id
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events([json.dumps({"message_type": "status"})])
    with pytest.raises(module.ResticStoreError):
        module.parse_backup_events([summary(snapshot_id), json.dumps({"message_type": "status"})])


def test_snapshot_and_entry_parsers_return_only_bounded_metadata():
    module = api()
    snapshot_id = "a" * 64
    tree_id = "b" * 64
    source_paths = ["/synthetic/room"]
    snapshot = module.parse_snapshot(
        json.dumps(
            [{"id": snapshot_id, "tree": tree_id, "paths": source_paths,
              "time": "2026-10-02T00:00:00Z"}]
        ),
        snapshot_id,
    )
    entries = list(
        module.parse_snapshot_entries(
            [
                json.dumps(
                    {
                        "struct_type": "snapshot",
                        "message_type": "snapshot",
                        "id": snapshot_id,
                        "tree": tree_id,
                        "paths": source_paths,
                    }
                ),
                json.dumps(
                    {
                        "struct_type": "node",
                        "message_type": "node",
                        "path": "/file.sock",
                        "name": "file.sock",
                        "type": "socket",
                        "mode": 49663,
                        "size": 0,
                    }
                ),
            ],
            snapshot,
        )
    )

    assert snapshot.tree_id == tree_id
    assert entries[0].entry_type == "socket"
    assert entries[0].path == "file.sock"
    assert entries[0].mode == 49663


def test_restic_ls_virtual_paths_are_normalized_and_escape_paths_rejected():
    module = api()
    snapshot = module.SnapshotInfo(
        snapshot_id="a" * 64,
        tree_id="b" * 64,
        parent_snapshot_id=None,
        time="2026-10-02T00:00:00Z",
        paths=("/synthetic/room",),
    )
    header = {
        "struct_type": "snapshot",
        "message_type": "snapshot",
        "id": snapshot.snapshot_id,
        "tree": snapshot.tree_id,
        "paths": list(snapshot.paths),
    }
    valid_node = {
        "struct_type": "node",
        "message_type": "node",
        "path": "/nested/file.txt",
        "name": "file.txt",
        "type": "file",
        "size": 1,
    }

    result = list(
        module.parse_snapshot_entries(
            [json.dumps(header), json.dumps(valid_node)], snapshot
        )
    )
    assert result[0].path == "nested/file.txt"

    for dangerous_path in ("/../../escape", "//absolute", "/nested/../escape", "/nested//file"):
        bad_node = {**valid_node, "path": dangerous_path}
        with pytest.raises(module.ResticStoreError):
            list(module.parse_snapshot_entries([json.dumps(header), json.dumps(bad_node)], snapshot))


def test_restic_ls_requires_matching_message_types_snapshot_id_tree_and_paths():
    module = api()
    snapshot = module.SnapshotInfo(
        snapshot_id="a" * 64,
        tree_id="b" * 64,
        parent_snapshot_id=None,
        time="2026-10-02T00:00:00Z",
        paths=("/synthetic/room",),
    )
    header = {
        "struct_type": "snapshot",
        "message_type": "snapshot",
        "id": snapshot.snapshot_id,
        "tree": snapshot.tree_id,
        "paths": list(snapshot.paths),
    }
    node = {"struct_type": "node", "message_type": "node", "path": "/file", "type": "file"}
    cases = [
        [{**header, "message_type": "node"}, node],
        [{**header, "id": "c" * 64}, node],
        [{**header, "tree": "c" * 64}, node],
        [{**header, "paths": ["/different/root"]}, node],
        [header, {**node, "message_type": "snapshot"}],
    ]
    for records in cases:
        with pytest.raises(module.ResticStoreError):
            list(module.parse_snapshot_entries([json.dumps(record) for record in records], snapshot))


def test_restic_ls_rejects_duplicate_normalized_paths():
    module = api()
    snapshot = module.SnapshotInfo(
        snapshot_id="a" * 64,
        tree_id="b" * 64,
        parent_snapshot_id=None,
        time="2026-10-02T00:00:00Z",
        paths=("/synthetic/room",),
    )
    header = {
        "struct_type": "snapshot",
        "message_type": "snapshot",
        "id": snapshot.snapshot_id,
        "tree": snapshot.tree_id,
        "paths": list(snapshot.paths),
    }
    node = {"struct_type": "node", "message_type": "node", "path": "/file", "type": "file"}

    with pytest.raises(module.ResticStoreError):
        list(module.parse_snapshot_entries([json.dumps(header), json.dumps(node), json.dumps(node)], snapshot))


def test_snapshot_parser_rejects_short_ids_and_invalid_timestamps():
    module = api()
    with pytest.raises(module.ResticStoreError):
        module.parse_snapshot(
            json.dumps([{"id": "abcd1234", "tree": "b" * 64, "time": "2026-10-02T00:00:00Z"}]),
            "abcd1234",
        )
    with pytest.raises(module.ResticStoreError):
        module.parse_snapshot(
            json.dumps([{"id": "a" * 64, "tree": "b" * 64, "time": "not-a-time"}]),
            "a" * 64,
        )


def test_snapshot_entry_stream_enforces_entry_count_bound():
    module = api()
    snapshot = module.SnapshotInfo(
        snapshot_id="a" * 64,
        tree_id="b" * 64,
        parent_snapshot_id=None,
        time="2026-10-02T00:00:00Z",
        paths=("/synthetic/room",),
    )
    entries = [
        json.dumps(
            {"struct_type": "snapshot", "message_type": "snapshot", "id": snapshot.snapshot_id,
             "tree": snapshot.tree_id, "paths": list(snapshot.paths)}
        ),
        json.dumps({"struct_type": "node", "message_type": "node", "path": "/one", "type": "file"}),
        json.dumps({"struct_type": "node", "message_type": "node", "path": "/two", "type": "file"}),
    ]

    with pytest.raises(module.ResticStoreError):
        list(module.parse_snapshot_entries(entries, snapshot, max_entries=1))


class FakeProcess:
    def __init__(self, stdout: bytes = b"", returncode: int = 0, wait_error=None):
        self.stdout = io.BytesIO(stdout)
        self.returncode = returncode
        self.wait_error = wait_error
        self.terminated = False
        self.waited = False
        self.args = None
        self.kwargs = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.wait_error is not None:
            raise self.wait_error
        return self.returncode

    def communicate(self, timeout=None):
        self.waited = True
        return self.stdout.read(), b""


class FakeProcesses:
    def __init__(self, processes):
        self.processes = list(processes)
        self.calls = []

    def __call__(self, args, **kwargs):
        process = self.processes.pop(0)
        process.args = list(args)
        process.kwargs = kwargs
        self.calls.append(process)
        return process


def make_store(tmp_path, processes, terminate=None, command_timeout=300):
    module = api()
    password = tmp_path / "restic-password"
    password.write_text("synthetic-restic-secret\n")
    password.chmod(0o600)
    factory = FakeProcesses(processes)
    store = module.ResticStore(
        repository=str(tmp_path / "repository"),
        cache_dir=tmp_path / "cache",
        password_file=password,
        executable="/managed/restic",
        provider_env={
            "AWS_ACCESS_KEY_ID": "synthetic-access-key",
            "AWS_SECRET_ACCESS_KEY": "synthetic-secret-key",
        },
        process_factory=factory,
        terminate_process=terminate,
        command_timeout=command_timeout,
    )
    return store, factory


def config(repository_id="a" * 64, version=2):
    return json.dumps({"id": repository_id, "version": version}).encode()


def restic_version():
    return b"restic 0.19.1 compiled with go1.26.5 on linux/amd64\n"


def summary(snapshot_id=None, **extra):
    value = {
        "message_type": "summary",
        "snapshot_id": snapshot_id,
        "errors": 0,
        "files_new": 0,
        "files_changed": 0,
        "files_unmodified": 1,
        "data_added": 0,
        "data_added_packed": 0,
        "total_bytes_processed": 12,
    }
    value.update(extra)
    return json.dumps(value).encode() + b"\n"


def initialized_store(tmp_path, backup_processes, terminate=None, command_timeout=300):
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(config()),
            *backup_processes,
        ],
        terminate=terminate,
        command_timeout=command_timeout,
    )
    store.__enter__()
    info = store.initialize()
    assert info.repository_id == "a" * 64
    store.__exit__(None, None, None)
    return store, factory


def test_initialize_reads_existing_repository_and_rejects_format_or_id_mismatch(tmp_path):
    module = api()
    store, factory = make_store(
        tmp_path,
        [FakeProcess(restic_version()), FakeProcess(config(version=1))],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.initialize()
    assert failure.value.code == module.ResticStoreErrorCode.REPOSITORY_FORMAT
    assert [call.args[1:] for call in factory.calls] == [["version"], ["cat", "config"]]

    bad_id_dir = tmp_path / "bad-id"
    bad_id_dir.mkdir()
    bad_store, _ = make_store(
        bad_id_dir,
        [FakeProcess(restic_version()), FakeProcess(config(repository_id="../private/path"))],
    )
    with bad_store, pytest.raises(module.ResticStoreError):
        bad_store.initialize()


def test_initialize_creates_only_format_two_when_repository_is_missing(tmp_path):
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(b"", returncode=10),
            FakeProcess(b"", returncode=0),
            FakeProcess(config()),
        ],
    )
    with store:
        info = store.initialize()

    assert info.repository_format == 2
    assert [call.args[1:] for call in factory.calls] == [
        ["version"],
        ["cat", "config"],
        ["init", "--repository-version", "2"],
        ["cat", "config"],
    ]


def test_initialize_rejects_unpinned_binary_before_repository_access(tmp_path):
    module = api()
    store, factory = make_store(
        tmp_path,
        [FakeProcess(b"restic 0.19.0\n")],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.initialize()

    assert failure.value.code == module.ResticStoreErrorCode.VERSION_MISMATCH
    assert [call.args[1:] for call in factory.calls] == [["version"]]


def test_initialize_accepts_a_concurrent_format_two_creator(tmp_path):
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(b"", returncode=10),
            FakeProcess(b"repository already initialized", returncode=1),
            FakeProcess(config()),
        ],
    )
    with store:
        info = store.initialize()

    assert info.repository_format == 2
    assert [call.args[1:] for call in factory.calls][-2:] == [
        ["init", "--repository-version", "2"],
        ["cat", "config"],
    ]


def test_initialize_rejects_unknown_init_exit_even_if_config_can_be_read(tmp_path):
    module = api()
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(b"", returncode=10),
            FakeProcess(b"", returncode=99),
            FakeProcess(config()),
        ],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.initialize()

    assert failure.value.code == module.ResticStoreErrorCode.UNKNOWN_EXIT
    assert [call.args[1:] for call in factory.calls] == [
        ["version"],
        ["cat", "config"],
        ["init", "--repository-version", "2"],
    ]


def test_open_existing_missing_repository_fails_without_init(tmp_path):
    module = api()
    store, factory = make_store(
        tmp_path,
        [FakeProcess(restic_version()), FakeProcess(b"", returncode=10)],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.open_existing()

    assert failure.value.code == module.ResticStoreErrorCode.REPOSITORY_MISSING
    assert [call.args[1:] for call in factory.calls] == [["version"], ["cat", "config"]]


@pytest.mark.parametrize(
    ("config_payload", "expected_code"),
    [
        (config(version=1), "repository-format-mismatch"),
        (config(repository_id="bad-id"), "repository-id-invalid"),
    ],
)
def test_open_existing_rejects_format_and_repository_id(tmp_path, config_payload, expected_code):
    module = api()
    store, _ = make_store(
        tmp_path,
        [FakeProcess(restic_version()), FakeProcess(config_payload)],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.open_existing()
    assert failure.value.code.value == expected_code


def test_ca_bundle_is_explicit_child_tls_environment_and_never_argv(tmp_path, monkeypatch):
    module = api()
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text("synthetic public CA bundle\n")
    factory = FakeProcesses([FakeProcess(restic_version()), FakeProcess(config()), FakeProcess()])
    store = module.ResticStore(
        repository=tmp_path / "repository",
        cache_dir=tmp_path / "cache",
        password_file=password,
        ca_bundle=ca_bundle,
        executable="/managed/restic",
        process_factory=factory,
    )
    monkeypatch.setenv("RESTIC_TLS_SKIP_VERIFY", "true")
    monkeypatch.setenv("RESTIC_PASSWORD", "unrelated-secret")
    with store:
        store.open_existing()
        assert store.repository_info.repository_format == 2
        store.check()

    check_call = factory.calls[-1]
    assert check_call.kwargs["env"]["RESTIC_CACERT"] == str(ca_bundle)
    assert "RESTIC_TLS_SKIP_VERIFY" not in check_call.kwargs["env"]
    assert "RESTIC_PASSWORD" not in check_call.kwargs["env"]
    assert str(ca_bundle) not in " ".join(check_call.args)
    assert "unrelated-secret" not in " ".join(check_call.args)


def test_ca_bundle_must_be_regular_non_symlink_and_bounded(tmp_path, monkeypatch):
    module = api()
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)
    target = tmp_path / "ca-target.pem"
    target.write_text("synthetic CA\n")
    link = tmp_path / "ca-link.pem"
    link.symlink_to(target)
    large = tmp_path / "large-ca.pem"
    large.write_bytes(b"synthetic CA bundle")
    monkeypatch.setattr(module, "MAX_CA_BUNDLE_BYTES", 4)

    for path in (link, tmp_path, large):
        store = module.ResticStore(
            repository=tmp_path / "repository",
            cache_dir=tmp_path / "cache",
            password_file=password,
            ca_bundle=path,
        )
        with pytest.raises(module.ResticStoreError) as failure:
            store.__enter__()
        assert failure.value.code == module.ResticStoreErrorCode.INVALID_CONFIGURATION


@pytest.mark.parametrize("unsafe", ["mode", "symlink", "owner", "size"])
def test_password_file_uses_canonical_posix_private_path_validation(
    tmp_path, monkeypatch, unsafe
):
    module = api()
    from josh_room import private_paths

    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)
    if unsafe == "mode":
        password.chmod(0o644)
    elif unsafe == "symlink":
        target = tmp_path / "password-target"
        target.write_text("synthetic-password\n")
        target.chmod(0o600)
        password.unlink()
        password.symlink_to(target)
    elif unsafe == "owner":
        actual_uid = os.getuid()
        monkeypatch.setattr(private_paths.os, "getuid", lambda: actual_uid + 1)
    else:
        monkeypatch.setattr(module, "MAX_PASSWORD_FILE_BYTES", 3)

    store = module.ResticStore(
        repository=tmp_path / "repository",
        cache_dir=tmp_path / "cache",
        password_file=password,
    )
    with pytest.raises(module.ResticStoreError) as failure:
        store.__enter__()
    assert failure.value.code == module.ResticStoreErrorCode.INVALID_CONFIGURATION
    assert str(tmp_path) not in str(failure.value)


def test_existing_cache_must_pass_canonical_posix_private_path_validation(tmp_path):
    module = api()
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o755)
    cache.chmod(0o755)
    store = module.ResticStore(
        repository=tmp_path / "repository",
        cache_dir=cache,
        password_file=password,
    )

    with pytest.raises(module.ResticStoreError) as failure:
        store.__enter__()
    assert failure.value.code == module.ResticStoreErrorCode.INVALID_CONFIGURATION


def test_windows_private_paths_delegate_to_protected_dacl_verification(tmp_path, monkeypatch):
    module = api()
    from josh_room import private_paths

    sid = "S-1-5-21-1000"

    class FakeWindowsAPI:
        def __init__(self):
            self.applied = []
            self.verified = []

        def current_user_sid(self):
            return sid

        def apply_private_acl(self, path, owner_sid, *, directory):
            self.applied.append((Path(path), owner_sid, directory))

        def read_security(self, path):
            target = Path(path)
            self.verified.append(target)
            return sid, True, private_paths._expected_aces(sid, directory=target.is_dir())

    fake_api = FakeWindowsAPI()
    monkeypatch.setattr(private_paths, "_is_windows", lambda: True)
    monkeypatch.setattr(private_paths, "_windows_api", lambda: fake_api)
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o644)  # Windows ACL, not POSIX mode bits, is authoritative here.
    cache = tmp_path / "cache"
    store = module.ResticStore(
        repository=tmp_path / "repository",
        cache_dir=cache,
        password_file=password,
    )

    with store:
        pass

    assert fake_api.applied == [(cache, sid, True)]
    assert password in fake_api.verified
    assert cache in fake_api.verified


def test_windows_private_paths_reject_unprotected_or_wrong_owner_dacl(tmp_path, monkeypatch):
    module = api()
    from josh_room import private_paths

    sid = "S-1-5-21-1000"
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)

    class UnsafeWindowsAPI:
        def current_user_sid(self):
            return sid

        def read_security(self, _path):
            return "S-1-5-21-other", False, ()

    monkeypatch.setattr(private_paths, "_is_windows", lambda: True)
    monkeypatch.setattr(private_paths, "_windows_api", UnsafeWindowsAPI)
    store = module.ResticStore(
        repository=tmp_path / "repository",
        cache_dir=tmp_path / "cache",
        password_file=password,
    )

    with pytest.raises(module.ResticStoreError) as failure:
        store.__enter__()
    assert failure.value.code == module.ResticStoreErrorCode.INVALID_CONFIGURATION


def test_backup_uses_parent_relative_dot_and_returns_noop_without_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    status = json.dumps(
        {"message_type": "status", "files_done": 1, "total_files": 1,
         "bytes_done": 12, "total_bytes": 12}
    ).encode() + b"\n"
    store, factory = initialized_store(
        tmp_path,
        [FakeProcess(status + summary(None))],
    )
    progress_events = []
    excludes = tmp_path / "capture.exclude"
    excludes.write_text(".venv/\n")
    with store:
        result = store.backup(
            workspace,
            parent="b" * 64,
            excludes=excludes,
            on_progress=progress_events.append,
        )

    call = factory.calls[-1]
    assert call.args[1:] == [
        "backup",
        "--json",
        "--skip-if-unchanged",
        "--parent",
        "b" * 64,
        "--exclude-file",
        str(excludes),
        "--",
        ".",
    ]
    assert call.kwargs["cwd"] == workspace
    assert result.snapshot_id is None
    assert result.data_added_packed == 0
    assert progress_events[0].files_done == 1
    assert progress_events[0].total_files == 1


def test_backup_exit_three_is_incomplete_with_only_bounded_orphan_id(tmp_path):
    module = api()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orphan_id = "c" * 64
    store, _ = initialized_store(
        tmp_path,
        [FakeProcess(summary(orphan_id), returncode=3)],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.backup(workspace)

    assert failure.value.code == module.ResticStoreErrorCode.INCOMPLETE_BACKUP
    assert failure.value.orphan_snapshot_id == orphan_id

    no_summary_dir = tmp_path / "no-summary"
    no_summary_dir.mkdir()
    no_summary_workspace = no_summary_dir / "workspace"
    no_summary_workspace.mkdir()
    no_summary_store, _ = initialized_store(
        no_summary_dir,
        [FakeProcess(b"", returncode=3)],
    )
    with no_summary_store, pytest.raises(module.ResticStoreError) as no_summary_failure:
        no_summary_store.backup(no_summary_workspace)
    assert no_summary_failure.value.code == module.ResticStoreErrorCode.INCOMPLETE_BACKUP
    assert no_summary_failure.value.orphan_snapshot_id is None


@pytest.mark.parametrize(
    ("exit_code", "expected_code"),
    [
        (130, "cancelled"),
        (99, "unknown-exit"),
    ],
)
def test_backup_classifies_cancelled_and_unknown_exit_with_bounded_orphan(
    tmp_path, exit_code, expected_code
):
    module = api()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    orphan_id = "e" * 64
    store, _ = initialized_store(
        tmp_path,
        [FakeProcess(summary(orphan_id), returncode=exit_code)],
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.backup(workspace)

    assert failure.value.code.value == expected_code
    assert failure.value.orphan_snapshot_id == orphan_id


def test_backup_hides_diagnostics_secrets_and_paths_and_terminates_unknown_output(tmp_path):
    module = api()
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()
    process = FakeProcess(
        json.dumps(
            {
                "message_type": "future_event",
                "message": "synthetic-secret /private/customer/workspace",
            }
        ).encode()
        + b"\n",
        returncode=None,
    )
    terminated = []
    store, _ = initialized_store(
            tmp_path,
            [process],
            terminate=lambda child: (
                terminated.append(child),
                setattr(child, "terminated", True),
                setattr(child, "returncode", -15),
            ),
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.backup(workspace)

    assert failure.value.code == module.ResticStoreErrorCode.INVALID_OUTPUT
    assert "synthetic-secret" not in str(failure.value)
    assert "/private/customer/workspace" not in str(failure.value)
    assert terminated == [process]
    assert process.terminated


def test_provider_environment_is_allowlisted_and_password_never_enters_argv(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("AWS_PROFILE", "private-profile")
    monkeypatch.setenv("JOSH_ROOM_SECRET", "do-not-pass")
    store, factory = initialized_store(tmp_path, [FakeProcess(summary("d" * 64))])
    with store:
        store.backup(workspace)

    child = factory.calls[-1]
    assert child.kwargs["env"]["AWS_ACCESS_KEY_ID"] == "synthetic-access-key"
    assert "AWS_PROFILE" not in child.kwargs["env"]
    assert "JOSH_ROOM_SECRET" not in child.kwargs["env"]
    assert "synthetic-restic-secret" not in " ".join(child.args)


@pytest.mark.parametrize(
    "repository",
    [
        "s3:http://storage.example.test/bucket/repository",
        "s3:https://synthetic-user:synthetic-secret@storage.example.test/bucket/repository",
        "s3:https://storage.example.test/bucket/repository?password=synthetic-secret",
        "s3:https://storage.example.test:invalid/bucket/repository",
    ],
)
def test_s3_repository_locator_rejects_embedded_credentials_and_unverified_urls(
    tmp_path, repository
):
    module = api()
    password = tmp_path / "password"
    password.write_text("synthetic-password\n")
    password.chmod(0o600)

    with pytest.raises(module.ResticStoreError) as failure:
        module.ResticStore(
            repository=repository,
            cache_dir=tmp_path / "cache",
            password_file=password,
        )

    assert failure.value.code == module.ResticStoreErrorCode.INVALID_CONFIGURATION
    assert "synthetic-secret" not in str(failure.value)


def test_backup_cancellation_uses_shared_owned_process_terminator(tmp_path):
    module = api()
    from josh_room.adapter_contract import CancellationToken

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    status = json.dumps({"message_type": "status", "files_done": 1}).encode() + b"\n"
    process = FakeProcess(status + summary("f" * 64), returncode=None)
    token = CancellationToken()
    terminated = []
    store, _ = initialized_store(
        tmp_path,
        [process],
        terminate=lambda child: (
            terminated.append(child),
            setattr(child, "returncode", -15),
        ),
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.backup(workspace, on_progress=lambda _progress: token.cancel(), cancellation=token)

    assert failure.value.code == module.ResticStoreErrorCode.CANCELLED
    assert terminated == [process]
    assert process.stdout.closed


def test_timeout_uses_shared_owned_process_terminator(tmp_path):
    module = api()
    timeout_process = FakeProcess(
        returncode=None,
        wait_error=subprocess.TimeoutExpired("restic", timeout=0.01),
    )
    terminated = []
    store, _ = initialized_store(
        tmp_path,
        [timeout_process],
        terminate=lambda child: (
            terminated.append(child),
            setattr(child, "returncode", -15),
        ),
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.check()

    assert failure.value.code == module.ResticStoreErrorCode.TIMED_OUT
    assert terminated == [timeout_process]
    assert timeout_process.stdout.closed


def test_silent_pipe_timeout_uses_shared_terminator(tmp_path):
    module = api()
    read_fd, write_fd = os.pipe()
    process = FakeProcess(returncode=None)
    process.stdout.close()
    process.stdout = os.fdopen(read_fd, "rb", buffering=0)
    terminated = []

    def terminate(child):
        terminated.append(child)
        os.close(write_fd)
        child.returncode = -15

    store, _ = initialized_store(
        tmp_path,
        [process],
        terminate=terminate,
        command_timeout=0.02,
    )
    with store, pytest.raises(module.ResticStoreError) as failure:
        store.check()

    assert failure.value.code == module.ResticStoreErrorCode.TIMED_OUT
    assert terminated == [process]
    assert process.stdout.closed


def test_closing_entries_early_terminates_the_owned_process(tmp_path):
    snapshot_id = "a" * 64
    snapshot_metadata = json.dumps(
        [{"id": snapshot_id, "tree": "b" * 64, "paths": ["/synthetic/room"],
          "time": "2026-10-02T00:00:00Z"}]
    ).encode()
    header = json.dumps(
        {"struct_type": "snapshot", "message_type": "snapshot", "id": snapshot_id,
         "tree": "b" * 64, "paths": ["/synthetic/room"]}
    ).encode() + b"\n"
    first_entry = json.dumps(
        {"struct_type": "node", "message_type": "node", "path": "/one", "type": "file", "size": 1}
    ).encode() + b"\n"
    second_entry = json.dumps(
        {"struct_type": "node", "message_type": "node", "path": "/two", "type": "file", "size": 2}
    ).encode() + b"\n"
    process = FakeProcess(header + first_entry + second_entry, returncode=None)
    terminated = []
    store, _ = initialized_store(
        tmp_path,
        [FakeProcess(snapshot_metadata), process],
        terminate=lambda child: (
            terminated.append(child),
            setattr(child, "returncode", -15),
        ),
    )
    with store:
        stream = store.entries(snapshot_id)
        assert next(stream).path == "one"
        stream.close()

    assert terminated == [process]
    assert process.stdout.closed


def test_snapshot_entries_restore_and_check_use_only_non_destructive_commands(tmp_path):
    snapshot_id = "1" * 64
    snapshot_json = json.dumps(
        [{"id": snapshot_id, "tree": "2" * 64, "paths": ["/synthetic/room"],
          "time": "2026-10-02T00:00:00Z"}]
    ).encode()
    entries_json = (
        json.dumps(
            {"struct_type": "snapshot", "message_type": "snapshot", "id": snapshot_id,
             "tree": "2" * 64, "paths": ["/synthetic/room"]}
        ).encode()
        + b"\n"
        + json.dumps(
            {"struct_type": "node", "message_type": "node", "path": "/workspace.txt",
             "type": "file", "size": 7}
        ).encode()
        + b"\n"
    )
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(config()),
            FakeProcess(snapshot_json),
            FakeProcess(entries_json),
            FakeProcess(),
            FakeProcess(),
            FakeProcess(),
        ],
    )
    destination = tmp_path / "restore-stage"
    with store:
        store.initialize()
        assert store.snapshot(snapshot_id).snapshot_id == snapshot_id
        assert next(iter(store.entries(snapshot_id))).path == "workspace.txt"
        store.restore(snapshot_id, destination)
        assert store.check().read_data is False
        assert store.check(read_data=True).read_data is True

    commands = [call.args[1:] for call in factory.calls]
    assert commands[-5] == ["snapshots", "--json", snapshot_id]
    assert commands[-4] == ["ls", "--json", snapshot_id]
    assert commands[-3] == ["restore", snapshot_id, "--target", str(destination)]
    assert commands[-2] == ["check"]
    assert commands[-1] == ["check", "--read-data"]
    assert all("prune" not in command and "forget" not in command for command in commands)


def test_snapshot_metadata_cache_is_cleared_at_context_boundary(tmp_path):
    snapshot_id = "1" * 64
    snapshot_json = json.dumps(
        [{"id": snapshot_id, "tree": "2" * 64, "paths": ["/synthetic/room"],
          "time": "2026-10-02T00:00:00Z"}]
    ).encode()
    store, factory = make_store(
        tmp_path,
        [
            FakeProcess(restic_version()),
            FakeProcess(config()),
            FakeProcess(snapshot_json),
            FakeProcess(restic_version()),
            FakeProcess(config()),
            FakeProcess(snapshot_json),
        ],
    )
    with store:
        store.initialize()
        store.snapshot(snapshot_id)
        store.snapshot(snapshot_id)
    with store:
        store.initialize()
        store.snapshot(snapshot_id)

    assert [call.args[1:] for call in factory.calls].count(
        ["snapshots", "--json", snapshot_id]
    ) == 2
