from __future__ import annotations

import os

import pytest

from josh_room import cli, local_save_receipt
from josh_room.config import DimensionConfig
from josh_room.room_store_operations import scan_workspace_for_status
from josh_room.workspace_state import write_stat_workspace_marker


@pytest.fixture
def saved_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_VERSION", "0.1.26")
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "source.txt").write_bytes(b"initial")
    instance = tmp_path / "private-state"
    dimension = DimensionConfig(
        "primary",
        "Synthetic",
        "minio",
        "https://synthetic.invalid",
        "synthetic",
        "opaque-profile",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    scan = scan_workspace_for_status(source)
    marker = write_stat_workspace_marker(
        source,
        dimension_id=dimension.dimension_id,
        encryption_domain_id=dimension.encryption_domain_id,
        project_id="room-test",
        snapshot_id="saved-point",
        display_name="Room Test",
        workspace_signature=scan.signature,
        signature_algorithm=scan.signature_algorithm,
        capture_policy_sha256=scan.capture_policy_sha256,
    )
    descriptor = {
        "logical_jat_id": "saved-point",
        "dimension_id": dimension.dimension_id,
        "encryption_domain_id": dimension.encryption_domain_id,
        "room_id": "room-test",
        "workspace": {
            "repository_id": "a" * 64,
            "snapshot_id": "b" * 64,
            "tree_id": "c" * 64,
        },
        "components": {
            "rcc_environment": None,
            "homebrew_recovery": None,
            "hauler_content": None,
        },
        "producer": local_save_receipt.current_producer(),
        "source": {},
    }
    result = {
        "ok": True,
        "status": "saved",
        "dimension_id": dimension.dimension_id,
        "encryption_domain_id": dimension.encryption_domain_id,
        "project_id": "room-test",
        "snapshot_id": "saved-point",
        "workspace_snapshot_id": "b" * 64,
        "workspace_signature": scan.signature,
        "signature_algorithm": scan.signature_algorithm,
        "capture_policy_sha256": scan.capture_policy_sha256,
        "display_name": "Room Test",
    }
    assert local_save_receipt.write_verified_receipt(
        instance, source, dimension, descriptor, result
    )
    return instance, source, dimension, descriptor, result, marker


def test_fresh_cli_noop_precedes_all_auth_provider_and_runtime_work(
    saved_workspace, monkeypatch, capsys
):
    instance, source, dimension, *_ = saved_workspace
    monkeypatch.setenv("JOSH_ROOM_INSTANCE", str(instance))
    monkeypatch.setattr(
        cli,
        "private_config",
        lambda: {"dimensions": {"primary": dimension.to_private()}},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("trusted local Save reached auth, provider or runtime")

    monkeypatch.setattr(cli, "_selected_encryption_environment", forbidden)
    monkeypatch.setattr(cli, "load_runtime_session", forbidden)
    monkeypatch.setattr(cli, "dispatch", forbidden)
    assert (
        cli.main(
            [
                "snapshot",
                "create",
                "Room Test",
                "--source",
                str(source),
                "--backend",
                "minio",
                "--dimension",
                "primary",
                "--json",
            ]
        )
        == 0
    )
    assert '"status": "already-saved"' in capsys.readouterr().out


def test_same_size_mtime_restored_edit_misses(saved_workspace):
    instance, source, dimension, *_ = saved_workspace
    file = source / "source.txt"
    before = file.stat()
    file.write_bytes(b"changed")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (
        local_save_receipt.read_noop(instance, source, dimension, "room-test") is None
    )


def test_insecure_receipt_misses(saved_workspace):
    instance, source, dimension, *_ = saved_workspace
    local_save_receipt.receipt_path(instance, source).chmod(0o666)
    assert (
        local_save_receipt.read_noop(instance, source, dimension, "room-test") is None
    )


def test_changed_producer_misses(saved_workspace, monkeypatch):
    instance, source, dimension, *_ = saved_workspace
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_VERSION", "0.1.27")
    assert (
        local_save_receipt.read_noop(instance, source, dimension, "room-test") is None
    )


def test_changed_policy_misses(saved_workspace):
    instance, source, dimension, *_ = saved_workspace
    (source / ".josh-roomignore").write_text("source.txt\n")
    assert (
        local_save_receipt.read_noop(instance, source, dimension, "room-test") is None
    )


def test_changed_physical_binding_misses(saved_workspace):
    from dataclasses import replace

    instance, source, dimension, *_ = saved_workspace
    changed = replace(dimension, bucket="another-synthetic-bucket")
    assert local_save_receipt.read_noop(instance, source, changed, "room-test") is None


def test_symlink_receipt_misses(saved_workspace, tmp_path):
    instance, source, dimension, *_ = saved_workspace
    path = local_save_receipt.receipt_path(instance, source)
    other = tmp_path / "untrusted.json"
    path.rename(other)
    path.symlink_to(other)
    assert (
        local_save_receipt.read_noop(instance, source, dimension, "room-test") is None
    )


def test_direct_save_cancellation_precedes_cached_success(saved_workspace):
    from types import SimpleNamespace

    from josh_room.cancellation import CLICancelled
    from josh_room.room_store_bridge import save_room_store

    instance, source, dimension, *_ = saved_workspace
    with pytest.raises(CLICancelled):
        save_room_store(
            instance,
            dimension,
            "room-test",
            source,
            None,
            components=[],
            cancellation=SimpleNamespace(cancelled=True),
        )


def test_direct_cache_does_not_bypass_material_or_authority_validation(saved_workspace):
    from types import SimpleNamespace

    from josh_room.room_store_bridge import RoomStoreBridgeError, save_room_store

    instance, source, dimension, *_ = saved_workspace
    with pytest.raises(RoomStoreBridgeError) as wrong_material:
        save_room_store(
            instance,
            dimension,
            "room-test",
            source,
            SimpleNamespace(encryption_domain_id=dimension.encryption_domain_id),
            components=[],
        )
    assert wrong_material.value.code == "material-required"
    with pytest.raises(RoomStoreBridgeError) as wrong_authority:
        save_room_store(
            instance,
            dimension,
            "room-test",
            source,
            None,
            components=[],
            authority_session=object(),
        )
    assert wrong_authority.value.code == "provider-binding-mismatch"


def test_receipt_inside_workspace_is_never_created(saved_workspace):
    _, source, dimension, descriptor, result, _ = saved_workspace
    assert not local_save_receipt.write_verified_receipt(
        source, source, dimension, descriptor, result
    )
    assert not (source / "save-receipts").exists()


@pytest.mark.parametrize("status", ["saved-but-dirty", "cancelled", "unknown"])
def test_dirty_cancelled_uncertain_finish_does_not_create_receipt(
    saved_workspace, tmp_path, status
):
    _, source, dimension, descriptor, result, _ = saved_workspace
    instance = tmp_path / "other-state"
    assert not local_save_receipt.write_verified_receipt(
        instance, source, dimension, descriptor, {**result, "status": status}
    )
    assert not local_save_receipt.receipt_path(instance, source).exists()
