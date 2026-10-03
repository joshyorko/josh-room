from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import auth, crypto, keyring
from josh_room import room_store_bridge as bridge
from josh_room.encryption_domain import EncryptionKeyset, EncryptionMaterial
from josh_room.local_store import ObjectRef
from josh_room.private_paths import protect_private_directory
from josh_room.restic_store import ResticStore


class FakeS3:
    def __init__(self, keyset: EncryptionKeyset, dimension):
        self.config = SimpleNamespace(
            endpoint=dimension.endpoint,
            bucket=dimension.bucket,
            catalog_key=dimension.catalog_key,
            dimension_id=dimension.dimension_id,
            verify_tls=dimension.option("verify_tls", True),
            ca_bundle=dimension.option("ca_bundle"),
        )
        self.control = {auth.KEYSET_CONTROL_KEY: keyset.to_json()}
        self.control_etag = '"control-1"'
        self.catalog = None
        self.catalog_etag = None
        self.objects = {}
        self.calls = []

    def read_control(self, key, _max_bytes):
        self.calls.append(("read_control", key))
        body = self.control.get(key)
        return body, self.control_etag if body is not None else None

    def replace_control(self, key, body, expected_etag):
        self.calls.append(("replace_control", key, expected_etag))
        if expected_etag != self.control_etag:
            raise RuntimeError("synthetic control conflict")
        self.control[key] = body.encode() if isinstance(body, str) else body
        self.control_etag = f'"control-{int(self.control_etag.split("-")[1].strip(chr(34))) + 1}"'
        return self.control_etag

    def read_catalog(self):
        self.calls.append(("read_catalog",))
        return self.catalog, self.catalog_etag

    def conditional_catalog_put(self, body, expected_etag):
        self.calls.append(("conditional_catalog_put", expected_etag))
        if expected_etag != self.catalog_etag:
            from josh_room.r2 import R2Conflict

            raise R2Conflict("synthetic catalog conflict")
        self.catalog = bytes(body)
        self.catalog_etag = f'"catalog-{len([call for call in self.calls if call[0] == "conditional_catalog_put"])}"'
        return self.catalog_etag

    def put_file(self, key, path):
        self.calls.append(("put_file", key))
        body = Path(path).read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if key != f"objects/sha256/{digest}":
            raise ValueError("object key mismatch")
        if key in self.objects and self.objects[key] != body:
            raise ValueError("immutable object conflict")
        self.objects[key] = body
        return ObjectRef(key, digest, len(body))

    def download_file(self, key, destination, expected_digest, expected_size):
        self.calls.append(("download_file", key))
        body = self.objects[key]
        if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_digest:
            raise ValueError("synthetic object integrity failure")
        Path(destination).write_bytes(body)


def _age_identity(tmp_path: Path, name: str) -> tuple[Path, str, str]:
    executable = crypto._managed_executable("age-keygen")
    completed = subprocess.run([str(executable)], capture_output=True, check=True)
    identity = tmp_path / name
    identity.write_bytes(completed.stdout)
    identity.chmod(0o600)
    identity_value = next(
        line for line in completed.stdout.decode().splitlines() if line.startswith("AGE-SECRET-KEY-")
    )
    return identity, identity_value, crypto.derive_recipient(identity)


def _fixture(tmp_path: Path, monkeypatch):
    operational_identity, operational_secret, operational_recipient = _age_identity(tmp_path, "operational.age")
    recovery_identity, _recovery_secret, recovery_recipient = _age_identity(tmp_path, "recovery.age")
    domain_id = str(uuid.uuid4())
    endpoint = "https://minio.example.test:9443"
    bucket = "synthetic-room-store"
    dimension = bridge.DimensionConfig(
        dimension_id="minio-main",
        display_name="Synthetic MinIO",
        provider="minio",
        endpoint=endpoint,
        bucket=bucket,
        credential_profile="synthetic-profile",
        encryption_domain_id=domain_id,
        options=(("verify_tls", True),),
    )
    v1_keyset = EncryptionKeyset.create(
        "minio",
        endpoint,
        bucket,
        operational_secret,
        operational_recipient,
        [recovery_recipient],
        encryption_domain_id=domain_id,
    )
    material = EncryptionMaterial(v1_keyset, operational_identity)
    backend = FakeS3(v1_keyset, dimension)
    monkeypatch.setattr(bridge, "_create_backend", lambda _dimension, _instance: backend)
    monkeypatch.setattr(bridge.keyring, "lookup", lambda _profile, **_kwargs: {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
        "session-token": "synthetic-temporary-token",
    })
    monkeypatch.setattr(auth, "store_room_store_secret", lambda *_args: None)

    def room_secret(_domain_id, _generation):
        return EncryptionKeyset.from_json(backend.control[auth.KEYSET_CONTROL_KEY]).room_store.secret

    monkeypatch.setattr(keyring, "lookup_room_store_secret", room_secret)
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "josh-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    local_repository = tmp_path / "restic-repository"
    restic_executable = os.environ.get("JOSH_ROOM_TEST_RESTIC") or shutil.which("restic")
    if restic_executable is None:
        pytest.skip("restic is unavailable")

    provider_environments = []

    def local_restic_store(*, repository, cache_dir, password_file, provider_env=None, ca_bundle=None, executable):
        provider_environments.append(dict(provider_env or {}))
        return ResticStore(
            repository=local_repository,
            cache_dir=cache_dir,
            password_file=password_file,
            provider_env=provider_env,
            executable=str(executable),
        )

    monkeypatch.setattr(bridge, "_restic_store_factory", local_restic_store)
    backend.provider_environments = provider_environments
    monkeypatch.setattr(
        bridge,
        "_verified_restic_executable",
        lambda *, runtime_root, install: Path(restic_executable),
    )
    return dimension, material, backend, operational_identity, recovery_identity


