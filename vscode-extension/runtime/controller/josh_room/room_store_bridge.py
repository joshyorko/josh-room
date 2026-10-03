"""Concrete MinIO, age, v3-catalog, and Restic bridge for Room Store JATs."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import auth, keyring
from .catalog import corroborate_logical_snapshot
from .config import DimensionConfig, config_dir
from .crypto import decrypt_file, encrypt
from .encryption_domain import (
    ROOM_STORE_PREFIX,
    EncryptionMaterial,
    physical_bucket_identity,
    validate_minio_transport,
)
from .logical_jat import MAX_LOGICAL_JAT_BYTES, LogicalJat
from .minio import MinioBackend, MinioConfig, validate_bucket_name
from .operations import _display_name, _encrypt_catalog, _read_remote_catalog
from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    protect_private_file,
    secure_private_file,
    validate_private_directory,
    verify_private_path,
)
from .restic_store import ResticStore, SnapshotEntry
from .room_store_components import (
    RCC_VERSION,
    RoomStoreComponentError,
    _host_platform,
    capture_rcc_component,
    source_input_sha256,
)
from .room_store_operations import (
    RoomStoreOperations,
    RoomStoreOperationsError,
    RoomStorePublicationError,
    SavePreview,
    SaveResult,
    scan_workspace_for_status,
)
from .workspace_state import write_stat_workspace_marker

_COMPONENTS = {"rcc_environment", "homebrew_recovery", "hauler_content"}


class RoomStoreBridgeError(RuntimeError):
    """Public-safe error and optional deletion-confirmation receipt."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "room-store-failed",
        confirmation_token: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.confirmation_token = confirmation_token
        self.result = {"ok": False, "error": code}
        if result:
            self.result.update(result)
        if confirmation_token is not None:
            self.result.update(
                {
                    "requires_confirmation": True,
                    "confirmation_token": confirmation_token,
                }
            )
        super().__init__(message)


class _CatalogPublicationError(RuntimeError):
    def __init__(self, published: bool | None):
        self.published = published
        super().__init__("Room Store catalog publication failed")


def _scope(dimension: DimensionConfig, material: EncryptionMaterial) -> str:
    if not isinstance(dimension, DimensionConfig) or dimension.provider != "minio":
        raise RoomStoreBridgeError("Room Store currently requires a MinIO Dimension", code="provider-unsupported")
    if not isinstance(material, EncryptionMaterial):
        raise RoomStoreBridgeError("selected Dimension encryption material is required", code="material-required")
    if dimension.encryption_domain_id and dimension.encryption_domain_id != material.encryption_domain_id:
        raise RoomStoreBridgeError("selected material does not match the Dimension encryption domain", code="domain-mismatch")
    try:
        validate_bucket_name(dimension.bucket)
        validate_minio_transport(
            dimension.endpoint,
            verify_tls=dimension.option("verify_tls", True),
            ca_bundle=dimension.option("ca_bundle"),
        )
        if physical_bucket_identity("minio", dimension.endpoint, dimension.bucket) != material.keyset.binding:
            raise ValueError("physical binding mismatch")
    except (TypeError, ValueError) as error:
        raise RoomStoreBridgeError("selected MinIO storage binding is invalid", code="storage-binding-invalid") from error
    return material.encryption_domain_id


def _repository_locator(dimension: DimensionConfig) -> str:
    if dimension.provider != "minio":
        raise RoomStoreBridgeError("Room Store currently requires a MinIO Dimension", code="provider-unsupported")
    try:
        validate_bucket_name(dimension.bucket)
        validate_minio_transport(
            dimension.endpoint,
            verify_tls=dimension.option("verify_tls", True),
            ca_bundle=dimension.option("ca_bundle"),
        )
        endpoint = urlsplit(dimension.endpoint)
        if endpoint.query or endpoint.fragment:
            raise ValueError("endpoint query or fragment is unsupported")
        base = dimension.endpoint.rstrip("/")
        return f"s3:{base}/{dimension.bucket}/{ROOM_STORE_PREFIX}"
    except (TypeError, ValueError) as error:
        raise RoomStoreBridgeError("MinIO repository locator is invalid", code="repository-locator-invalid") from error


def _cache_directory(dimension: DimensionConfig, cache_root: Path | None = None) -> Path:
    if cache_root is None:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "josh-room" / "cache"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "josh-room"
    else:
        base = Path(cache_root)
    binding = physical_bucket_identity("minio", dimension.endpoint, dimension.bucket)
    suffix = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return base / "room-store" / suffix


