"""Native Windows Room Store acceptance with an explicit fixture storage boundary."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any

from josh_room.local_store import ObjectRef
from josh_room.restic_store import SnapshotEntry
from josh_room.room_store_operations import (
    RoomStoreOperationsError,
    _validate_snapshot_entries,
)

FIXTURE_STORE_KIND = "in-memory-fixture"


class AcceptanceFailure(RuntimeError):
    """Path-free failed check name for the hosted acceptance receipt."""


def _failure_receipt(error: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "failed", "error_type": type(error).__name__}
    code = getattr(error, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", code):
        result["error_code"] = code
    details = getattr(error, "result", None)
    if isinstance(details, Mapping):
        for key, target in (
            ("error_type", "cause_type"),
            ("error_site", "error_site"),
            ("missing_attribute", "missing_attribute"),
        ):
            value = details.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", value):
                result[target] = value
        line = details.get("error_line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            result["error_line"] = line
    if isinstance(error, AcceptanceFailure):
        result["failed_check"] = str(error)
    if type(error) is RoomStoreOperationsError:
        message = str(error)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,159}", message):
            result["failed_check"] = message
        frame = error.__traceback__
        room_store_frame = None
        while frame is not None:
            if Path(frame.tb_frame.f_code.co_filename).name == "room_store_operations.py":
                room_store_frame = frame
            frame = frame.tb_next
        if room_store_frame is not None:
            site = room_store_frame.tb_frame.f_code.co_name
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", site):
                result["error_site"] = site
            result["error_line"] = room_store_frame.tb_lineno
    return result


class FixtureObjectStore:
    """In-memory ObjectStore contract for controller tests; never a MinIO server."""

    real_minio = False

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.controls: dict[str, tuple[bytes, str]] = {}
        self._catalog: bytes | None = None
        self._catalog_etag: str | None = None
        self._revision = 0

    @staticmethod
    def _ref(key: str, body: bytes) -> ObjectRef:
        return ObjectRef(key, hashlib.sha256(body).hexdigest(), len(body))

    def put_bytes(self, key: str, body: bytes) -> ObjectRef:
        ref = self._ref(key, body)
        if key != f"objects/sha256/{ref.sha256}":
            raise ValueError("fixture object key does not match its digest")
        prior = self.objects.get(key)
        if prior is not None and prior != body:
            raise ValueError("fixture immutable object key collision")
        self.objects[key] = body
        return ref

    def put_file(self, key: str, path: Path) -> ObjectRef:
        return self.put_bytes(key, Path(path).read_bytes())

    def get_bytes(self, key: str, expected_digest=None, expected_size=None) -> bytes:
        body = self.objects[key]
        if expected_digest is not None and hashlib.sha256(body).hexdigest() != expected_digest:
            raise ValueError("fixture object digest mismatch")
        if expected_size is not None and len(body) != expected_size:
            raise ValueError("fixture object size mismatch")
        return body

    def download_file(self, key, destination, expected_digest, expected_size) -> None:
        body = self.get_bytes(key, expected_digest, expected_size)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    def read_catalog(self):
        return self._catalog, self._catalog_etag

    def conditional_catalog_put(self, body: bytes, expected_etag):
        if expected_etag != self._catalog_etag:
            error = RuntimeError("fixture catalog etag conflict")
            error.published = False
            raise error
        self._revision += 1
        self._catalog = bytes(body)
        self._catalog_etag = f"fixture-{self._revision}-{hashlib.sha256(body).hexdigest()}"
        return self._catalog_etag

    def read_control(self, key: str, max_bytes: int):
        if key not in self.controls:
            return None, None
        body, etag = self.controls[key]
        if len(body) > max_bytes:
            raise ValueError("fixture control exceeds size limit")
        return body, etag

    def create_control(self, key: str, body: bytes):
        if key in self.controls:
            error = RuntimeError("fixture control already exists")
            error.published = False
            raise error
        etag = hashlib.sha256(body).hexdigest()
        self.controls[key] = (bytes(body), etag)
        return etag

    def replace_control(self, key: str, body: bytes, expected_etag: str):
        current = self.controls.get(key)
        if current is None or current[1] != expected_etag:
            error = RuntimeError("fixture control etag conflict")
            error.published = False
            raise error
        etag = hashlib.sha256(body).hexdigest()
        self.controls[key] = (bytes(body), etag)
        return etag

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


def verify_hostile_inventory_entries() -> dict[str, str]:
    """Exercise the native controller's portable snapshot-entry guards."""
    root = SnapshotEntry(".", "dir", 0, 0o755, None)
    cases = {
        "casefold-collision": [
            root,
            SnapshotEntry("Data.txt", "file", 1, 0o644, None),
            SnapshotEntry("data.TXT", "file", 1, 0o644, None),
        ],
        "unicode-normalization-collision": [
            root,
            SnapshotEntry("é.txt", "file", 1, 0o644, None),
            SnapshotEntry("e\u0301.txt", "file", 1, 0o644, None),
        ],
        "windows-reserved-name": [
            root,
            SnapshotEntry("CON.txt", "file", 1, 0o644, None),
        ],
        "special-file": [root, SnapshotEntry("pipe", "fifo", 0, 0o600, None)],
        "unsafe-symlink": [
            root,
            SnapshotEntry("outside", "symlink", 0, 0o777, "C:/private/file"),
        ],
    }
    rejected: dict[str, str] = {}
    for name, rows in cases.items():
        try:
            _validate_snapshot_entries(rows)
        except RoomStoreOperationsError:
            rejected[name] = "rejected"
        else:
            raise AcceptanceFailure(f"hostile-{name}-inventory-accepted")
    return rejected


