"""Native Hauler content-component capture for Room Store saves."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .logical_jat import MAX_LOGICAL_JAT_BYTES
from .private_paths import (
    protect_private_directory,
    protect_private_file,
    verify_private_path,
)

MAX_SOURCE_FILE = 8 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SOURCES = 4096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$")
_KINDS = {"image", "chart", "file"}


class RoomStoreHaulerError(RuntimeError):
    """Path-free Hauler component failure."""


def _cancelled(token: Any) -> bool:
    return token is not None and bool(token.cancelled)


def _check_cancel(token: Any) -> None:
    if _cancelled(token):
        raise RoomStoreHaulerError("Hauler component capture was cancelled")


def _validate_image_reference(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character.isspace() for character in value)
        or "://" in value
        or "?" in value
        or "#" in value
        or "@" in value and re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", value) is None
        or not re.fullmatch(r"[A-Za-z0-9._:/-]+(?:@sha256:[0-9a-f]{64})?", value)
    ):
        raise RoomStoreHaulerError("Hauler image selection is invalid")
    if value.startswith("/") or "/../" in f"/{value}/" or "/./" in f"/{value}/":
        raise RoomStoreHaulerError("Hauler image selection is invalid")
    return value


def _canonical_image_reference(reference: str) -> str:
    name, separator, digest = reference.partition("@")
    if separator:
        suffix = f"@{digest}"
    else:
        final = name.rsplit("/", 1)[-1]
        suffix = "" if ":" in final else ":latest"
    if name.startswith("docker.io/"):
        name = "index.docker.io/" + name.removeprefix("docker.io/")
    return name + suffix


def _verify_requested_images(requested: Sequence[str], inventory: object) -> None:
    if not requested:
        return
    if not isinstance(inventory, list):
        raise RoomStoreHaulerError("Hauler returned invalid component inventory")
    for selected in requested:
        expected = _canonical_image_reference(selected)
        matching = [
            item
            for item in inventory
            if isinstance(item, Mapping)
            and str(item.get("Type", item.get("type", ""))).lower() == "image"
            and _canonical_image_reference(str(item.get("Reference", item.get("reference", "")))) == expected
        ]
        if not matching:
            raise RoomStoreHaulerError("Hauler omitted a selected image")
        if "@sha256:" in selected:
            expected_digest = selected.rsplit("@", 1)[1]
            if any(item.get("Digest", item.get("digest")) != expected_digest for item in matching):
                raise RoomStoreHaulerError("Hauler selected image digest did not match")


def _hauler_call(callback):
    try:
        return callback()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise RoomStoreHaulerError("Hauler component capture failed") from None


def _read_input(path: Path, limit: int) -> tuple[str, int]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > limit:
            raise RoomStoreHaulerError("Hauler input is unsafe")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ):
                raise RoomStoreHaulerError("Hauler input changed during capture")
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                size += len(chunk)
                if size > limit:
                    raise RoomStoreHaulerError("Hauler input exceeds its size limit")
                digest.update(chunk)
            after = os.fstat(source.fileno())
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) or size != before.st_size:
            raise RoomStoreHaulerError("Hauler input changed during capture")
        return digest.hexdigest(), size
    except RoomStoreHaulerError:
        raise
    except OSError:
        raise RoomStoreHaulerError("Hauler input is unavailable") from None


def _source_identity(
    *,
    images: Sequence[str],
    manifests: Sequence[Path],
    files: Sequence[tuple[Path, str]],
    hauler_version: str,
    local_images: Sequence[tuple[str, str]] = (),
    manifest_identity: str | None = None,
) -> tuple[str, bool]:
    if not isinstance(hauler_version, str) or not _VERSION.fullmatch(hauler_version):
        raise RoomStoreHaulerError("selected Hauler version is invalid")
    if len(images) + len(manifests) + len(files) + len(local_images) > MAX_SOURCES:
        raise RoomStoreHaulerError("Hauler selection contains too many sources")
    image_refs = [_validate_image_reference(item) for item in images]
    if len(set(image_refs)) != len(image_refs):
        raise RoomStoreHaulerError("Hauler image selection contains duplicates")
    digest = hashlib.sha256(b"josh-room-hauler-inputs-v1\0")
    digest.update(hauler_version.encode("ascii") + b"\0")
    if manifest_identity is not None:
        if not _SHA256.fullmatch(manifest_identity):
            raise RoomStoreHaulerError("manifest identity is invalid")
        digest.update(b"manifest-dependencies\0" + bytes.fromhex(manifest_identity))
    for image in sorted(image_refs):
        digest.update(b"image\0" + image.encode("utf-8") + b"\0")
    local_names = set()
    for image, identity in sorted(local_images):
        image = _validate_image_reference(image)
        if image in local_names or not isinstance(identity, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", identity) is None:
            raise RoomStoreHaulerError("local image identity is invalid")
        local_names.add(image)
        digest.update(b"local-image\0" + image.encode("utf-8") + b"\0" + identity.encode("ascii") + b"\0")
    pinned = all("@sha256:" in image for image in image_refs)
    for index, manifest in enumerate(manifests):
        manifest_digest, size = _read_input(Path(manifest), MAX_MANIFEST_BYTES)
        digest.update(b"manifest\0" + index.to_bytes(4, "big") + bytes.fromhex(manifest_digest) + size.to_bytes(8, "big"))
        pinned = False
    seen_names: set[str] = set()
    for source, name in files:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) or name in {".", ".."}:
            raise RoomStoreHaulerError("Hauler file selection is invalid")
        if name in seen_names:
            raise RoomStoreHaulerError("Hauler file selection contains duplicate names")
        seen_names.add(name)
        source_digest, size = _read_input(Path(source), MAX_SOURCE_FILE)
        digest.update(b"file\0" + name.encode("ascii") + b"\0" + bytes.fromhex(source_digest) + size.to_bytes(8, "big"))
    return digest.hexdigest(), pinned


def _freeze_input(source_path: Path, owned: Path, limit: int, cancellation) -> None:
    before = source_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RoomStoreHaulerError("Hauler input is unsafe")
    descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source, owned.open("xb") as output:
        opened = os.fstat(source.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_size > limit:
            raise RoomStoreHaulerError("Hauler input changed while being staged")
        protect_private_file(owned)
        copied = 0
        while block := source.read(1024 * 1024):
            _check_cancel(cancellation)
            copied += len(block)
            if copied > limit:
                raise RoomStoreHaulerError("Hauler input exceeds its size limit")
            output.write(block)
        after = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ) or copied != opened.st_size:
            raise RoomStoreHaulerError("Hauler input changed while being staged")


def _native_references(inventory: object) -> list[dict[str, str]]:
    if not isinstance(inventory, list) or not inventory or len(inventory) > MAX_SOURCES:
        raise RoomStoreHaulerError("Hauler returned invalid component inventory")
    references: dict[tuple[str, ...], dict[str, str]] = {}
    for item in inventory:
        if not isinstance(item, Mapping):
            raise RoomStoreHaulerError("Hauler returned invalid component inventory")
        digest = item.get("Digest") or item.get("digest")
        kind_value = item.get("Type") or item.get("type")
        media_type = item.get("MediaType") or item.get("mediaType") or item.get("media_type")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise RoomStoreHaulerError("Hauler inventory is missing immutable digest evidence")
        value = {"digest": digest}
        native_reference = item.get("Reference", item.get("reference"))
        native_platform = item.get("Platform", item.get("platform"))
        if native_reference is not None:
            if (not isinstance(native_reference, str) or not native_reference
                    or len(native_reference) > 4096 or any(character.isspace() for character in native_reference)):
                raise RoomStoreHaulerError("Hauler reference is invalid")
            if native_reference in {
                "hauler/joshs-all-the-things-workspace.tar.zst:latest",
                "hauler/homebrew-recovery.tar.zst:latest",
                "hauler/rcc-environment.rcca:latest",
                "hauler/rcc-environment-metadata.json:latest",
            } or native_reference.endswith(".rcca:latest"):
                raise RoomStoreHaulerError("Hauler content contains a reserved JAT anchor")
            value["reference_sha256"] = hashlib.sha256(native_reference.encode("utf-8")).hexdigest()
        if native_platform is not None:
            if not isinstance(native_platform, str) or not native_platform or len(native_platform) > 128:
                raise RoomStoreHaulerError("Hauler platform is invalid")
            value["platform_sha256"] = hashlib.sha256(native_platform.encode("utf-8")).hexdigest()
        kind = str(kind_value).lower() if isinstance(kind_value, str) else ""
        if kind in _KINDS:
            value["kind"] = kind
        if media_type is not None:
            if not isinstance(media_type, str) or not _MIME.fullmatch(media_type):
                raise RoomStoreHaulerError("Hauler returned invalid native media type")
            value["media_type"] = media_type
        if "kind" not in value and "media_type" not in value:
            raise RoomStoreHaulerError("Hauler inventory lacks native type evidence")
        identity = (digest, value.get("kind", ""), value.get("media_type", ""),
                    value.get("reference_sha256", ""), value.get("platform_sha256", ""))
        references[identity] = value
    result = [references[key] for key in sorted(references)]
    if len(json.dumps(result, separators=(",", ":")).encode("utf-8")) > MAX_LOGICAL_JAT_BYTES // 2:
        raise RoomStoreHaulerError("Hauler references exceed the logical descriptor budget")
    return result


def _prior_references(value: object) -> list[dict[str, str]] | None:
    if not isinstance(value, list):
        return None
    references = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        digest = item.get("digest")
        kind = item.get("kind")
        media_type = item.get("media_type")
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            return None
        reference = {"digest": digest}
        for field in ("reference_sha256", "platform_sha256"):
            if field in item:
                if not isinstance(item[field], str) or not item[field]:
                    return None
                reference[field] = item[field]
        if kind is not None:
            if kind not in _KINDS:
                return None
            reference["kind"] = kind
        if media_type is not None:
            if not isinstance(media_type, str) or not _MIME.fullmatch(media_type):
                return None
            reference["media_type"] = media_type
        if len(reference) == 1:
            return None
        references.append(reference)
    return sorted(references, key=lambda item: (item["digest"], item.get("kind", ""), item.get("media_type", ""), item.get("reference_sha256", ""), item.get("platform_sha256", "")))


def _validate_prior(
    component: Mapping[str, Any], repository_id: str, repository_format: int
) -> dict[str, Any]:
    snapshot = component.get("snapshot")
    if (
        component.get("kind") != "hauler-content"
        or not isinstance(snapshot, Mapping)
        or snapshot.get("repository_id") != repository_id
        or snapshot.get("repository_format") != repository_format
        or not isinstance(snapshot.get("snapshot_id"), str)
        or not _SHA256.fullmatch(snapshot["snapshot_id"])
    ):
        raise RoomStoreHaulerError("prior Hauler component belongs to another Room Store")
    return dict(snapshot)


def capture_hauler_component(
    *,
    workspace: Path,
    prior_component: Mapping[str, Any] | None,
    repository_id: str,
    repository_format: int,
    restic: Any,
    hauler: Any,
    hauler_version: str,
    requested_images: Sequence[str] = (),
    local_images: Sequence[tuple[str, str]] = (),
    manifests: Sequence[Path] = (),
    files: Sequence[tuple[Path, str]] = (),
    cancellation: Any = None,
) -> dict[str, Any] | None:
    """Acquire selected content through Hauler and persist one small component tree."""
    del workspace  # Source selection is explicit and independent of workspace contents.
    if repository_format != 2 or not isinstance(repository_id, str) or not _SHA256.fullmatch(repository_id):
        raise RoomStoreHaulerError("Room Store repository identity is invalid")
    images = tuple(requested_images)
    manifest_paths = tuple(Path(path) for path in manifests)
    file_inputs = tuple((Path(path), name) for path, name in files)
    if not images and not manifest_paths and not file_inputs and not local_images:
        return None
    manifest_plan = _hauler_call(lambda: hauler.manifest_inputs(manifest_paths)) if manifest_paths else None
    manifest_identity = manifest_plan["sha256"] if manifest_plan else None
    source_digest, pinned_sources = _source_identity(
        images=images,
        manifests=manifest_paths,
        files=file_inputs,
        hauler_version=hauler_version,
        local_images=local_images,
        manifest_identity=manifest_identity,
    )
    prior_snapshot = None
    prior_refs = None
    if prior_component is not None:
        prior_snapshot = _validate_prior(prior_component, repository_id, repository_format)
        prior_refs = _prior_references(prior_component.get("references"))
        if (
            pinned_sources
            and prior_component.get("source_input_sha256") == source_digest
            and prior_component.get("hauler_version") == hauler_version
            and prior_refs
        ):
            return dict(prior_component)
    _check_cancel(cancellation)
    with tempfile.TemporaryDirectory(prefix="josh-room-hauler-") as temporary:
        private = Path(temporary)
        protect_private_directory(private)
        verify_private_path(private, directory=True)
        hauler_store = private / "store"
        hauler_temp = private / "hauler-temp"
        hauler_temp.mkdir(mode=0o700)
        protect_private_directory(hauler_temp)
        component_stage = private / "component"
        component_stage.mkdir(mode=0o700)
        protect_private_directory(component_stage)
        try:
            input_stage = private / "inputs"
            input_stage.mkdir(mode=0o700)
            protect_private_directory(input_stage)
            frozen_files = []
            for index, (source_path, name) in enumerate(file_inputs):
                owned = input_stage / f"file-{index}"
                _freeze_input(Path(source_path), owned, MAX_SOURCE_FILE, cancellation)
                frozen_files.append((owned, name))
            frozen_manifests = []
            if manifest_paths:
                frozen_plan = _hauler_call(lambda: hauler.manifest_inputs(manifest_paths, input_stage / "manifests"))
                if frozen_plan["sha256"] != manifest_identity:
                    raise RoomStoreHaulerError("manifest inputs changed while being staged")
                frozen_manifests = frozen_plan["manifests"]
            if _source_identity(images=images, manifests=manifest_paths, files=frozen_files,
                                hauler_version=hauler_version, local_images=local_images,
                                manifest_identity=manifest_identity)[0] != source_digest:
                raise RoomStoreHaulerError("Hauler inputs changed while being staged")
            if images:
                image_list = private / "selected-images.txt"
                image_list.write_text("".join(f"{image}\n" for image in images), encoding="utf-8")
                protect_private_file(image_list)
                _check_cancel(cancellation)
                _hauler_call(lambda: hauler.sync_image_txt(hauler_store, hauler_temp, [str(image_list)]))
                _check_cancel(cancellation)
            if manifest_paths:
                _check_cancel(cancellation)
                _hauler_call(lambda: hauler.sync(hauler_store, hauler_temp, *frozen_manifests))
                _check_cancel(cancellation)
            if file_inputs or local_images:
                _check_cancel(cancellation)
                _hauler_call(lambda: hauler.sync_files(hauler_store, hauler_temp, frozen_files,
                                                      **({"images": [name for name, _identity in local_images]} if local_images else {})))
                _check_cancel(cancellation)
            protect_private_directory(hauler_store)
            protect_private_directory(hauler_temp)
            expected_local = [*local_images, *(manifest_plan["local_images"] if manifest_plan else [])]
            if expected_local:
                _hauler_call(lambda: hauler.verify_local_images(hauler_store, expected_local))
            inventory = _hauler_call(lambda: hauler.inventory(hauler_store, hauler_temp))
            _verify_requested_images(images, inventory)
            _verify_requested_images([name for name, _identity in local_images], inventory)
            references = _native_references(inventory)
            if prior_refs is not None and prior_refs == references and prior_component.get("source_input_sha256") == source_digest and prior_component.get("hauler_version") == hauler_version:
                return dict(prior_component)
            archive = component_stage / "hauler-content.tar.zst"
            _check_cancel(cancellation)
            _hauler_call(lambda: hauler.save(hauler_store, hauler_temp, archive))
            _check_cancel(cancellation)
            metadata = archive.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or metadata.st_size <= 0:
                raise RoomStoreHaulerError("Hauler produced an invalid component archive")
            if metadata.st_size > MAX_ARCHIVE_BYTES:
                raise RoomStoreHaulerError("Hauler archive exceeds the portable export limit")
            protect_private_file(archive)
            archive_digest = hashlib.sha256()
            with archive.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    archive_digest.update(chunk)
            metadata_value = {
                "format_version": 1,
                "hauler_version": hauler_version,
                "source_input_sha256": source_digest,
                "references": references,
                "archive_sha256": archive_digest.hexdigest(),
                "archive_size": metadata.st_size,
            }
            metadata_path = component_stage / "metadata.json"
            metadata_path.write_text(json.dumps(metadata_value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            protect_private_file(metadata_path)
            verify_private_path(component_stage, directory=True)
            if manifest_paths and _hauler_call(lambda: hauler.manifest_inputs(manifest_paths))["sha256"] != manifest_identity:
                raise RoomStoreHaulerError("manifest inputs changed during capture")
            if _source_identity(
                images=images,
                manifests=manifest_paths,
                files=file_inputs,
                hauler_version=hauler_version,
                local_images=local_images,
                manifest_identity=manifest_identity,
            )[0] != source_digest:
                raise RoomStoreHaulerError("Hauler selection changed during capture")
            _check_cancel(cancellation)
            parent_id = prior_snapshot["snapshot_id"] if prior_snapshot else None
            summary = restic.backup(component_stage, parent=parent_id, cancellation=cancellation)
            snapshot_id = getattr(summary, "snapshot_id", None)
            if not isinstance(snapshot_id, str) or not _SHA256.fullmatch(snapshot_id):
                raise RoomStoreHaulerError("Restic did not capture the Hauler component")
            saved = restic.snapshot(snapshot_id)
            return {
                "kind": "hauler-content",
                "snapshot": {
                    "repository_id": repository_id,
                    "repository_format": repository_format,
                    "snapshot_id": snapshot_id,
                    "tree_id": saved.tree_id,
                },
                "archive_sha256": archive_digest.hexdigest(),
                "archive_size": metadata.st_size,
                "member_basename": "hauler-content.tar.zst",
                "references": references,
                "source_input_sha256": source_digest,
                "hauler_version": hauler_version,
            }
        except RoomStoreHaulerError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise RoomStoreHaulerError("Hauler component capture failed") from None
