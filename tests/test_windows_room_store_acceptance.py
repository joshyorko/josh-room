from __future__ import annotations

import hashlib

import pytest

from josh_room.object_store import ObjectStore
from josh_room.restic_store import SnapshotEntry
from josh_room.room_store_operations import (
    RoomStoreOperationsError,
    _validate_snapshot_entries,
)
from scripts.windows_room_store_acceptance import (
    FIXTURE_STORE_KIND,
    AcceptanceFailure,
    FixtureObjectStore,
    _failure_receipt,
    _portable_mode,
    run_acceptance,
    verify_hostile_inventory_entries,
)


def test_fixture_store_uses_explicit_non_minio_label_and_conditional_catalog_writes():
    store = FixtureObjectStore()
    assert FIXTURE_STORE_KIND == "in-memory-fixture"
    assert store.real_minio is False
    assert isinstance(store, ObjectStore)

    first = store.conditional_catalog_put(b"first", None)
    assert store.read_catalog() == (b"first", first)
    with pytest.raises(RuntimeError, match="fixture catalog etag conflict"):
        store.conditional_catalog_put(b"stale", None)
    assert store.read_catalog() == (b"first", first)

    body = b"body"
    digest = hashlib.sha256(body).hexdigest()
    reference = store.put_bytes(f"objects/sha256/{digest}", body)
    assert reference.sha256 == digest
    assert reference.size == len(body)
    with pytest.raises(ValueError, match="object key does not match"):
        store.put_bytes(f"objects/sha256/{digest}", b"different")


def test_native_room_store_entry_guards_reject_windows_collisions_and_special_files():
    assert verify_hostile_inventory_entries() == {
        "casefold-collision": "rejected",
        "unicode-normalization-collision": "rejected",
        "windows-reserved-name": "rejected",
        "special-file": "rejected",
        "unsafe-symlink": "rejected",
    }


def test_fixture_acceptance_cannot_report_a_non_windows_host_as_a_pass(tmp_path):
    with pytest.raises(AcceptanceFailure, match="native-windows-required"):
        run_acceptance(
            restic_executable=tmp_path / "restic.exe",
            operational_identity=tmp_path / "operational.age",
            recovery_identity=tmp_path / "recovery.age",
            jat_root=tmp_path / "jat",
            output=tmp_path / "capsule.haul.tar.zst",
        )


def test_failure_receipt_reports_safe_bridge_diagnostics_without_paths():
    class BridgeFailure(RuntimeError):
        def __init__(self, message):
            self.code = "restore-failed"
            self.result = {
                "error_type": "FileNotFoundError",
                "error_site": "run_restore",
                "error_line": 41,
                "missing_attribute": "payload_path",
                "path": "C:\\Users\\runner\\private\\workspace",
            }
            super().__init__(message)

    receipt = _failure_receipt(BridgeFailure("C:\\Users\\runner\\private\\workspace"))

    assert receipt == {
        "status": "failed",
        "error_type": "BridgeFailure",
        "error_code": "restore-failed",
        "cause_type": "FileNotFoundError",
        "error_site": "run_restore",
        "error_line": 41,
        "missing_attribute": "payload_path",
    }


def test_failure_receipt_reports_path_free_room_operation_site():
    with pytest.raises(RoomStoreOperationsError) as failure:
        _validate_snapshot_entries([SnapshotEntry("../private", "file", 1, 0o600, None)])

    receipt = _failure_receipt(failure.value)

    assert set(receipt) == {
        "status", "error_type", "failed_check", "error_site", "error_line"
    }
    assert receipt["status"] == "failed"
    assert receipt["error_type"] == "RoomStoreOperationsError"
    assert receipt["failed_check"] == "snapshot contains an unsafe path"
    assert receipt["error_site"] == "_relative_parts"
    assert isinstance(receipt["error_line"], int)


def test_windows_mode_assertion_uses_readonly_semantics_but_posix_is_exact():
    assert _portable_mode(0o444, "nt") == "read-only"
    assert _portable_mode(0o555, "nt") == "read-only"
    assert _portable_mode(0o666, "nt") == "writable"
    assert _portable_mode(0o444, "posix") == "0444"
    assert _portable_mode(0o555, "posix") == "0555"