def wait_for_native_clock_advance(after_filetime: int, timeout_ns: int = 5_000_000_000) -> bool:
    """Wait for a real FILETIME advance; a delay alone is never success."""
    from josh_room.windows_file_metadata import system_time_100ns

    deadline = time.monotonic_ns() + timeout_ns
    while system_time_100ns() <= after_filetime:
        if time.monotonic_ns() >= deadline:
            return False
        time.sleep(0)
    return True


class _CountingRestic:
    def __init__(self, store, calls: dict[str, int]) -> None:
        self._store = store
        self._calls = calls

    def __enter__(self):
        self._store.__enter__()
        return self

    def __exit__(self, *args):
        return self._store.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._store, name)

    def backup(self, *args, **kwargs):
        self._calls["backup"] += 1
        return self._store.backup(*args, **kwargs)


def _source_inventory(root: Path) -> dict[str, tuple[bytes, str]]:
    inventory = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".josh-room.json":
            inventory[path.relative_to(root).as_posix()] = (
                path.read_bytes(), _portable_mode(path.stat().st_mode)
            )
    return inventory


def _portable_mode(mode: int, platform: str | None = None) -> str:
    value = stat.S_IMODE(mode)
    if (platform or os.name) == "nt":
        return "writable" if value & 0o222 else "read-only"
    return f"{value:04o}"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceFailure(message)


