"""Verified materialization of logical Room Store recovery points."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from . import jat
from .cancellation import CLICancelled
from .logical_jat import LogicalJat
from .private_paths import (
    PrivatePathError,
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)
from .restic_store import ResticStore, ResticStoreError
from .room_store_operations import (
    RoomStoreOperationsError,
    _scan_workspace,
    _validate_snapshot_entries,
)
from .workspace_policy import load_capture_policy

_COMPONENT_FILES = {
    "rcc_environment": ("rcc-environment.rcca", "metadata.json"),
    "homebrew_recovery": ("homebrew-recovery.tar.zst",),
    "hauler_content": ("hauler-content.tar.zst", "metadata.json"),
}
_CHUNK = 1024 * 1024


class PortableExportError(RuntimeError):
    """Path-free failure while materializing a portable JAT."""


@dataclass(frozen=True, slots=True)
class PortableExportResult:
    logical_jat_id: str
    status: str
    output_size: int
    output_sha256: str
    workspace_entry_count: int
    verified_components: tuple[str, ...]
    estimated_required_bytes: int
    available_bytes: int


@dataclass(slots=True)
class MaterializedComponents:
    """Verified component files kept alive until the caller closes this lease."""

    rcc_archive: Path | None
    rcc_metadata: Path | None
    jat_rcc_metadata: Path | None
    brew_archive: Path | None
    hauler_archive: Path | None
    manifest: dict[str, dict[str, Any]]
    _owned_stage: tempfile.TemporaryDirectory | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._owned_stage is not None:
            self._owned_stage.cleanup()
            self._owned_stage = None

    def __enter__(self) -> Self:
        if self._owned_stage is None:
            raise PortableExportError("component materialization is already closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _check_cancelled(cancellation: Any) -> None:
    if cancellation is None:
        return
    cancelled = getattr(cancellation, "cancelled", False)
    if callable(cancelled):
        cancelled = cancelled()
    if cancelled:
        raise CLICancelled


def _digest(path: Path) -> tuple[int, str]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise PortableExportError("materialized component is not a regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _check_component_snapshot(
    restic: ResticStore, component: dict[str, Any], name: str
) -> list[Any]:
    identity = component["snapshot"]
    snapshot_id = identity["snapshot_id"]
    try:
        snapshot = restic.snapshot(snapshot_id)
        if snapshot.tree_id != identity["tree_id"]:
            raise PortableExportError("component snapshot tree identity does not match")
        rows = list(restic.entries(snapshot_id))
        entries = _validate_snapshot_entries(rows)
    except PortableExportError:
        raise
    except RoomStoreOperationsError:
        raise PortableExportError(
            "component snapshot contains an unsafe or unexpected path"
        ) from None
    except (OSError, ResticStoreError, ValueError, TypeError):
        raise PortableExportError("component snapshot could not be verified") from None
    expected = set(_COMPONENT_FILES[name])
    if set(entries) != {".", *expected} or any(
        row.entry_type != "file" for path, row in entries.items() if path != "."
    ):
        raise PortableExportError(
            "component snapshot contains an unsafe or unexpected path"
        )
    return rows


def _verify_component_archive(root: Path, component: dict[str, Any], name: str) -> Path:
    archive = root / component["member_basename"]
    try:
        size, digest = _digest(archive)
    except (OSError, PortableExportError):
        raise PortableExportError("component archive is unavailable") from None
    if size != component["archive_size"] or digest != component["archive_sha256"]:
        raise PortableExportError("component archive identity does not match")
    return archive


def _rcc_metadata(
    component: dict[str, Any], archive: Path, metadata_source: Path, destination: Path
) -> Path:
    try:
        metadata_stat = metadata_source.lstat()
        if (
            not stat.S_ISREG(metadata_stat.st_mode)
            or metadata_stat.st_size > 1024 * 1024
        ):
            raise ValueError
        value = json.loads(metadata_source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise PortableExportError("RCC component metadata is invalid") from None
    required = {
        "artifact_digest": component["artifact_digest"],
        "specification_digest": component["specification_digest"],
        "platform": component["platform"],
        "rcc_version": component["rcc_version"],
        "archive_sha256": component["archive_sha256"],
        "archive_size": component["archive_size"],
        "robot_relative_path": component["robot_relative_path"],
    }
    if not isinstance(value, dict) or any(
        value.get(key) != expected for key, expected in required.items()
    ):
        raise PortableExportError("RCC component metadata identity does not match")
    legacy_key = value.get("legacy_blueprint_key")
    if not isinstance(legacy_key, str) or not legacy_key or len(legacy_key) > 1024:
        raise PortableExportError("RCC component metadata identity is incomplete")
    body = {
        "artifact": component["artifact_digest"],
        "specification_digest": component["specification_digest"],
        "legacy_blueprint_key": legacy_key,
        "archive": archive.name,
        "archive_sha256": component["archive_sha256"],
        "archive_size": component["archive_size"],
        "rcc_version": component["rcc_version"],
        "platform": component["platform"],
        "robot": component["robot_relative_path"],
        "provider": "local",
        "acquired": False,
    }
    destination.write_text(
        json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    destination.chmod(0o600)
    return destination


def _verify_hauler_metadata(component: dict[str, Any], metadata_path: Path) -> None:
    try:
        metadata = metadata_path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError
        value = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise PortableExportError("Hauler component metadata is invalid") from None
    required = {
        "format_version": 1,
        "archive_sha256": component["archive_sha256"],
        "archive_size": component["archive_size"],
        "references": component["references"],
    }
    for key in ("hauler_version", "source_input_sha256"):
        if key in component:
            required[key] = component[key]
    if not isinstance(value, dict) or any(
        value.get(key) != expected for key, expected in required.items()
    ):
        raise PortableExportError("Hauler component metadata identity does not match")


def _materialize_components_into(
    descriptor: LogicalJat,
    restic: ResticStore,
    component_root: Path,
    cancellation: Any = None,
) -> MaterializedComponents:
    components = descriptor.to_dict()["components"]
    paths: dict[str, Path] = {}
    manifest: dict[str, dict[str, Any]] = {}
    component_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, component in components.items():
        if component is None:
            continue
        _check_cancelled(cancellation)
        _check_component_snapshot(restic, component, name)
        destination = component_root / name
        try:
            restic.restore(component["snapshot"]["snapshot_id"], destination)
        except CLICancelled:
            raise
        except (OSError, ResticStoreError, ValueError):
            raise PortableExportError(
                "component snapshot could not be materialized"
            ) from None
        try:
            destination_metadata = destination.lstat()
            materialized_names = {path.name for path in destination.iterdir()}
            if not stat.S_ISDIR(destination_metadata.st_mode):
                raise ValueError
        except (OSError, ValueError):
            raise PortableExportError(
                "component snapshot materialized unsafely"
            ) from None
        expected_files = _COMPONENT_FILES[name]
        if materialized_names != set(expected_files):
            raise PortableExportError("component snapshot contains an unexpected path")
        try:
            if any(
                not stat.S_ISREG((destination / filename).lstat().st_mode)
                for filename in expected_files
            ):
                raise ValueError
        except (OSError, ValueError):
            raise PortableExportError(
                "component snapshot contains an unsafe filesystem entry"
            ) from None
        archive = _verify_component_archive(destination, component, name)
        if name == "rcc_environment":
            _rcc_metadata(
                component,
                archive,
                destination / "metadata.json",
                component_root / "rcc-environment-metadata.json",
            )
            paths["rcc_archive"] = archive
            paths["rcc_metadata"] = destination / "metadata.json"
            paths["jat_rcc_metadata"] = component_root / "rcc-environment-metadata.json"
        elif name == "homebrew_recovery":
            paths["brew_archive"] = archive
        else:
            _verify_hauler_metadata(component, destination / "metadata.json")
            paths["hauler_archive"] = archive
        manifest[name] = dict(component)
        _check_cancelled(cancellation)
    return MaterializedComponents(
        rcc_archive=paths.get("rcc_archive"),
        rcc_metadata=paths.get("rcc_metadata"),
        jat_rcc_metadata=paths.get("jat_rcc_metadata"),
        brew_archive=paths.get("brew_archive"),
        hauler_archive=paths.get("hauler_archive"),
        manifest=manifest,
    )


def materialize_components(
    descriptor: LogicalJat,
    restic: ResticStore,
    staging_parent: Path,
    cancellation: Any = None,
) -> MaterializedComponents:
    """Restore and verify every referenced component into an owned temporary lease."""
    owned_stage = None
    try:
        verify_private_path(Path(staging_parent), directory=True)
        owned_stage = tempfile.TemporaryDirectory(
            prefix="room-store-components-", dir=staging_parent
        )
        stage_path = Path(owned_stage.name)
        protect_private_directory(stage_path)
    except (OSError, PrivatePathError):
        if owned_stage is not None:
            owned_stage.cleanup()
        raise PortableExportError(
            "private component staging directory is unavailable"
        ) from None
    try:
        result = _materialize_components_into(
            descriptor, restic, stage_path / "components", cancellation
        )
    except BaseException:
        owned_stage.cleanup()
        raise
    result._owned_stage = owned_stage
    return result


def _semantic_workspace(root: Path, policy) -> dict[str, tuple[Any, ...]]:
    """Hash content, modes, and symlink targets while excluding canonical generated state."""
    result: dict[str, tuple[Any, ...]] = {}
    try:
        for directory, directories, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            entries = sorted((*directories, *files))
            directories[:] = [
                name
                for name in directories
                if not (parent / name).is_symlink()
                and not policy.is_excluded((parent / name).relative_to(root).as_posix())
            ]
            for name in entries:
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if policy.is_excluded(relative):
                    continue
                metadata = path.lstat()
                mode = stat.S_IMODE(metadata.st_mode)
                if stat.S_ISLNK(metadata.st_mode):
                    result[relative] = ("symlink", os.readlink(path), mode)
                elif stat.S_ISDIR(metadata.st_mode):
                    result[relative] = ("dir", mode)
                elif stat.S_ISREG(metadata.st_mode):
                    digest = hashlib.sha256()
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(_CHUNK), b""):
                            digest.update(chunk)
                    result[relative] = (
                        "file",
                        metadata.st_size,
                        digest.hexdigest(),
                        mode,
                    )
                else:
                    raise PortableExportError(
                        "workspace contains an unsafe filesystem entry"
                    )
    except PortableExportError:
        raise
    except (OSError, ValueError):
        raise PortableExportError(
            "workspace content could not be compared safely"
        ) from None
    return result


def _verify_inspection(
    inspection: dict[str, Any],
    components: dict[str, Any],
) -> None:
    if not isinstance(inspection, dict) or inspection.get("success") is not True:
        raise PortableExportError("JAT could not inspect the composed capsule")
    anchors = inspection.get("anchors")
    if not isinstance(anchors, dict):
        raise PortableExportError("JAT capsule component identities are unavailable")
    expected = {
        "workspace": True,
        "brew": components["homebrew_recovery"] is not None,
        "rcc_environment": components["rcc_environment"] is not None,
        "rcc_metadata": components["rcc_environment"] is not None,
    }
    if any(anchors.get(name) is not value for name, value in expected.items()):
        raise PortableExportError("JAT capsule component identities do not match")
    inventory = inspection.get("inventory")
    if not isinstance(inventory, list):
        raise PortableExportError("JAT capsule inventory is unavailable")
    hauler = components["hauler_content"]
    if hauler is not None:
        rows = [row for row in inventory if isinstance(row, dict)]
        used: set[int] = set()
        for expected in hauler["references"]:
            matches = [
                index
                for index, row in enumerate(rows)
                if index not in used
                and row.get("digest") == expected["digest"]
                and (
                    "kind" not in expected
                    or str(row.get("type", "")).lower() == expected["kind"]
                )
            ]
            if len(matches) != 1:
                raise PortableExportError("Hauler component identities do not match")
            used.add(matches[0])


def _inventory_reference(inventory: list[Any], artifact_name: str) -> str:
    expected = f"hauler/{artifact_name}:latest"
    matches = [
        row.get("reference")
        for row in inventory
        if isinstance(row, dict)
        and row.get("reference") == expected
        and str(row.get("type", "")).lower() == "file"
    ]
    if len(matches) != 1:
        raise PortableExportError("JAT capsule component identities are unavailable")
    return matches[0]


def _verify_extracted_component(
    *,
    jat_root: Path,
    haul: Path,
    inventory: list[Any],
    artifact_name: str,
    source: Path,
    destination: Path,
    run_extract_fn: Callable[..., dict[str, Any]],
) -> None:
    reference = _inventory_reference(inventory, artifact_name)
    try:
        expected_size, expected_digest = _digest(source)
        result = run_extract_fn(jat_root, haul, reference, destination)
        if not isinstance(result, dict) or result.get("success") is not True:
            raise ValueError
        payloads = result.get("payloads")
        if not isinstance(payloads, list) or len(payloads) != 1:
            raise ValueError
        payload = payloads[0]
        if not isinstance(payload, dict):
            raise TypeError
        extracted = Path(payload.get("path", ""))
        extracted.relative_to(destination)
        if extracted.name != artifact_name:
            raise ValueError
        actual_size, actual_digest = _digest(extracted)
        if (
            actual_size != expected_size
            or actual_digest != expected_digest
            or payload.get("size") != actual_size
            or payload.get("sha256") != actual_digest
        ):
            raise ValueError
    except (jat.JATError, OSError, TypeError, ValueError):
        raise PortableExportError(
            "JAT capsule component identity does not match"
        ) from None


def _verify_restore_result(
    result: dict[str, Any],
    components: dict[str, Any],
    expected_rcc_metadata: Path | None,
) -> None:
    if not isinstance(result, dict) or result.get("success") is not True:
        raise PortableExportError("JAT clean-room restore did not succeed")
    rcc = components["rcc_environment"]
    if rcc is not None:
        identity = result.get("environment_artifact")
        if expected_rcc_metadata is None or not isinstance(identity, dict):
            raise PortableExportError(
                "RCC component identity changed after clean-room restore"
            )
        try:
            expected = json.loads(expected_rcc_metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise PortableExportError(
                "RCC component JAT metadata is unavailable"
            ) from None
        fields = (
            "artifact",
            "specification_digest",
            "legacy_blueprint_key",
            "archive",
            "archive_sha256",
            "archive_size",
            "rcc_version",
            "platform",
            "robot",
        )
        if (
            identity.get("artifact") != rcc["artifact_digest"]
            or identity.get("specification_digest") != rcc["specification_digest"]
            or any(identity.get(field) != expected.get(field) for field in fields)
        ):
            raise PortableExportError(
                "RCC component identity changed after clean-room restore"
            )


def _read_archive(path: Path) -> tuple[int, str]:
    try:
        return _digest(path)
    except (OSError, PortableExportError):
        raise PortableExportError(
            "JAT did not produce a regular portable capsule"
        ) from None


def export_portable_jat(
    *,
    descriptor: LogicalJat,
    restic: ResticStore,
    staging_parent: Path,
    output: Path,
    jat_root: Path,
    cancellation: Any = None,
    run_build_fn: Callable[..., dict[str, Any]] = jat.run_build,
    run_inspect_fn: Callable[..., dict[str, Any]] = jat.run_inspect,
    run_restore_fn: Callable[..., dict[str, Any]] = jat.run_restore,
    run_extract_fn: Callable[..., dict[str, Any]] = jat.run_extract,
) -> PortableExportResult:
    """Materialize, compose, clean-room restore, compare, and atomically publish a logical JAT."""
    body = descriptor.to_dict()
    workspace = body["workspace"]
    components = body["components"]
    stage_parent = Path(staging_parent)
    target = Path(output)
    try:
        verify_private_path(stage_parent, directory=True)
    except (OSError, PrivatePathError):
        raise PortableExportError(
            "private export staging directory is unsafe"
        ) from None
    try:
        target_parent = target.parent.resolve(strict=True)
        if not target_parent.is_dir():
            raise PortableExportError("portable output directory is unavailable")
        target = target_parent / target.name
        if target.exists() or target.is_symlink():
            raise PortableExportError("portable output already exists")
    except PortableExportError:
        raise
    except OSError:
        raise PortableExportError("portable output directory is unavailable") from None

    component_bytes = sum(
        component["archive_size"]
        for component in components.values()
        if component is not None
    )
    estimated = 2 * (workspace["logical_bytes"] + component_bytes)
    try:
        available = min(
            shutil.disk_usage(stage_parent).free, shutil.disk_usage(target_parent).free
        )
    except OSError:
        raise PortableExportError(
            "portable export disk-space preflight failed"
        ) from None
    if available < estimated:
        raise PortableExportError("portable export requires more free disk space")

    output_stage = tempfile.TemporaryDirectory(
        prefix=".josh-room-export-", dir=target_parent
    )
    temporary_output: Path | None = Path(output_stage.name) / "portable.haul.tar.zst"
    try:
        try:
            protect_private_directory(Path(output_stage.name))
        except PrivatePathError:
            raise PortableExportError(
                "portable export staging could not be secured"
            ) from None
        with tempfile.TemporaryDirectory(
            prefix="portable-export-", dir=stage_parent
        ) as temporary:
            staging = Path(temporary)
            try:
                protect_private_directory(staging)
            except PrivatePathError:
                raise PortableExportError(
                    "private export staging directory is unsafe"
                ) from None
            workspace_stage = staging / "workspace"
            snapshot_id = workspace["snapshot_id"]
            try:
                snapshot = restic.snapshot(snapshot_id)
                if snapshot.tree_id != workspace["tree_id"]:
                    raise PortableExportError(
                        "workspace snapshot tree identity does not match"
                    )
                workspace_rows = list(restic.entries(snapshot_id))
                _validate_snapshot_entries(workspace_rows)
                restic.restore(snapshot_id, workspace_stage)
                policy = load_capture_policy(workspace_stage)
                _scan_workspace(workspace_stage, policy)
                _semantic_workspace(workspace_stage, policy)
            except CLICancelled:
                raise
            except PortableExportError:
                raise
            except (
                OSError,
                ResticStoreError,
                RoomStoreOperationsError,
                ValueError,
                TypeError,
            ):
                raise PortableExportError(
                    "workspace snapshot could not be materialized safely"
                ) from None
            expected_workspace = _semantic_workspace(workspace_stage, policy)
            _check_cancelled(cancellation)

            materialized = _materialize_components_into(
                descriptor, restic, staging / "components", cancellation
            )
            build_kwargs: dict[str, Any] = {}
            if materialized.rcc_archive is not None:
                if materialized.jat_rcc_metadata is None:
                    raise PortableExportError(
                        "RCC component JAT metadata is unavailable"
                    )
                build_kwargs.update(
                    rcc_archive=materialized.rcc_archive,
                    rcc_metadata=materialized.jat_rcc_metadata,
                )
            if materialized.brew_archive is not None:
                build_kwargs["brew_archive"] = materialized.brew_archive
            if materialized.hauler_archive is not None:
                build_kwargs["hauler_archive"] = materialized.hauler_archive

            try:
                build_result = run_build_fn(
                    Path(jat_root), workspace_stage, temporary_output, **build_kwargs
                )
            except CLICancelled:
                raise
            except jat.JATError:
                raise PortableExportError("JAT capsule composition failed") from None
            if (
                not isinstance(build_result, dict)
                or build_result.get("success") is not True
            ):
                raise PortableExportError("JAT capsule composition failed")
            _check_cancelled(cancellation)
            _read_archive(temporary_output)
            try:
                protect_private_file(temporary_output)
            except PrivatePathError:
                raise PortableExportError(
                    "composed capsule staging could not be secured"
                ) from None

            try:
                inspection = run_inspect_fn(Path(jat_root), temporary_output)
            except CLICancelled:
                raise
            except jat.JATError:
                raise PortableExportError("JAT capsule inspection failed") from None
            _verify_inspection(inspection, components)
            _check_cancelled(cancellation)

            capsule_inventory = inspection["inventory"]
            component_checks = staging / "component-checks"
            component_checks.mkdir()
            if materialized.brew_archive is not None:
                _verify_extracted_component(
                    jat_root=Path(jat_root),
                    haul=temporary_output,
                    inventory=capsule_inventory,
                    artifact_name=materialized.brew_archive.name,
                    source=materialized.brew_archive,
                    destination=component_checks / "homebrew-recovery",
                    run_extract_fn=run_extract_fn,
                )
            if materialized.rcc_archive is not None:
                if materialized.jat_rcc_metadata is None:
                    raise PortableExportError(
                        "RCC component JAT metadata is unavailable"
                    )
                for artifact_name, source, label in (
                    (
                        materialized.rcc_archive.name,
                        materialized.rcc_archive,
                        "rcc-environment",
                    ),
                    (
                        "rcc-environment-metadata.json",
                        materialized.jat_rcc_metadata,
                        "rcc-metadata",
                    ),
                ):
                    _verify_extracted_component(
                        jat_root=Path(jat_root),
                        haul=temporary_output,
                        inventory=capsule_inventory,
                        artifact_name=artifact_name,
                        source=source,
                        destination=component_checks / label,
                        run_extract_fn=run_extract_fn,
                    )
            _check_cancelled(cancellation)

            clean_restore = staging / "clean-room-restore"
            try:
                restore_result = run_restore_fn(
                    Path(jat_root), temporary_output, clean_restore
                )
            except CLICancelled:
                raise
            except jat.JATError:
                raise PortableExportError("JAT clean-room restore failed") from None
            _verify_restore_result(
                restore_result, components, materialized.jat_rcc_metadata
            )
            workspace_container = clean_restore / "workspace"
            restored_workspace = workspace_container / workspace_stage.name
            try:
                if workspace_container.is_symlink() or set(workspace_container.iterdir()) != {restored_workspace}:
                    raise PortableExportError("JAT clean-room workspace root identity does not match")
                restored_policy = load_capture_policy(restored_workspace)
                _scan_workspace(restored_workspace, restored_policy)
                actual_workspace = _semantic_workspace(
                    restored_workspace, restored_policy
                )
            except PortableExportError:
                raise
            except (OSError, RoomStoreOperationsError, ValueError, TypeError):
                raise PortableExportError(
                    "JAT clean-room workspace is unavailable"
                ) from None
            if actual_workspace != expected_workspace:
                raise PortableExportError(
                    "JAT clean-room workspace content identity does not match"
                )
            if (
                components["homebrew_recovery"] is not None
                and not (clean_restore / "homebrew-recovery").is_dir()
            ):
                raise PortableExportError(
                    "Homebrew component identity is missing after clean-room restore"
                )
            size, digest = _read_archive(temporary_output)
            _check_cancelled(cancellation)
            try:
                os.chmod(temporary_output, 0o600)
                os.link(temporary_output, target, follow_symlinks=False)
                temporary_output.unlink()
                temporary_output = None
            except FileExistsError:
                raise PortableExportError("portable output already exists") from None
            except OSError:
                raise PortableExportError(
                    "portable output could not be promoted atomically"
                ) from None
            return PortableExportResult(
                logical_jat_id=body["logical_jat_id"],
                status="exported",
                output_size=size,
                output_sha256=digest,
                workspace_entry_count=len(expected_workspace),
                verified_components=tuple(
                    name for name, value in components.items() if value is not None
                ),
                estimated_required_bytes=estimated,
                available_bytes=available,
            )
    except CLICancelled:
        raise PortableExportError("portable export cancelled") from None
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)
        output_stage.cleanup()
