"""Opt-in, small real-engine contracts. No provider credentials are inherited."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from josh_room.restic_store import ResticStore, ResticStoreError
from josh_room.rustic_store import RusticStore


@pytest.fixture
def binary():
    value = os.environ.get("JOSH_ROOM_RUSTIC_TEST_BINARY")
    if not value:
        pytest.skip("explicit pinned Rustic test binary was not supplied")
    return str(Path(value).resolve(strict=True))


@pytest.fixture
def authorities(tmp_path):
    password = tmp_path / "password"
    password.write_text("synthetic-live-contract-password")
    password.chmod(0o600)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "subdir").mkdir()
    (workspace / "subdir/file.txt").write_text("before\n")
    (workspace / "subdir/file.txt").chmod(0o640)
    if os.name == "posix":
        (workspace / "shortcut").symlink_to("subdir/file.txt")
    return {
        "repository": tmp_path / "repository",
        "cache_dir": tmp_path / "cache",
        "password_file": password,
    }, workspace


def lifecycle(store, workspace, tmp_path):
    with store as opened:
        assert opened.initialize().repository_format == 2
        seed = opened.backup(workspace)
        assert seed.snapshot_id
        seed_info = opened.snapshot(seed.snapshot_id)
        rows = list(
            opened.entries(seed.snapshot_id, expected_tree_id=seed_info.tree_id)
        )
        assert "subdir/file.txt" in {row.path for row in rows}
        if os.name == "posix":
            assert (
                next(row for row in rows if row.path == "subdir/file.txt").mode & 0o777
                == 0o640
            )
            assert (
                next(row for row in rows if row.path == "shortcut").link_target
                == "subdir/file.txt"
            )
        assert rows == list(
            opened.entries(seed.snapshot_id, expected_tree_id=seed_info.tree_id)
        )
        (workspace / "subdir/file.txt").write_text("after!\n")
        changed = opened.backup(workspace, parent=seed.snapshot_id)
        assert changed.snapshot_id
        assert (
            opened.snapshot(changed.snapshot_id).parent_snapshot_id == seed.snapshot_id
        )
        noop = opened.backup(workspace, parent=changed.snapshot_id)
        if os.name != "nt":
            assert noop.snapshot_id is None
        assert len(opened.snapshots()) == 2
        opened.check()
        opened.check(read_data=True)
        opened.check(read_data_subset="1/2")
        destination = tmp_path / "restored"
        opened.restore(changed.snapshot_id, destination)
        assert (destination / "subdir/file.txt").read_text() == "after!\n"
        plan = opened.plan_forget([seed.snapshot_id])
        with pytest.raises(ResticStoreError):
            opened.forget([seed.snapshot_id], plan=plan)
        opened.forget([seed.snapshot_id], plan=plan, confirmed=True)
        assert [item.snapshot_id for item in opened.snapshots()] == [
            changed.snapshot_id
        ]
        opened.prune()
        with pytest.raises(ResticStoreError):
            opened.prune(dry_run=False)
        opened.prune(dry_run=False, confirmed=True)
        opened.check(read_data=True)
        return opened.repository_info.repository_id, changed.snapshot_id


def test_real_local_lifecycle(binary, authorities, tmp_path):
    kwargs, workspace = authorities
    lifecycle(
        RusticStore(**kwargs, executable=binary, command_timeout=15),
        workspace,
        tmp_path,
    )


def test_real_policy_excludes_are_exclusions(binary, authorities, tmp_path):
    kwargs, workspace = authorities
    (workspace / "private").mkdir()
    (workspace / "private/token").write_text("synthetic-excluded-data")
    (workspace / "private.txt").write_text("synthetic-excluded-data")
    excludes = tmp_path / "policy"
    excludes.write_text("private\nprivate/**\n**/private.txt\n**/private.txt/**\n")
    with RusticStore(**kwargs, executable=binary) as store:
        store.initialize()
        result = store.backup(workspace, excludes=excludes)
        paths = {row.path for row in store.entries(result.snapshot_id)}
        assert "subdir/file.txt" in paths
        assert not any(path.startswith("private") for path in paths)


def test_restic_rustic_repository_round_trip(binary, authorities, tmp_path):
    restic = os.environ.get("JOSH_ROOM_RESTIC_TEST_BINARY")
    if not restic:
        pytest.skip("explicit pinned Restic test binary was not supplied")
    kwargs, workspace = authorities
    with ResticStore(**kwargs, executable=restic) as store:
        identity = store.initialize().repository_id
        seed = store.backup(workspace)
    with RusticStore(**kwargs, executable=binary) as store:
        assert store.open_existing().repository_id == identity
        info = store.snapshot(seed.snapshot_id)
        list(store.entries(seed.snapshot_id, expected_tree_id=info.tree_id))
        (workspace / "subdir/file.txt").write_text("rustic\n")
        changed = store.backup(workspace, parent=seed.snapshot_id)
        store.check(read_data=True)
    with ResticStore(
        **{**kwargs, "cache_dir": tmp_path / "restic-cache"}, executable=restic
    ) as store:
        assert store.open_existing().repository_id == identity
        assert (
            store.snapshot(changed.snapshot_id).parent_snapshot_id == seed.snapshot_id
        )
        store.check(read_data=True)
        store.restore(changed.snapshot_id, tmp_path / "restic-restore")
        assert (tmp_path / "restic-restore/subdir/file.txt").read_text() == "rustic\n"


def test_s3_tls_lifecycle(binary, authorities, tmp_path):
    endpoint = os.environ.get("JOSH_ROOM_SYNTHETIC_MINIO_ENDPOINT")
    ca = os.environ.get("JOSH_ROOM_SYNTHETIC_MINIO_CA")
    if not endpoint or not ca:
        pytest.skip("isolated synthetic TLS MinIO fixture was not supplied")
    kwargs, workspace = authorities
    # Synthetic test-only credentials; no host/provider secrets are read.
    kwargs.update(
        repository=f"s3:{endpoint}/synthetic-rustic-contract/repository",
        ca_bundle=Path(ca),
        provider_env={
            "AWS_ACCESS_KEY_ID": "synthetic-contract-user",
            "AWS_SECRET_ACCESS_KEY": "synthetic-contract-password",
        },
    )
    lifecycle(
        RusticStore(**kwargs, executable=binary, command_timeout=15),
        workspace,
        tmp_path,
    )


def test_room_store_operations_real_save_noop_restore(binary, tmp_path):
    import base64
    from types import SimpleNamespace

    from josh_room.private_paths import secure_private_file, validate_private_directory
    from josh_room.room_store_bridge import _restic_store_factory
    from josh_room.room_store_operations import RoomStoreOperations

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("before\n")
    secret = SimpleNamespace(
        secret=base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("="),
        generation=1,
        repository_id=None,
    )
    keyset = SimpleNamespace(room_store=secret)
    state = {"latest": None, "etag": None, "signature": None}

    def bind(_dimension, _backend, repository_id, **_kwargs):
        secret.repository_id = repository_id
        return keyset

    def publish(descriptor, expected_etag=None, **kwargs):
        state["latest"] = descriptor
        state["etag"] = descriptor.to_dict()["logical_jat_id"]
        state["signature"] = (
            kwargs["workspace_signature"],
            kwargs["signature_algorithm"],
            descriptor.to_dict()["capture_policy_sha256"],
        )

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o700)

    def factory(**kwargs):
        return _restic_store_factory(
            **kwargs, provider_env={}, ca_bundle=None, executable=Path(binary)
        )

    previous = os.environ.get("JOSH_ROOM_STORE_ENGINE")
    os.environ["JOSH_ROOM_STORE_ENGINE"] = "rustic"
    try:
        operations = RoomStoreOperations(
            workspace=workspace,
            repository=str(tmp_path / "repository"),
            cache_dir=cache,
            password_dir=private,
            store_factory=factory,
            dimension=object(),
            backend=object(),
            ensure_keyset=lambda *_: keyset,
            bind_repository=bind,
            read_latest=lambda: (state["latest"], state["etag"]),
            read_catalog_signature=lambda: state["signature"],
            publish_descriptor=publish,
            write_marker=lambda *_args, **_kwargs: None,
            descriptor_metadata={
                "dimension_id": "dimension-test",
                "encryption_domain_id": "domain-test",
                "room_id": "room-test",
                "components": {
                    "rcc_environment": None,
                    "homebrew_recovery": None,
                    "hauler_content": None,
                },
                "source": {},
                "producer": {
                    "josh_room_version": "0.1.27",
                    "restic_version": "rustic-0.11.4",
                    "source_platform": "linux-x64",
                    "restore_platforms": ["linux-x64"],
                },
            },
            secure_private_file=secure_private_file,
            validate_private_directory=validate_private_directory,
        )
        assert operations.save().status == "saved"
        seed = state["latest"]
        assert operations.save().status == "already-saved"
        (workspace / "file.txt").write_text("after!\n")
        assert operations.save().status == "saved"
        changed = state["latest"]
        assert (
            changed.to_dict()["workspace"]["parent_snapshot_id"]
            == seed.to_dict()["workspace"]["snapshot_id"]
        )
        result = operations.restore(
            changed, tmp_path / "restored", write_restore_marker=lambda *_: None
        )
        assert (tmp_path / "restored/file.txt").read_text() == "after!\n"
        assert result.destination == tmp_path / "restored"
    finally:
        if previous is None:
            os.environ.pop("JOSH_ROOM_STORE_ENGINE", None)
        else:
            os.environ["JOSH_ROOM_STORE_ENGINE"] = previous


def test_real_source_error_never_returns_success(binary, authorities):
    if os.name != "posix":
        pytest.skip("POSIX unreadable-source fixture")
    from josh_room.restic_store import ResticStoreErrorCode

    kwargs, workspace = authorities
    source = workspace / "subdir/file.txt"
    source.chmod(0)
    try:
        with RusticStore(**kwargs, executable=binary) as store:
            store.initialize()
            with pytest.raises(ResticStoreError) as error:
                store.backup(workspace)
            assert error.value.code == ResticStoreErrorCode.BACKUP_ERRORS
            assert store.data_added_bytes == 0
            store.check(read_data=True)
    finally:
        source.chmod(0o600)


def test_real_progress_cancellation_leaves_checkable_repository(binary, authorities):
    from josh_room.adapter_contract import CancellationToken
    from josh_room.restic_store import ResticStoreErrorCode

    kwargs, workspace = authorities
    token = CancellationToken()
    with RusticStore(**kwargs, executable=binary) as store:
        store.initialize()
        with pytest.raises(ResticStoreError) as error:
            store.backup(
                workspace, on_progress=lambda _event: token.cancel(), cancellation=token
            )
        assert error.value.code == ResticStoreErrorCode.CANCELLED
        assert store.data_added_bytes == 0
        store.check(read_data=True)


def test_s3_untrusted_ca_never_initializes(binary, authorities):
    endpoint = os.environ.get("JOSH_ROOM_SYNTHETIC_MINIO_ENDPOINT")
    if not endpoint:
        pytest.skip("isolated synthetic TLS MinIO fixture was not supplied")
    kwargs, _workspace = authorities
    kwargs.update(
        repository=f"s3:{endpoint}/synthetic-rustic-contract/untrusted",
        provider_env={
            "AWS_ACCESS_KEY_ID": "synthetic-contract-user",
            "AWS_SECRET_ACCESS_KEY": "synthetic-contract-password",
        },
    )
    with RusticStore(**kwargs, executable=binary, command_timeout=1) as store:
        with pytest.raises(ResticStoreError):
            store.initialize()
        assert store._repository_info is None


def test_s3_wrong_credentials_never_initialize(binary, authorities):
    endpoint = os.environ.get("JOSH_ROOM_SYNTHETIC_MINIO_ENDPOINT")
    ca = os.environ.get("JOSH_ROOM_SYNTHETIC_MINIO_CA")
    if not endpoint or not ca:
        pytest.skip("isolated synthetic TLS MinIO fixture was not supplied")
    kwargs, _workspace = authorities
    kwargs.update(
        repository=f"s3:{endpoint}/synthetic-rustic-contract/unauthorized",
        ca_bundle=Path(ca),
        provider_env={
            "AWS_ACCESS_KEY_ID": "synthetic-contract-user",
            "AWS_SECRET_ACCESS_KEY": "synthetic-wrong-password",
        },
    )
    with RusticStore(**kwargs, executable=binary, command_timeout=2) as store:
        with pytest.raises(ResticStoreError):
            store.initialize()
        assert store._repository_info is None