@pytest.mark.integration
def test_fake_s3_real_restic_and_age_save_noop_incremental_hydrate(tmp_path, monkeypatch):
    try:
        crypto._managed_executable("age")
        crypto._managed_executable("age-keygen")
    except crypto.CryptoError:
        pytest.skip("managed age runtime is unavailable")
    dimension, material, backend, _identity, _recovery = _fixture(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "notes.txt"
    source.write_text("first small capture\n", encoding="utf-8")
    (workspace / "robot.yaml").write_text("tasks: []\n", encoding="utf-8")
    rcc_captures = []

    def capture_rcc_component(
        *, workspace, prior_component, repository_id, repository_format, restic, cancellation, rcc_runtime
    ):
        if not (workspace / "robot.yaml").exists():
            rcc_captures.append("cleared")
            return None
        if prior_component is not None:
            rcc_captures.append("reused")
            return copy.deepcopy(prior_component)
        stage = tmp_path / "synthetic-rcca"
        stage.mkdir()
        (stage / "rcc-environment.rcca").write_bytes(b"synthetic rcca archive")
        summary = restic.backup(stage, parent=None, cancellation=cancellation)
        snapshot = restic.snapshot(summary.snapshot_id)
        archive = (stage / "rcc-environment.rcca").read_bytes()
        rcc_captures.append("captured")
        return {
            "kind": "rcca",
            "snapshot": {
                "repository_id": repository_id,
                "repository_format": repository_format,
                "snapshot_id": snapshot.snapshot_id,
                "tree_id": snapshot.tree_id,
            },
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "archive_size": len(archive),
            "member_basename": "rcc-environment.rcca",
            "artifact_digest": "sha256:" + "1" * 64,
            "specification_digest": "sha256:" + "2" * 64,
            "platform": "linux-x64",
            "rcc_version": "v18.19.5",
            "robot_relative_path": "robot.yaml",
            "source_input_sha256": bridge.source_input_sha256(workspace),
        }

    monkeypatch.setattr(bridge, "capture_rcc_component", capture_rcc_component)

    try:
        first = bridge.save_room_store(
            tmp_path / "instance",
            dimension,
            "room-synthetic",
            workspace,
            material,
            components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
        )
    except bridge.RoomStoreBridgeError as error:
        raise AssertionError(error.result) from error
    calls_before_preview = list(backend.calls)
    object_writes_before_preview = len([call for call in calls_before_preview if call[0] == "put_file"])
    catalog_writes_before_preview = len([call for call in calls_before_preview if call[0] == "conditional_catalog_put"])
    preview = bridge.preview_room_store(
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
        components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
    )
    assert preview["deleted_paths"] == []
    assert preview["restic_data_added_bytes"] is None
    assert preview["rcc_capture_pending"] is False
    preview_actions = [call[0] for call in backend.calls[len(calls_before_preview):]]
    assert set(preview_actions) <= {"read_catalog", "download_file", "read_control"}
    assert len([call for call in backend.calls if call[0] == "put_file"]) == object_writes_before_preview
    assert len([call for call in backend.calls if call[0] == "conditional_catalog_put"]) == catalog_writes_before_preview
    unchanged = bridge.save_room_store(
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
        components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
    )
    source.write_text("second capture with changed bytes\n", encoding="utf-8")
    second = bridge.save_room_store(
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
        components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
    )
    restore_parent = tmp_path / "restore-targets"
    restore_parent.mkdir()
    destination = restore_parent / "restored"
    try:
        restored = bridge.hydrate_room_store(
            tmp_path / "instance",
            dimension,
            "room-synthetic",
            destination,
            material,
            snapshot_id=first["snapshot_id"],
        )
    except bridge.RoomStoreBridgeError as error:
        raise AssertionError(error.result) from error

    assert first["status"] == "saved"
    assert unchanged["status"] == "already-saved"
    assert unchanged["data_added_bytes"] == 0
    assert second["status"] == "saved"
    assert second["workspace_parent_snapshot_id"] == first["workspace_snapshot_id"]
    assert rcc_captures == ["captured", "reused", "reused"]
    assert restored["snapshot_id"] == first["snapshot_id"]
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "first small capture\n"
    assert all(environment["AWS_SESSION_TOKEN"] == "synthetic-temporary-token" for environment in backend.provider_environments)
    assert len([call for call in backend.calls if call[0] == "put_file"]) == 2
    assert len([call for call in backend.calls if call[0] == "conditional_catalog_put"]) == 2
    assert backend.calls.index(next(call for call in backend.calls if call[0] == "put_file")) < backend.calls.index(
        next(call for call in backend.calls if call[0] == "conditional_catalog_put")
    )
    marker = json.loads((destination / ".josh-room.json").read_text(encoding="utf-8"))
    assert marker["format_version"] == 3
    assert marker["snapshot_id"] == first["snapshot_id"]
    assert all("synthetic-secret" not in repr(call) for call in backend.calls)

    (workspace / "robot.yaml").unlink()
    cleared = bridge.save_room_store(
        tmp_path / "instance",
        dimension,
        "room-synthetic",
        workspace,
        material,
        components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
    )
    catalog, _etag = bridge._read_catalog(backend, tmp_path / "instance", dimension, material)
    with bridge._private_operation_directory() as private_dir:
        latest = bridge._load_descriptor(
            backend,
            tmp_path / "instance",
            material,
            "room-synthetic",
            catalog.latest("room-synthetic"),
            private_dir,
        )
    assert cleared["status"] == "saved"
    assert latest.to_dict()["components"]["rcc_environment"] is None
    assert rcc_captures == ["captured", "reused", "reused", "cleared"]


def test_repository_locator_and_cache_scope_are_physical_binding_scoped(tmp_path):
    dimension = bridge.DimensionConfig(
        dimension_id="alias-a",
        display_name="Alias A",
        provider="minio",
        endpoint="https://minio.example.test:9443",
        bucket="synthetic-room-store",
        credential_profile="synthetic-profile",
        encryption_domain_id=str(uuid.uuid4()),
        options=(("verify_tls", True),),
    )
    alias = bridge.DimensionConfig(
        dimension_id="alias-b",
        display_name="Alias B",
        provider="minio",
        endpoint=dimension.endpoint,
        bucket=dimension.bucket,
        credential_profile="other-profile",
        encryption_domain_id=dimension.encryption_domain_id,
        options=dimension.options,
    )

    assert bridge._repository_locator(dimension) == "s3:https://minio.example.test:9443/synthetic-room-store/room-store/v1"
    assert bridge._cache_directory(dimension, tmp_path) == bridge._cache_directory(alias, tmp_path)


def test_requested_uncaptured_component_fails_before_provider_or_runtime_work(tmp_path, monkeypatch):
    dimension = bridge.DimensionConfig(
        dimension_id="minio-main",
        display_name="Synthetic MinIO",
        provider="minio",
        endpoint="https://minio.example.test:9443",
        bucket="synthetic-room-store",
        credential_profile="synthetic-profile",
        encryption_domain_id=str(uuid.uuid4()),
        options=(("verify_tls", True),),
    )
    material = SimpleNamespace(
        keyset=SimpleNamespace(
            binding=bridge.physical_bucket_identity("minio", dimension.endpoint, dimension.bucket),
            recovery_recipients=("age1synthetic",),
        ),
        encryption_domain_id=dimension.encryption_domain_id,
        recipient="age1synthetic-operational",
    )
    monkeypatch.setattr(bridge, "_create_backend", lambda *_args: pytest.fail("must fail before provider access"))

    with pytest.raises(bridge.RoomStoreBridgeError) as failure:
        bridge.save_room_store(
            tmp_path / "instance",
            dimension,
            "room-synthetic",
            tmp_path,
            material,
            components=[],
            required_components=("hauler_content",),
        )

    assert failure.value.code == "components-unsupported"


def test_restic_runtime_handoff_rejects_arbitrary_path_and_preview_never_installs(tmp_path, monkeypatch):
    private_runtime = tmp_path / "private-runtime"
    monkeypatch.setenv("JOSH_ROOM_RESTIC_EXE", str(tmp_path / "untrusted-restic"))

    with pytest.raises(bridge.RoomStoreBridgeError) as invalid:
        bridge._verified_restic_executable(runtime_root=private_runtime, install=True)
    assert invalid.value.code == "restic-runtime-invalid"
    assert not private_runtime.exists()

    monkeypatch.delenv("JOSH_ROOM_RESTIC_EXE")
    private_runtime.mkdir(mode=0o700)
    protect_private_directory(private_runtime)
    with pytest.raises(bridge.RoomStoreBridgeError) as missing:
        bridge._verified_restic_executable(runtime_root=private_runtime, install=False)
    assert missing.value.code == "restic-runtime-unavailable"
    assert list(private_runtime.iterdir()) == []