@contextmanager
def _private_operation_directory():
    root = config_dir() / "room-store-runtime"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_directory(root)
    except (OSError, PrivatePathError) as error:
        raise RoomStoreBridgeError("private Room Store runtime directory is unavailable", code="private-runtime-unavailable") from error
    with tempfile.TemporaryDirectory(prefix="operation-", dir=root) as path:
        private = Path(path)
        try:
            protect_private_directory(private)
        except PrivatePathError as error:
            raise RoomStoreBridgeError("private Room Store operation directory is unsafe", code="private-runtime-unsafe") from error
        yield private


def _private_cache(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        protect_private_directory(directory)
    except (OSError, PrivatePathError) as error:
        raise RoomStoreBridgeError("private Restic cache is unavailable", code="cache-unavailable") from error


def _create_backend(dimension: DimensionConfig, _instance: Path) -> MinioBackend:
    _repository_locator(dimension)
    try:
        return MinioBackend(MinioConfig.from_dimension(dimension))
    except Exception:  # noqa: BLE001 - sanitize SDK, TLS, and credential diagnostics at this boundary.
        raise RoomStoreBridgeError("MinIO storage connection is unavailable", code="provider-unavailable") from None


def _provider_environment(dimension: DimensionConfig) -> dict[str, str]:
    try:
        credentials = keyring.lookup(dimension.credential_profile, allow_runtime=False)
    except Exception:  # noqa: BLE001 - native keyring errors may contain backend details.
        raise RoomStoreBridgeError("MinIO credential profile is unavailable", code="credentials-unavailable") from None
    values = {
        "AWS_ACCESS_KEY_ID": credentials.get("access-key-id"),
        "AWS_SECRET_ACCESS_KEY": credentials.get("secret-access-key"),
        "AWS_DEFAULT_REGION": dimension.region,
    }
    if credentials.get("session-token"):
        values["AWS_SESSION_TOKEN"] = credentials["session-token"]
    if not values["AWS_ACCESS_KEY_ID"] or not values["AWS_SECRET_ACCESS_KEY"]:
        raise RoomStoreBridgeError("MinIO credential profile is incomplete", code="credentials-unavailable")
    return {key: value for key, value in values.items() if value}


def _restic_store_factory(
    *,
    repository: str,
    cache_dir: Path,
    password_file: Path,
    provider_env: Mapping[str, str],
    ca_bundle: str | None,
    executable: Path,
) -> ResticStore:
    return ResticStore(
        repository=repository,
        cache_dir=cache_dir,
        password_file=password_file,
        provider_env=provider_env,
        ca_bundle=ca_bundle,
        executable=str(executable),
    )


def _managed_runtime_assets() -> tuple[Path, Path]:
    module = Path(__file__).resolve()
    candidates = (
        (module.parents[2] / "restic-manifest.json", module.parents[1] / "install_restic.py"),
        (
            module.parents[2] / "vscode-extension" / "runtime" / "restic-manifest.json",
            module.parents[2] / "scripts" / "install_restic.py",
        ),
    )
    for manifest, installer in candidates:
        if manifest.is_file() and installer.is_file():
            return manifest, installer
    raise RoomStoreBridgeError("verified Restic runtime assets are unavailable", code="restic-runtime-unavailable")


def _runtime_platform() -> str:
    if os.name == "nt" and __import__("sys").maxsize > 2**32:
        return "win32-x64"
    if os.name == "posix" and __import__("sys").platform.startswith("linux") and __import__("sys").maxsize > 2**32:
        return "linux-x64"
    raise RoomStoreBridgeError("managed Restic runtime platform is unsupported", code="platform-unsupported")


def _verified_restic_executable(*, runtime_root: Path, install: bool) -> Path:
    """Return only a version/checksum-verified binary beneath its private runtime root."""
    manifest, installer_path = _managed_runtime_assets()
    platform = _runtime_platform()
    hinted = os.environ.get("JOSH_ROOM_RESTIC_EXE")
    runtime_root = Path(runtime_root).expanduser()
    runtime_value = os.environ.get("JOSH_ROOM_RESTIC_RUNTIME")
    if runtime_value and Path(runtime_value).expanduser().resolve() != runtime_root.resolve():
        raise RoomStoreBridgeError("Restic runtime handoff is outside the selected instance", code="restic-runtime-invalid")
    binary_name = "restic.exe" if platform == "win32-x64" else "restic"
    expected_path = runtime_root / "restic" / "0.19.1" / platform / binary_name
    if hinted and Path(hinted).expanduser().resolve() != expected_path.resolve():
        raise RoomStoreBridgeError("Restic runtime handoff does not match its private runtime", code="restic-runtime-invalid")
    if runtime_root.exists() or runtime_root.is_symlink():
        try:
            verify_private_path(runtime_root, directory=True)
        except PrivatePathError as error:
            raise RoomStoreBridgeError("Restic runtime root is unsafe", code="restic-runtime-invalid") from error
    elif not install:
        raise RoomStoreBridgeError("verified Restic runtime has not been prepared", code="restic-runtime-unavailable")
    else:
        runtime_root.mkdir(parents=True, mode=0o700)
        protect_private_directory(runtime_root)
    if not install and (
        not expected_path.is_file()
        or not expected_path.with_name(binary_name + ".sha256").is_file()
    ):
        raise RoomStoreBridgeError("verified Restic runtime has not been prepared", code="restic-runtime-unavailable")
    runtime_dirs = (
        runtime_root / "restic",
        runtime_root / "restic" / "0.19.1",
        runtime_root / "restic" / "0.19.1" / platform,
    )
    try:
        for directory in runtime_dirs:
            if install:
                directory.mkdir(mode=0o700, exist_ok=True)
                protect_private_directory(directory)
            else:
                verify_private_path(directory, directory=True)
    except (OSError, PrivatePathError) as error:
        raise RoomStoreBridgeError("Restic runtime path is unsafe", code="restic-runtime-invalid") from error
    spec = importlib.util.spec_from_file_location("_josh_room_install_restic", installer_path)
    if spec is None or spec.loader is None:
        raise RoomStoreBridgeError("Restic runtime installer is unavailable", code="restic-runtime-unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.install_restic(manifest, runtime_root, platform)
        executable = Path(result["executable"]).resolve(strict=True)
    except Exception:  # noqa: BLE001 - installer diagnostics may include private runtime paths.
        raise RoomStoreBridgeError("verified Restic runtime could not be prepared", code="restic-runtime-invalid") from None
    expected = (runtime_root / "restic" / "0.19.1" / platform / ("restic.exe" if platform == "win32-x64" else "restic")).resolve(strict=True)
    digest_marker = expected.with_name(expected.name + ".sha256")
    try:
        if install:
            protect_private_file(digest_marker)
        else:
            verify_private_path(digest_marker, directory=False)
        binary_info = expected.lstat()
        if not stat.S_ISREG(binary_info.st_mode) or stat.S_ISLNK(binary_info.st_mode):
            raise ValueError("managed binary is not a regular file")
        if os.name == "nt":
            protect_private_file(expected)
        elif binary_info.st_uid != os.getuid() or binary_info.st_mode & 0o077 or not os.access(expected, os.X_OK):
            raise ValueError("managed binary permissions are unsafe")
    except (OSError, PrivatePathError, ValueError) as error:
        raise RoomStoreBridgeError("verified Restic binary path is unsafe", code="restic-runtime-invalid") from error
    if executable != expected or (hinted and Path(hinted).expanduser().resolve(strict=True) != expected):
        raise RoomStoreBridgeError("Restic runtime handoff does not match its verified pin", code="restic-runtime-invalid")
    return executable


def _components(value: Mapping[str, Any] | Sequence[Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        if set(value) != _COMPONENTS:
            raise RoomStoreBridgeError("component references must contain the three named slots", code="components-invalid")
        result = deepcopy(dict(value))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 0:
        result = {name: None for name in _COMPONENTS}
    else:
        raise RoomStoreBridgeError(
            "native component references are required; automatic JAT/Hauler capture is not available",
            code="components-required",
        )
    if any(component is not None and not isinstance(component, dict) for component in result.values()):
        raise RoomStoreBridgeError("component references are invalid", code="components-invalid")
    return result


def _check_required_components(components: Mapping[str, Any], required: Sequence[str]) -> None:
    if any(name not in _COMPONENTS for name in required):
        raise RoomStoreBridgeError("required component selection is invalid", code="components-invalid")
    unresolved = [name for name in required if name != "rcc_environment" and components[name] is None]
    if unresolved:
        raise RoomStoreBridgeError(
            "selected native component capture is unavailable",
            code="components-unsupported",
        )


def _load_descriptor(
    backend,
    instance: Path,
    material: EncryptionMaterial,
    project_id: str,
    index_record: dict,
    private_dir: Path,
) -> LogicalJat:
    if index_record.get("payload_kind") != "room-store-v1":
        raise RoomStoreBridgeError("selected recovery point is a portable JAT, not a logical Room Store JAT", code="legacy-snapshot")
    encrypted_path = private_dir / "logical-jat.age"
    plain_path = private_dir / "logical-jat.json"
    try:
        backend.download_file(
            index_record["object_key"],
            encrypted_path,
            index_record["ciphertext_sha256"],
            index_record["ciphertext_size"],
        )
        decrypt_file(encrypted_path, [material.identity], plain_path, MAX_LOGICAL_JAT_BYTES)
        descriptor = LogicalJat.from_json(plain_path.read_bytes())
        corroborate_logical_snapshot(project_id, index_record, descriptor)
        return descriptor
    except RoomStoreBridgeError:
        raise
    except Exception:  # noqa: BLE001 - age/S3/parser errors can contain private paths.
        raise RoomStoreBridgeError("logical JAT descriptor could not be verified", code="descriptor-invalid") from None
    finally:
        encrypted_path.unlink(missing_ok=True)
        plain_path.unlink(missing_ok=True)


def _read_catalog(backend, instance: Path, dimension: DimensionConfig, material: EncryptionMaterial):
    try:
        return _read_remote_catalog(
            backend,
            material.identity,
            instance,
            dimension.dimension_id,
            material.encryption_domain_id,
        )
    except Exception:  # noqa: BLE001 - age and provider diagnostics are not public-safe.
        raise RoomStoreBridgeError("encrypted Room catalog is unavailable", code="catalog-unavailable") from None


def _latest_descriptor(backend, instance: Path, dimension, material, project_id, private_dir):
    catalog, etag = _read_catalog(backend, instance, dimension, material)
    project = catalog.body["projects"].get(project_id)
    if project is None or project.get("latest") is None:
        return catalog, etag, None
    record = catalog.latest(project_id)
    if record.get("payload_kind") != "room-store-v1":
        return catalog, etag, None
    return catalog, etag, _load_descriptor(backend, instance, material, project_id, record, private_dir)


def _read_only_snapshot_entries(
    *,
    dimension,
    material,
    backend,
    repository,
    cache_dir,
    runtime_dir,
    provider_env,
    executable,
    snapshot_id,
) -> list[SnapshotEntry]:
    try:
        keyset, _etag = auth._read_keyset_record_from_backend(dimension, backend)
        if keyset is None or keyset.room_store is None or keyset.room_store.repository_id is None:
            raise ValueError("Room Store repository is not bound")
        if keyset.encryption_domain_id != material.encryption_domain_id:
            raise ValueError("Room Store domain mismatch")
        secret = keyring.lookup_room_store_secret(
            keyset.encryption_domain_id,
            keyset.room_store.generation,
        )
        password_file = runtime_dir / "preview-password"
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            secure_private_file(descriptor, password_file)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(secret.encode("ascii") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        store = _restic_store_factory(
            repository=repository,
            cache_dir=cache_dir,
            password_file=password_file,
            provider_env=provider_env,
            ca_bundle=dimension.option("ca_bundle"),
            executable=executable,
        )
        with store as opened:
            repository_info = opened.open_existing()
            if repository_info.repository_id != keyset.room_store.repository_id:
                raise ValueError("Room Store repository binding mismatch")
            return list(opened.entries(snapshot_id))
    except Exception:  # noqa: BLE001 - keep backend/path diagnostics out of Preview results.
        raise RoomStoreBridgeError("read-only Restic preview is unavailable", code="preview-unavailable") from None
    finally:
        (runtime_dir / "preview-password").unlink(missing_ok=True)


def _build_operations(
    *,
    instance: Path,
    dimension: DimensionConfig,
    project_id: str,
    workspace: Path,
    material: EncryptionMaterial,
    components,
    display_name: str | None,
    backend,
    runtime_dir: Path,
    executable: Path,
    rcc_runtime: Path | str | None = None,
    required_components: Sequence[str] = (),
    cancellation=None,
    active_runtime_root: Path | None = None,
):
    domain_id = _scope(dimension, material)
    repository = _repository_locator(dimension)
    cache_dir = _cache_directory(dimension)
    _private_cache(cache_dir)
    provider_env = _provider_environment(dimension)
    component_value = _components(components)
    _check_required_components(component_value, required_components)
    recipients = [material.recipient, *material.keyset.recovery_recipients]
    if len(set(recipients)) < 2:
        raise RoomStoreBridgeError("two distinct age recipients are required", code="recipients-unavailable")
    metadata = {
        "dimension_id": dimension.dimension_id,
        "encryption_domain_id": domain_id,
        "room_id": project_id,
        "components": component_value,
        "source": {},
        "producer": {
            "josh_room_version": _room_version(),
            "restic_version": "0.19.1",
            "source_platform": _source_platform(),
            "restore_platforms": [_source_platform()],
        },
    }
    state: dict[str, Any] = {
        "catalog": None,
        "etag": None,
        "object_ref": None,
        "display_name": None,
        "latest_descriptor": None,
    }

    def read_latest():
        catalog, etag, descriptor = _latest_descriptor(
            backend,
            instance,
            dimension,
            material,
            project_id,
            runtime_dir,
        )
        state["catalog"] = catalog
        state["etag"] = etag
        state["latest_descriptor"] = descriptor
        project = catalog.body["projects"].get(project_id)
        state["display_name"] = (
            display_name
            or (project["display_name"] if project else None)
            or _display_name(project_id)
        )
        return descriptor, etag

    def read_snapshot_entries(snapshot_id: str):
        return _read_only_snapshot_entries(
            dimension=dimension,
            material=material,
            backend=backend,
            repository=repository,
            cache_dir=cache_dir,
            runtime_dir=runtime_dir,
            provider_env=provider_env,
            executable=executable,
            snapshot_id=snapshot_id,
        )

    def publish_descriptor(
        descriptor: LogicalJat,
        *,
        expected_etag: str | None,
        workspace_signature: str,
        signature_algorithm: str,
    ) -> None:
        if signature_algorithm != "josh-room-stat-v1":
            raise _CatalogPublicationError(False)
        if state["catalog"] is None or state["etag"] != expected_etag:
            raise _CatalogPublicationError(False)
        ciphertext_path = runtime_dir / "logical-jat.age"
        try:
            encrypt(descriptor.to_json().encode("utf-8"), recipients, ciphertext_path)
            ciphertext_digest = _digest_file(ciphertext_path)
            object_ref = backend.put_file(f"objects/sha256/{ciphertext_digest}", ciphertext_path)
            if object_ref.sha256 != ciphertext_digest or object_ref.size != ciphertext_path.stat().st_size:
                raise ValueError("logical descriptor object metadata mismatch")
        except Exception:  # noqa: BLE001 - provider errors may contain endpoint or request details.
            ciphertext_path.unlink(missing_ok=True)
            raise _CatalogPublicationError(False) from None
        finally:
            ciphertext_path.unlink(missing_ok=True)
        try:
            candidate = state["catalog"].add_logical_snapshot(
                project_id,
                display_name or state["display_name"] or _display_name(project_id),
                descriptor,
                object_ref,
                workspace_signature,
            )
            encrypted_catalog = _encrypt_catalog(candidate, recipients, instance)
        except Exception:  # noqa: BLE001 - catalog/age errors can include local paths.
            raise _CatalogPublicationError(False) from None
        try:
            etag = backend.conditional_catalog_put(encrypted_catalog, expected_etag)
        except Exception as error:  # noqa: BLE001 - preserve tri-state outcome and redact diagnostics.
            raise _CatalogPublicationError(getattr(error, "published", None)) from None
        state.update(catalog=candidate, etag=etag, object_ref=object_ref)

    def resolve_components(opened_store, latest_descriptor):
        prior_components = (
            latest_descriptor.to_dict()["components"]
            if latest_descriptor is not None
            else {name: None for name in _COMPONENTS}
        )
        prior_rcc = prior_components["rcc_environment"]
        if (workspace / "robot.yaml").exists() or prior_rcc is not None:
            try:
                rcc_component = capture_rcc_component(
                    workspace=workspace,
                    prior_component=prior_rcc,
                    repository_id=opened_store.repository_info.repository_id,
                    repository_format=opened_store.repository_info.repository_format,
                    restic=opened_store,
                    cancellation=cancellation,
                    rcc_runtime=rcc_runtime,
                )
            except RoomStoreComponentError as error:
                raise RoomStoreOperationsError(str(error)) from None
        else:
            rcc_component = component_value["rcc_environment"]
        resolved = {**component_value, "rcc_environment": rcc_component}
        missing = [name for name in required_components if resolved[name] is None]
        if missing:
            raise RoomStoreOperationsError("selected native component capture is unavailable")
        return resolved

    def write_marker(
        descriptor,
        *,
        clean,
        workspace_signature,
        signature_algorithm,
        capture_policy_sha256,
    ):
        write_stat_workspace_marker(
            workspace,
            dimension_id=dimension.dimension_id,
            encryption_domain_id=domain_id,
            project_id=project_id,
            snapshot_id=descriptor.to_dict()["logical_jat_id"],
            display_name=state["display_name"] or display_name or _display_name(project_id),
            workspace_signature=workspace_signature,
            signature_algorithm=signature_algorithm,
            capture_policy_sha256=capture_policy_sha256,
            path_binding=workspace,
        )

    def ensure_keyset(selected_dimension, selected_backend):
        keyset = auth.ensure_room_store_keyset(selected_dimension, selected_backend)
        if keyset.encryption_domain_id != domain_id:
            raise RoomStoreBridgeError("Room Store keyset belongs to another encryption domain", code="domain-mismatch")
        return keyset

    def bind_repository(selected_dimension, selected_backend, repository_id, *, expected_generation):
        keyset = auth.bind_room_store_repository(
            selected_dimension,
            selected_backend,
            repository_id,
            expected_generation=expected_generation,
        )
        if keyset.encryption_domain_id != domain_id:
            raise RoomStoreBridgeError("Room Store keyset belongs to another encryption domain", code="domain-mismatch")
        return keyset

    def store_factory(*, repository, cache_dir, password_file):
        return _restic_store_factory(
            repository=repository,
            cache_dir=cache_dir,
            password_file=password_file,
            provider_env=provider_env,
            ca_bundle=dimension.option("ca_bundle"),
            executable=executable,
        )

    operations = RoomStoreOperations(
        workspace=workspace,
        repository=repository,
        cache_dir=cache_dir,
        password_dir=runtime_dir,
        store_factory=store_factory,
        dimension=dimension,
        backend=backend,
        ensure_keyset=ensure_keyset,
        bind_repository=bind_repository,
        read_latest=read_latest,
        read_snapshot_entries=read_snapshot_entries,
        publish_descriptor=publish_descriptor,
        write_marker=write_marker,
        descriptor_metadata=metadata,
        resolve_components=resolve_components,
        active_runtime_root=active_runtime_root,
        secure_private_file=secure_private_file,
        validate_private_directory=validate_private_directory,
    )
    return operations, state, display_name or _display_name(project_id)


def _room_version() -> str:

    try:
        return version("josh-room")
    except PackageNotFoundError:
        raise RoomStoreBridgeError("Josh Room package version is unavailable", code="runtime-version-unavailable") from None


def _source_platform() -> str:
    if os.name == "nt":
        return "win32-x64"
    if os.name == "posix" and os.uname().machine in {"x86_64", "amd64"}:
        return "linux-x64"
    raise RoomStoreBridgeError("Room Store source platform is unsupported", code="platform-unsupported")


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _operation_inputs(instance, dimension, project_id, source, material):
    if not isinstance(project_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project_id) is None:
        raise RoomStoreBridgeError("Room ID is invalid", code="room-invalid")
    domain_id = _scope(dimension, material)
    workspace = Path(source)
    if not workspace.is_dir():
        raise RoomStoreBridgeError("workspace directory is unavailable", code="workspace-unavailable")
    backend = _create_backend(dimension, Path(instance))
    return workspace, domain_id, backend


def _preview_output(preview: SavePreview, *, rcc_capture_pending: bool = False) -> dict:
    return {
        "ok": True,
        "scanned_bytes": preview.scanned_bytes,
        "restic_data_added_bytes": preview.restic_added_bytes,
        "previous_entry_count": preview.previous_entry_count,
        "current_entry_count": preview.current_entry_count,
        "deleted_paths": list(preview.deleted_paths),
        "deletion_confirmation_token": preview.deletion_confirmation_token,
        "workspace_signature": preview.workspace_signature,
        "signature_algorithm": preview.signature_algorithm,
        "capture_policy_sha256": preview.capture_policy_sha256,
        "rcc_capture_pending": rcc_capture_pending,
    }


def _rcc_capture_pending(workspace: Path, parent: LogicalJat | None) -> bool:
    if not (workspace / "robot.yaml").exists():
        return False
    if parent is None:
        return True
    prior = parent.to_dict()["components"]["rcc_environment"]
    if prior is None:
        return True
    try:
        current_input = source_input_sha256(workspace)
    except (RoomStoreComponentError, OSError, ValueError):
        return True
    return not (
        prior.get("source_input_sha256") == current_input
        and prior.get("rcc_version") == RCC_VERSION
        and prior.get("platform") == _host_platform()
    )


def preview_room_store(
    instance: Path,
    dimension: DimensionConfig,
    project_id: str,
    source: Path,
    selected_material: EncryptionMaterial,
    *,
    snapshot_id: str = "latest",
    components: Mapping[str, Any] | Sequence[Any],
    rcc_runtime: Path | str | None = None,
    required_components: Sequence[str] = (),
) -> dict:
    component_value = _components(components)
    _check_required_components(component_value, required_components)
    workspace, _domain_id, backend = _operation_inputs(instance, dimension, project_id, source, selected_material)
    try:
        active_runtime = Path(os.environ["ROBOCORP_HOME"]) if os.environ.get("ROBOCORP_HOME") else None
        runtime_root = Path(instance).parent / "josh-room-runtime"
        executable = _verified_restic_executable(runtime_root=runtime_root, install=False)
        with _private_operation_directory() as runtime_dir:
            operations, state, _display = _build_operations(
                instance=Path(instance),
                dimension=dimension,
                project_id=project_id,
                workspace=workspace,
                material=selected_material,
                components=component_value,
                display_name=None,
                backend=backend,
                runtime_dir=runtime_dir,
                executable=executable,
                rcc_runtime=rcc_runtime,
                required_components=required_components,
                active_runtime_root=active_runtime if active_runtime and active_runtime.is_dir() else None,
            )
            if snapshot_id == "latest":
                preview = operations.preview()
                latest = state["latest_descriptor"]
                return _preview_output(preview, rcc_capture_pending=_rcc_capture_pending(workspace, latest))
            else:
                catalog, _catalog_etag = _read_catalog(backend, Path(instance), dimension, selected_material)
                try:
                    record = catalog.resolve_snapshot(project_id, snapshot_id)
                except ValueError:
                    raise RoomStoreBridgeError("selected logical JAT is unavailable", code="snapshot-unavailable") from None
                parent = _load_descriptor(backend, Path(instance), selected_material, project_id, record, runtime_dir)
            return _preview_output(
                operations.preview(parent),
                rcc_capture_pending=_rcc_capture_pending(workspace, parent),
            )
    except RoomStoreOperationsError as error:
        raise RoomStoreBridgeError(str(error), code="preview-invalid") from None


def save_room_store(
    instance: Path,
    dimension: DimensionConfig,
    project_id: str,
    source: Path,
    selected_material: EncryptionMaterial,
    *,
    components: Mapping[str, Any] | Sequence[Any],
    display_name: str | None = None,
    confirmation_token: str | None = None,
    on_progress=None,
    cancellation=None,
    rcc_runtime: Path | str | None = None,
    required_components: Sequence[str] = (),
) -> dict:
    component_value = _components(components)
    _check_required_components(component_value, required_components)
    workspace, _domain_id, backend = _operation_inputs(instance, dimension, project_id, source, selected_material)
    active_runtime = Path(os.environ["ROBOCORP_HOME"]) if os.environ.get("ROBOCORP_HOME") else None
    try:
        runtime_root = Path(instance).parent / "josh-room-runtime"
        executable = _verified_restic_executable(runtime_root=runtime_root, install=True)
        with _private_operation_directory() as runtime_dir:
            operations, state, resolved_display_name = _build_operations(
                instance=Path(instance),
                dimension=dimension,
                project_id=project_id,
                workspace=workspace,
                material=selected_material,
                components=component_value,
                display_name=display_name,
                backend=backend,
                runtime_dir=runtime_dir,
                executable=executable,
                rcc_runtime=rcc_runtime,
                required_components=required_components,
                cancellation=cancellation,
                active_runtime_root=active_runtime if active_runtime and active_runtime.is_dir() else None,
            )
            result: SaveResult = operations.save(
                deletion_confirmation_token=confirmation_token,
                on_progress=on_progress,
                cancellation=cancellation,
            )
            descriptor = result.descriptor
            logical_id = descriptor.to_dict()["logical_jat_id"] if descriptor else None
            output = {
                "ok": True,
                "status": result.status,
                "dimension_id": dimension.dimension_id,
                "encryption_domain_id": selected_material.encryption_domain_id,
                "project_id": project_id,
                "snapshot_id": logical_id,
                "workspace_snapshot_id": result.snapshot_id,
                "workspace_parent_snapshot_id": (
                    descriptor.to_dict()["workspace"].get("parent_snapshot_id") if descriptor else None
                ),
                "data_added_bytes": result.data_added_bytes,
                "scanned_bytes": result.scanned_bytes,
                "workspace_signature": result.workspace_signature,
                "signature_algorithm": result.signature_algorithm,
                "capture_policy_sha256": result.capture_policy_sha256,
                "display_name": resolved_display_name,
            }
            object_ref = state.get("object_ref")
            if object_ref is not None:
                output["object_key"] = object_ref.key
                output["ciphertext_sha256"] = object_ref.sha256
                output["ciphertext_size"] = object_ref.size
            return output
    except RoomStoreBridgeError:
        raise
    except RoomStorePublicationError as error:
        raise RoomStoreBridgeError(
            "Room Store catalog publication needs reconciliation",
            code="catalog-publication-" + error.publication_state,
            result=error.result,
        ) from None
    except RoomStoreOperationsError as error:
        if error.deletion_confirmation_token:
            raise RoomStoreBridgeError(
                "suspicious workspace deletions need confirmation",
                code="deletion-confirmation-required",
                confirmation_token=error.deletion_confirmation_token,
            ) from None
        raise RoomStoreBridgeError(str(error), code="save-failed") from None
    except Exception:  # noqa: BLE001 - public Save errors must not expose SDK, identity, or path diagnostics.
        raise RoomStoreBridgeError("logical Room Store Save failed", code="save-failed") from None


def hydrate_room_store(
    instance: Path,
    dimension: DimensionConfig,
    project_id: str,
    destination: Path,
    selected_material: EncryptionMaterial,
    *,
    snapshot_id: str = "latest",
) -> dict:
    _scope(dimension, selected_material)
    backend = _create_backend(dimension, Path(instance))
    try:
        runtime_root = Path(instance).parent / "josh-room-runtime"
        executable = _verified_restic_executable(runtime_root=runtime_root, install=False)
        with _private_operation_directory() as runtime_dir:
            catalog, _etag = _read_catalog(backend, Path(instance), dimension, selected_material)
            try:
                record = catalog.resolve_snapshot(project_id, snapshot_id)
            except ValueError:
                raise RoomStoreBridgeError("selected recovery point is unavailable", code="snapshot-unavailable") from None
            descriptor = _load_descriptor(backend, Path(instance), selected_material, project_id, record, runtime_dir)
            project_name = catalog.body["projects"][project_id]["display_name"]
            repository = _repository_locator(dimension)
            cache_dir = _cache_directory(dimension)
            _private_cache(cache_dir)
            provider_env = _provider_environment(dimension)
            def unused_save_marker(*_args, **_kwargs):
                raise RoomStoreBridgeError("Save marker callback is unavailable during restore", code="restore-callback-invalid")

            # Restore consumes the existing repository; the unused save callbacks
            # are intentionally closed over the selected catalog revision.
            operations = RoomStoreOperations(
                workspace=Path(destination).parent,
                repository=repository,
                cache_dir=cache_dir,
                password_dir=runtime_dir,
                store_factory=lambda **kwargs: _restic_store_factory(
                    **kwargs,
                    provider_env=provider_env,
                    ca_bundle=dimension.option("ca_bundle"),
                    executable=executable,
                ),
                dimension=dimension,
                backend=backend,
                read_latest=lambda: (descriptor, catalog.body["revision"]),
                publish_descriptor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    _CatalogPublicationError(False)
                ),
                write_marker=unused_save_marker,
                descriptor_metadata={
                    "dimension_id": dimension.dimension_id,
                    "encryption_domain_id": selected_material.encryption_domain_id,
                    "room_id": project_id,
                    "components": descriptor.to_dict()["components"],
                    "source": descriptor.to_dict()["source"],
                    "producer": descriptor.to_dict()["producer"],
                },
                secure_private_file=secure_private_file,
                validate_private_directory=validate_private_directory,
            )
            restored = operations.restore(descriptor, Path(destination))
            scan = scan_workspace_for_status(Path(destination))
            write_stat_workspace_marker(
                Path(destination),
                dimension_id=dimension.dimension_id,
                encryption_domain_id=selected_material.encryption_domain_id,
                project_id=project_id,
                snapshot_id=descriptor.to_dict()["logical_jat_id"],
                display_name=project_name,
                workspace_signature=scan.signature,
                signature_algorithm=scan.signature_algorithm,
                capture_policy_sha256=scan.capture_policy_sha256,
                path_binding=Path(destination),
            )
            return {
                "ok": True,
                "dimension_id": dimension.dimension_id,
                "encryption_domain_id": selected_material.encryption_domain_id,
                "project_id": project_id,
                "snapshot_id": descriptor.to_dict()["logical_jat_id"],
                "workspace_snapshot_id": descriptor.to_dict()["workspace"]["snapshot_id"],
                "destination": str(restored.destination),
                "workspace_signature": scan.signature,
                "signature_algorithm": scan.signature_algorithm,
                "capture_policy_sha256": scan.capture_policy_sha256,
                "display_name": project_name,
            }
    except RoomStoreBridgeError:
        raise
    except RoomStoreOperationsError as error:
        raise RoomStoreBridgeError(str(error), code="restore-failed") from None
    except Exception as error:  # noqa: BLE001 - suppress provider, identity, and filesystem diagnostics.
        frame = error.__traceback__
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        raise RoomStoreBridgeError(
            "logical JAT restore failed",
            code="restore-failed",
            result={
                "error_type": type(error).__name__,
                **({"missing_attribute": error.name} if isinstance(error, AttributeError) and isinstance(error.name, str) else {}),
                **({"error_site": frame.tb_frame.f_code.co_name, "error_line": frame.tb_lineno} if frame else {}),
            },
        ) from None