def run_acceptance(
    *,
    restic_executable: Path,
    operational_identity: Path,
    recovery_identity: Path,
    jat_root: Path,
    output: Path,
    object_store_factory: Callable[[], FixtureObjectStore] = FixtureObjectStore,
) -> dict[str, Any]:
    """Run Room Store controller paths with real Windows Restic/age and fixture storage."""
    if os.name != "nt" or not sys.platform.startswith("win"):
        raise AcceptanceFailure("native-windows-required")
    _require(
        os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1",
        "managed-controller-runtime-required",
    )

    from unittest.mock import patch

    from josh_room import crypto
    from josh_room.config import DimensionConfig
    from josh_room.encryption_domain import EncryptionKeyset, EncryptionMaterial
    from josh_room.private_paths import protect_private_directory
    from josh_room.room_store_bridge import (
        ExistingRoomStoreContext,
        _build_operations,
        _latest_descriptor,
    )
    from josh_room.room_store_operations import scan_workspace_for_status
    from josh_room.workspace_state import write_stat_workspace_marker

    restic_executable = Path(restic_executable).resolve(strict=True)
    operational_identity = Path(operational_identity).resolve(strict=True)
    recovery_identity = Path(recovery_identity).resolve(strict=True)
    jat_root = Path(jat_root).resolve(strict=True)
    output = Path(output).resolve()
    try:
        version_result = subprocess.run(
            [str(restic_executable), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("managed Windows Restic could not run") from None
    version_line = version_result.stdout.strip().splitlines()[0] if version_result.stdout.strip() else ""
    _require(version_result.returncode == 0 and version_line.startswith("restic 0.19.1"), "managed Windows Restic version did not match")
    store = object_store_factory()
    room_id = "windows-room-store-fixture"
    endpoint = "http://127.0.0.1:9000"
    bucket = "room-store-fixture"
    operational_text = next(
        (
            line.strip()
            for line in operational_identity.read_text(encoding="utf-8").splitlines()
            if line.startswith("AGE-SECRET-KEY-")
        ),
        None,
    )
    _require(operational_text is not None, "synthetic age identity is invalid")
    operational_recipient = crypto.derive_recipient(operational_identity)
    recovery_recipient = crypto.derive_recipient(recovery_identity)
    domain_keyset = EncryptionKeyset.create(
        provider="minio",
        endpoint=endpoint,
        bucket=bucket,
        operational_identity=operational_text,
        operational_recipient=operational_recipient,
        recovery_recipients=[recovery_recipient],
    ).upgrade_for_room_store()
    store.keyset = domain_keyset
    material = EncryptionMaterial(domain_keyset, operational_identity)
    dimension = DimensionConfig(
        dimension_id="windows-fixture-dimension",
        display_name="Windows fixture only",
        provider="minio",
        endpoint=endpoint,
        bucket=bucket,
        credential_profile="windows-fixture-profile",
        encryption_domain_id=material.encryption_domain_id,
    )

    with tempfile.TemporaryDirectory(prefix="josh-room-windows-room-store-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        instance = root / "instance"
        repository = root / "restic-repository"
        cache_dir = root / "restic-cache"
        runtime_dir = root / "private-runtime"
        for directory in (root, workspace, instance, cache_dir, runtime_dir):
            directory.mkdir(parents=True, exist_ok=True)
            protect_private_directory(directory)

        (workspace / "source.txt").write_bytes(b"synthetic Windows Room Store source\n")
        payload = workspace / "payload.bin"
        original_payload = secrets.token_bytes(8 * 1024 * 1024)
        payload.write_bytes(original_payload)
        delete_target = workspace / "delete-me.txt"
        delete_target.write_bytes(b"synthetic deletion fixture\n")
        readonly = workspace / "readonly.txt"
        readonly.write_bytes(b"mode-preservation fixture\n")
        readonly.chmod(0o444)
        readonly_mode = _portable_mode(readonly.stat().st_mode)
        _require(readonly_mode == "read-only", "Windows read-only mode could not be represented")
        store_calls = {"backup": 0}

        def ensure_keyset(selected_dimension, selected_backend):
            keyset = selected_backend.keyset
            _require(keyset.provider == selected_dimension.provider, "fixture-keyset-provider-mismatch")
            _require(keyset.binding == keyset.room_store.physical_binding, "fixture-keyset-binding-mismatch")
            return keyset

        def bound_keyset(
            selected_dimension,
            selected_backend,
            repository_id,
            *,
            expected_generation,
        ):
            keyset = selected_backend.keyset
            if keyset.room_store.generation != expected_generation:
                raise ValueError("fixture keyset generation changed")
            if keyset.room_store.repository_id not in {None, repository_id}:
                raise ValueError("fixture repository binding changed")
            metadata = replace(
                keyset.room_store,
                repository_id=repository_id,
                generation=expected_generation + 1,
            )
            selected_backend.keyset = replace(
                keyset, room_store=metadata
            )
            return selected_backend.keyset

        def read_keyset(selected_dimension, selected_backend):
            _require(selected_backend.keyset.encryption_domain_id == selected_dimension.encryption_domain_id, "fixture-keyset-domain-mismatch")
            return selected_backend.keyset, "fixture-keyset-etag"

        def lookup_room_store_secret(domain_id, generation):
            metadata = store.keyset.room_store
            _require(domain_id == store.keyset.encryption_domain_id, "fixture-secret-domain-mismatch")
            _require(generation == metadata.generation, "fixture-secret-generation-mismatch")
            return metadata.secret

        def store_factory(**kwargs):
            from josh_room.room_store_bridge import _restic_store_factory

            real = _restic_store_factory(
                **kwargs,
                provider_env={},
                ca_bundle=None,
                executable=restic_executable,
            )
            return _CountingRestic(real, store_calls)

        with (
            patch("josh_room.room_store_bridge._repository_locator", lambda _dimension: str(repository)),
            patch("josh_room.room_store_bridge._cache_directory", lambda *_args, **_kwargs: cache_dir),
            patch("josh_room.room_store_bridge._provider_environment", lambda _dimension: {}),
            patch("josh_room.room_store_bridge.auth.ensure_room_store_keyset", ensure_keyset),
            patch("josh_room.room_store_bridge.auth.bind_room_store_repository", bound_keyset),
            patch("josh_room.room_store_bridge.auth._read_keyset_record_from_backend", read_keyset),
            patch("josh_room.room_store_bridge.keyring.lookup_room_store_secret", lookup_room_store_secret),
        ):
            operations, state, _display_name = _build_operations(
                instance=instance,
                dimension=dimension,
                project_id=room_id,
                workspace=workspace,
                material=material,
                components=[],
                display_name=room_id,
                backend=store,
                runtime_dir=runtime_dir,
                executable=restic_executable,
                jat_root=jat_root,
            )
            operations.store_factory = store_factory

            initial = operations.save()
            _require(initial.status == "saved" and initial.descriptor is not None, "initial Room Store Save failed")
            initial_snapshot = initial.descriptor.to_dict()["workspace"]["snapshot_id"]
            initial_catalog_etag = state["etag"]
            backups_after_initial = store_calls["backup"]

            noop = operations.save()
            _require(noop.status == "already-saved", "unchanged Room Store Save did not take the native Windows no-op path")
            _require(store_calls["backup"] == backups_after_initial, "no-op Save reached Restic backup")
            _require(state["etag"] == initial_catalog_etag, "no-op Save published a new catalog revision")
            _require(noop.descriptor is not None, "no-op Save omitted the existing logical recovery point")
            noop_body = noop.descriptor.to_dict()
            initial_body = initial.descriptor.to_dict()
            _require(
                noop_body["logical_jat_id"] == initial_body["logical_jat_id"],
                "no-op Save changed the logical recovery point",
            )
            _require(
                noop_body["workspace"]["snapshot_id"] == initial_snapshot,
                "no-op Save changed the Restic snapshot",
            )

            before_stat = payload.stat()
            from josh_room.windows_file_metadata import change_time_ns

            before_change = change_time_ns(payload, before_stat)
            _require(wait_for_native_clock_advance(before_change // 100), "Windows FILETIME did not advance after the baseline")
            edited_payload = bytearray(original_payload)
            edited_payload[1024 * 1024 : 1024 * 1024 + 16] = b"changed-bytes-16"
            payload.write_bytes(edited_payload)
            os.utime(payload, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
            after_stat = payload.stat()
            after_change = change_time_ns(payload, after_stat)
            _require(after_stat.st_size == before_stat.st_size, "synthetic content edit changed file size")
            _require(after_stat.st_mtime_ns == before_stat.st_mtime_ns, "synthetic content edit did not restore mtime")
            _require(after_change != before_change, "native ChangeTime did not detect the content edit")
            edit = operations.save()
            _require(edit.status == "saved" and edit.descriptor is not None, "same-size edited Room Store Save failed")
            _require(edit.descriptor.to_dict()["workspace"]["snapshot_id"] != initial_snapshot, "same-size edit did not publish a new Restic snapshot")
            edit_workspace = edit.descriptor.to_dict()["workspace"]
            _require(edit_workspace["parent_snapshot_id"] == initial_snapshot, "incremental edit Save did not use the initial Restic parent")
            _require(edit_workspace["data_added_packed"] < 4 * 1024 * 1024, "same-size edit added an unbounded amount of packed data")

            renamed = workspace / "renamed-payload.bin"
            payload.rename(renamed)
            rename = operations.save()
            _require(rename.status == "saved" and rename.descriptor is not None, "Room Store rename Save failed")
            rename_workspace = rename.descriptor.to_dict()["workspace"]
            _require(rename_workspace["parent_snapshot_id"] == edit_workspace["snapshot_id"], "rename did not use the latest Restic parent")
            _require(rename_workspace["data_added_packed"] < 1024 * 1024, "rename failed to reuse stored content")

            delete_target.unlink()
            delete_preview = operations.preview()
            _require("delete-me.txt" in delete_preview.deleted_paths, "Room Store deletion preview omitted the deleted path")
            delete_options = {}
            if delete_preview.deletion_confirmation_token is not None:
                delete_options["deletion_confirmation_token"] = delete_preview.deletion_confirmation_token
            delete_result = operations.save(**delete_options)
            _require(delete_result.status == "saved" and delete_result.descriptor is not None, "Room Store delete Save failed")
            final_descriptor = delete_result.descriptor
            final_body = final_descriptor.to_dict()
            _require(final_body["workspace"]["snapshot_id"] != rename_workspace["snapshot_id"], "delete did not publish a new recovery point")
            _require(final_body["workspace"]["parent_snapshot_id"] == rename_workspace["snapshot_id"], "delete Save did not use the latest Restic parent")
            _require(store_calls["backup"] == 4, "Room Store Save reached Restic an unexpected number of times")

            restored_room = root / "room-store-enter"

            def write_restore_marker(stage: Path, target: Path, descriptor) -> None:
                scan = scan_workspace_for_status(stage)
                write_stat_workspace_marker(
                    stage,
                    dimension_id=dimension.dimension_id,
                    project_id=room_id,
                    snapshot_id=descriptor.to_dict()["logical_jat_id"],
                    encryption_domain_id=material.encryption_domain_id,
                    display_name=room_id,
                    workspace_signature=scan.signature,
                    signature_algorithm=scan.signature_algorithm,
                    capture_policy_sha256=scan.capture_policy_sha256,
                    path_binding=target,
                )

            room_restore = operations.restore(
                final_descriptor,
                restored_room,
                write_restore_marker=write_restore_marker,
            )
            _require(room_restore.destination == restored_room, "Room Store Enter promoted the wrong destination")
            _require((restored_room / "renamed-payload.bin").read_bytes() == bytes(edited_payload), "Room Store Enter restored edited payload bytes incorrectly")
            _require(not (restored_room / "delete-me.txt").exists(), "Room Store Enter restored a deleted file")
            _require(_portable_mode((restored_room / "readonly.txt").stat().st_mode) == readonly_mode, "Room Store Enter changed the read-only mode")
            _require(_source_inventory(restored_room) == _source_inventory(workspace), "Room Store Enter changed user file bytes or modes")

            hostile = verify_hostile_inventory_entries()
            keyset, password_file, restic_store = operations._open_store()
            try:
                with restic_store as opened:
                    repository_info = opened.open_existing()
                    catalog, catalog_etag, descriptor = _latest_descriptor(
                        store, instance, dimension, material, room_id, runtime_dir
                    )
                    record = catalog.resolve_snapshot(room_id, "latest")
                    context = ExistingRoomStoreContext(
                        instance=instance,
                        backend=store,
                        catalog=catalog,
                        catalog_etag=catalog_etag,
                        store=opened,
                        private_dir=runtime_dir,
                        material=material,
                        dimension=dimension,
                        project_id=room_id,
                        repository_info=repository_info,
                        selected_record=record,
                        selected_descriptor=descriptor,
                        physical_binding=keyset.room_store.physical_binding,
                    )
                    from josh_room import cli

                    @contextmanager
                    def selected_material_context(_args, _instance):
                        yield material

                    @contextmanager
                    def selected_room_store_context(_args, _instance, **_kwargs):
                        yield context

                    captured_stdout = io.StringIO()
                    with (
                        patch.object(cli, "_uses_minio_encryption", lambda _args: True),
                        patch.object(cli, "_selected_encryption_environment", selected_material_context),
                        patch.object(cli, "_effective_dimension", lambda _args: dimension),
                        patch.object(cli, "_open_room_store_context", selected_room_store_context),
                        patch.object(cli, "_write_runtime_result", lambda _result: None),
                        redirect_stdout(captured_stdout),
                    ):
                        cli_exit = cli.main([
                            "snapshot", "export", room_id,
                            "--backend", "minio",
                            "--dimension", dimension.dimension_id,
                            "--snapshot", "latest",
                            "--output", str(output),
                            "--json",
                        ])
                    try:
                        portable = json.loads(captured_stdout.getvalue())
                    except json.JSONDecodeError:
                        raise RuntimeError("native Room Store export returned invalid JSON") from None
                    _require(cli_exit == 0 and portable.get("ok") is True, "native Room Store export CLI failed")
            finally:
                password_file.unlink(missing_ok=True)

            clean_room = root / "portable-clean-room"
            from josh_room.jat import run_restore

            restore_result = run_restore(jat_root, output, clean_room)
            _require(
                restore_result.get("operation") == "restore"
                and restore_result.get("success") is True
                and restore_result.get("exit_status") == 0,
                "portable JAT clean-room Restore failed",
            )
            restored_payload = Path(restore_result.get("payload_path") or "")
            payload_path_matches = restored_payload.resolve() == clean_room.resolve()
            _require(payload_path_matches, "portable JAT Restore returned a different payload destination")
            clean_workspace = restored_payload / "workspace"
            expected_inventory = _source_inventory(restored_room)
            restored_inventory = _source_inventory(clean_workspace)
            _require(restored_inventory == expected_inventory, "portable JAT clean-room Restore changed file bytes or modes")

            outside_target = root / "outside-workspace-target.txt"
            outside_target.write_bytes(b"synthetic external symlink target\n")
            workspace_link = workspace / "outside-link.txt"
            try:
                workspace_link.symlink_to(outside_target)
            except (OSError, NotImplementedError):
                raise AcceptanceFailure("native-workspace-symlink-creation-unavailable") from None
            published_before_symlink = state["etag"]
            backups_before_symlink = store_calls["backup"]
            try:
                operations.save()
            except RoomStoreOperationsError as error:
                _require("unsafe symlink" in str(error), "workspace symlink failed for an unexpected reason")
            else:
                raise AcceptanceFailure("native-workspace-symlink-was-accepted")
            finally:
                workspace_link.unlink(missing_ok=True)
            _require(state["etag"] == published_before_symlink, "workspace symlink failure changed the catalog")
            _require(store_calls["backup"] == backups_before_symlink, "workspace symlink reached Restic backup")

            return {
                "status": "passed",
                "platform": "win32-x64",
                "storage": {
                    "kind": FIXTURE_STORE_KIND,
                    "provider_contract": "MinIO Dimension shape with in-memory ObjectStore fixture",
                    "real_minio": store.real_minio,
                    "hostile_inventory_source": "synthetic Restic snapshot rows passed through the Room Store validator",
                },
                "unproved": [
                    "real MinIO provider semantics",
                    "R2 provider semantics",
                    "Windows maximum path-length policy",
                ],
                "encryption": {"engine": "age", "operational_and_recovery_recipients": 2},
                "restic": {"engine": "restic", "version": version_line.split()[1], "repository_format": 2},
                "checks": {
                    "initial_save": initial.status,
                    "unchanged_noop": noop.status,
                    "no_op_skipped_backup": True,
                    "same_size_edit_mtime_restored": True,
                    "incremental_edit_save": edit.status,
                    "rename_reuse": rename.status,
                    "delete_save": delete_result.status,
                    "room_store_enter": "passed",
                    "casefold_collision": hostile["casefold-collision"],
                    "unicode_normalization_collision": hostile["unicode-normalization-collision"],
                    "windows_reserved_name": hostile["windows-reserved-name"],
                    "special_file": hostile["special-file"],
                    "unsafe_symlink": hostile["unsafe-symlink"],
                    "native_workspace_symlink": "rejected",
                    "portable_export": portable["status"],
                    "portable_clean_room_restore": "passed",
                    "portable_restore_payload_path_matches": payload_path_matches,
                    "restore_bytes_and_modes": "passed",
                },
                "metrics": {
                    "native_save_backup_calls": store_calls["backup"],
                    "initial_snapshot_id": initial_snapshot,
                    "edit_snapshot_id": edit_workspace["snapshot_id"],
                    "rename_snapshot_id": rename_workspace["snapshot_id"],
                    "delete_snapshot_id": final_body["workspace"]["snapshot_id"],
                    "edit_data_added_packed": edit_workspace["data_added_packed"],
                    "rename_data_added_packed": rename_workspace["data_added_packed"],
                    "portable_size": portable["output_size"],
                    "portable_sha256": portable["output_sha256"],
                    "workspace_entry_count": portable["workspace_entry_count"],
                    "restored_readonly_mode": readonly_mode,
                },
            }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restic", type=Path, required=True)
    parser.add_argument("--operational-identity", type=Path, required=True)
    parser.add_argument("--recovery-identity", type=Path, required=True)
    parser.add_argument("--jat-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_acceptance(
            restic_executable=args.restic,
            operational_identity=args.operational_identity,
            recovery_identity=args.recovery_identity,
            jat_root=args.jat_root,
            output=args.output,
        )
    except Exception as error:  # noqa: BLE001 - keep native runtime paths out of receipts.
        result = _failure_receipt(error)
        status = 1
    else:
        status = 0
    encoded = json.dumps(result, sort_keys=True)
    result_file = os.environ.get("JOSH_ROOM_RESULT_FILE")
    if result_file:
        target = Path(result_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
