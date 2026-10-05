"""Freeze Hauler's local manifest dependencies inside the managed JAT worker."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .private_paths import protect_private_directory, protect_private_file
from .room_store_hauler import (
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SOURCE_FILE,
    RoomStoreHaulerError,
    _freeze_input,
    _read_input,
)


def prepare_manifest_inputs(manifests, staging=None, local_image_lookup=None):
    import yaml

    digest = hashlib.sha256(b"josh-room-manifest-inputs-v1\0")
    stage = Path(staging) if staging is not None else None
    if stage is not None:
        stage.mkdir(mode=0o700)
        protect_private_directory(stage)
    frozen = []
    local_images = []
    total_bytes = 0
    count = 0

    def capture(path, label):
        nonlocal total_bytes, count
        path = Path(path)
        if path.is_symlink():
            raise RoomStoreHaulerError("manifest dependency is unsafe")
        paths = []
        if path.is_dir():
            for directory, dirs, files in os.walk(path, followlinks=False):
                if any((Path(directory) / name).is_symlink() for name in dirs):
                    raise RoomStoreHaulerError("manifest dependency is unsafe")
                paths.extend(Path(directory) / name for name in sorted(files))
        else:
            paths = [path]
        owned = stage / f"dependency-{count}" / path.name if stage is not None else None
        for source in sorted(paths):
            count += 1
            if count > 100_000:
                raise RoomStoreHaulerError("manifest dependencies exceed their member limit")
            size = source.lstat().st_size
            total_bytes += size
            if total_bytes > MAX_ARCHIVE_BYTES or not stat.S_ISREG(source.lstat().st_mode):
                raise RoomStoreHaulerError("manifest dependencies exceed their input limit")
            sha, read_size = _read_input(source, MAX_SOURCE_FILE)
            relative = str(source.relative_to(path)) if path.is_dir() else source.name
            digest.update(json.dumps([label, relative, sha, read_size], separators=(",", ":")).encode())
            if owned is not None:
                target = owned / relative if path.is_dir() else owned
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _freeze_input(source, target, MAX_SOURCE_FILE, None)
                if _read_input(target, MAX_SOURCE_FILE) != (sha, read_size):
                    raise RoomStoreHaulerError("manifest dependency changed while being staged")
        if owned is not None and path.is_dir():
            owned.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(owned if owned is not None else path)

    def file_uri_path(parsed):
        if parsed.netloc.lower() not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise RoomStoreHaulerError("local file URI authority is unsupported")
        path = Path(url2pathname(parsed.path))
        if not path.is_absolute():
            raise RoomStoreHaulerError("local file URI is not absolute")
        return path

    for index, original in enumerate(manifests):
        manifest = Path(original)
        before = _read_input(manifest, MAX_MANIFEST_BYTES)
        raw = manifest.read_bytes()
        if (hashlib.sha256(raw).hexdigest(), len(raw)) != before:
            raise RoomStoreHaulerError("Hauler manifest changed while being staged")
        documents = list(yaml.safe_load_all(raw.decode("utf-8")))
        digest.update(json.dumps([index, *before], separators=(",", ":")).encode())
        for document in documents:
            if not isinstance(document, dict) or not isinstance(document.get("spec"), dict):
                raise RoomStoreHaulerError("Hauler manifest is invalid")
            spec = document["spec"]
            kind = document.get("kind")
            if kind == "Charts":
                charts = [(chart, list(chart.get("valuesFiles", [])), chart.get("repoURL"))
                          for chart in spec.get("charts", [])]
                for chart, values, repository in charts:
                    for value_index, value in enumerate(values):
                        if not isinstance(value, str) or not value:
                            raise RoomStoreHaulerError("manifest dependency is invalid")
                        source = Path(value)
                        if not source.is_absolute():
                            source = manifest.parent / source
                        values[value_index] = capture(source, [index, "values", value])
                    if "valuesFiles" in chart:
                        chart["valuesFiles"] = values
                    if isinstance(repository, str):
                        parsed = urlsplit(repository)
                        local = file_uri_path(parsed) if parsed.scheme == "file" else Path(repository)
                        if parsed.scheme == "file" or (not parsed.scheme or local.is_absolute()) and local.exists():
                            captured = capture(local, [index, "repository", repository])
                            chart["repoURL"] = Path(captured).resolve().as_uri() if parsed.scheme == "file" else captured
            elif kind == "Files":
                for entry in spec.get("files", []):
                    value = entry.get("path")
                    if isinstance(value, str):
                        parsed = urlsplit(value)
                        if parsed.scheme == "file":
                            captured = capture(file_uri_path(parsed), [index, "file", value])
                            entry["path"] = Path(captured).resolve().as_uri()
                        elif not parsed.scheme or Path(value).is_absolute():
                            entry["path"] = capture(Path(value), [index, "file", value])
            elif kind == "Images":
                names = [entry["name"] for entry in spec.get("images", []) if entry.get("local") is True]
                if names:
                    if local_image_lookup is None:
                        raise RoomStoreHaulerError("local manifest images require native identity evidence")
                    observed = local_image_lookup(names)
                    identities = dict(observed)
                    local_images.extend(
                        [entry.get("rewrite") or entry["name"], identities[entry["name"]]]
                        for entry in spec["images"] if entry.get("local") is True
                    )
                    digest.update(json.dumps(observed, separators=(",", ":")).encode())
        if _read_input(manifest, MAX_MANIFEST_BYTES) != before:
            raise RoomStoreHaulerError("Hauler manifest changed while being staged")
        if stage is None:
            frozen.append(str(manifest))
        else:
            target = stage / f"manifest-{index}.yaml"
            target.write_text(yaml.safe_dump_all(documents, sort_keys=False), encoding="utf-8")
            protect_private_file(target)
            frozen.append(str(target))
    return {"sha256": digest.hexdigest(), "manifests": frozen, "local_images": local_images}
