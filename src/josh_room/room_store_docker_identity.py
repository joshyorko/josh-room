"""Read exact image-config identities from Docker's supported image-save stream."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import BinaryIO

MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 100_000
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_IMAGE_COUNT = 4096
MAX_IMAGE_SAVE_TIMEOUT = 3600.0
MAX_JSON_TOKENS = 100_000
MAX_JSON_DEPTH = 64
_MAX_MEMBER_NAME_BYTES = 256
_MAX_MEMBER_OVERHEAD = 192
_CHUNK_BYTES = 64 * 1024
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OCI_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_OCI_MANIFEST_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_OCI_CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}


class DockerImageIdentityError(RuntimeError):
    """A selected Docker image could not be bound to saved config bytes."""


def _fail(message: str = "Docker image config identity could not be verified") -> None:
    raise DockerImageIdentityError(message) from None


class _BoundedReader:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.bytes_read = 0

    def read(self, size: int) -> bytes:
        if size < 0:
            _fail("Docker image save archive is invalid")
        remaining = MAX_ARCHIVE_BYTES - self.bytes_read
        data = self.stream.read(min(size, remaining + 1))
        if data is None or not isinstance(data, (bytes, bytearray)):
            _fail("Docker image save stream is invalid")
        if len(data) > remaining:
            _fail("Docker image save archive exceeds its size limit")
        self.bytes_read += len(data)
        return bytes(data)

    def exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            block = self.read(size - len(result))
            if not block:
                _fail("Docker image save archive is truncated")
            result.extend(block)
        return bytes(result)

@dataclass(frozen=True)
class _JsonRecord:
    size: int
    digest: str
    raw: bytes
    value: object
    kind: str


def _parse_octal(value: bytes) -> int:
    if value and value[0] & 0x80:
        _fail("Docker image save archive uses unsupported numeric fields")
    text = value.strip(b"\0 ")
    if not text:
        return 0
    if any(character not in b"01234567" for character in text):
        _fail("Docker image save archive has invalid numeric fields")
    return int(text, 8)


def _member_path(header: bytes) -> tuple[str, bool]:
    def field(raw: bytes) -> bytes:
        value, separator, trailing = raw.partition(b"\0")
        if separator and any(trailing):
            _fail("Docker image save archive has an ambiguous member name")
        return value

    name_bytes = field(header[:100])
    prefix_bytes = field(header[345:500])
    try:
        name = name_bytes.decode("utf-8")
        prefix = prefix_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _fail("Docker image save archive has an invalid member name")
    if prefix:
        name = f"{prefix}/{name}"
    directory = header[156:157] == b"5"
    if directory and name.endswith("/"):
        name = name[:-1]
    encoded = name.encode("utf-8")
    parts = name.split("/")
    if (not encoded or len(encoded) > _MAX_MEMBER_NAME_BYTES or name.startswith("/")
            or "\\" in name or ":" in name
            or any(part in {"", ".", ".."} for part in parts)):
        _fail("Docker image save archive contains an unsafe member path")
    return name, directory


def _validate_header(header: bytes) -> None:
    expected = _parse_octal(header[148:156])
    observed = sum(header[:148]) + (32 * 8) + sum(header[156:])
    if expected != observed:
        _fail("Docker image save archive has an invalid header")
    magic = header[257:263]
    if magic not in {b"\0" * 6, b"ustar\0", b"ustar "}:
        _fail("Docker image save archive format is unsupported")


def _json_kind(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    if (type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2
            and isinstance(value.get("manifests"), list)):
        return "index"
    if (type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2
            and isinstance(value.get("config"), dict) and isinstance(value.get("layers"), list)):
        return "manifest"
    if (isinstance(value.get("architecture"), str) and isinstance(value.get("os"), str)
            and isinstance(value.get("rootfs"), dict) and isinstance(value.get("config"), dict)):
        return "config"
    return ""


def _strict_json_loads(raw: bytes):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("invalid JSON constant")

    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


def _json_complexity(raw: bytes) -> tuple[int, int]:
    tokens = 0
    depth = 0
    maximum_depth = 0
    in_string = False
    escaped = False
    for index, byte in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            tokens += 1
            in_string = True
        elif byte in {0x7B, 0x5B}:
            tokens += 1
            depth += 1
            maximum_depth = max(maximum_depth, depth)
        elif byte in {0x7D, 0x5D}:
            depth -= 1
        elif byte in b"-0123456789tfn":
            previous = raw[index - 1] if index else 0x20
            if previous in b" \t\r\n[:,{":
                tokens += 1
        if tokens > MAX_JSON_TOKENS or maximum_depth > MAX_JSON_DEPTH:
            return tokens, maximum_depth
    return tokens, maximum_depth


def _read_json_record(
    raw: bytes,
    size: int,
    expected_blob: str | None = None,
    remaining_memory: int = MAX_METADATA_BYTES,
) -> _JsonRecord | None:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected_blob is not None and digest != expected_blob:
        _fail("Docker image save blob digest does not match")
    tokens, depth = _json_complexity(raw)
    if tokens > MAX_JSON_TOKENS or depth > MAX_JSON_DEPTH:
        return None
    if (2 * len(raw)) + (tokens * 128) > remaining_memory:
        return None
    try:
        value = _strict_json_loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
        return None
    kind = _json_kind(value)
    if not kind:
        return None
    return _JsonRecord(size=size, digest=digest, raw=raw, value=value, kind=kind)


def _read_member(reader: _BoundedReader, path: str, size: int, metadata_budget: list[int]):
    root_json = path in {"manifest.json", "index.json"}
    root_config = "/" not in path and path.endswith(".json") and not root_json
    blob_match = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", path)
    if root_json and size > MAX_METADATA_BYTES:
        _fail("Docker image save metadata exceeds its size limit")
    if root_config and size > MAX_CONFIG_BYTES:
        _fail("Docker image config exceeds its size limit")
    if (root_json or root_config) and metadata_budget[0] + (2 * size) > MAX_METADATA_BYTES:
        _fail("Docker image save metadata exceeds its memory limit")
    candidate = root_json or root_config
    if blob_match is not None and size <= MAX_CONFIG_BYTES:
        candidate = (2 * size) <= MAX_METADATA_BYTES - metadata_budget[0]
    digest = hashlib.sha256()
    body = bytearray() if candidate else None
    remaining = size
    while remaining:
        block = reader.read(min(remaining, _CHUNK_BYTES))
        if not block:
            _fail("Docker image save archive is truncated")
        digest.update(block)
        if body is not None:
            body.extend(block)
        remaining -= len(block)
    hexdigest = "sha256:" + digest.hexdigest()
    if blob_match is not None:
        expected = "sha256:" + blob_match.group(1)
        if hexdigest != expected:
            _fail("Docker image save blob digest does not match")
    if body is None:
        return None

    raw = bytes(body)
    del body
    if root_json:
        tokens, depth = _json_complexity(raw)
        if tokens > MAX_JSON_TOKENS or depth > MAX_JSON_DEPTH:
            _fail("Docker image save metadata is invalid")
        estimate = (2 * len(raw)) + (tokens * 128)
        if estimate > MAX_METADATA_BYTES - metadata_budget[0]:
            _fail("Docker image save metadata exceeds its memory limit")
        try:
            value = _strict_json_loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            _fail("Docker image save metadata is invalid")
        expected_kind = "docker-manifest" if path == "manifest.json" else "index"
        if expected_kind == "docker-manifest" and not isinstance(value, list):
            _fail("Docker image save manifest is invalid")
        index_media_type = value.get("mediaType") if isinstance(value, dict) else None
        if expected_kind == "index" and not (
            isinstance(value, dict) and type(value.get("schemaVersion")) is int
            and value["schemaVersion"] == 2 and isinstance(value.get("manifests"), list)
            and (index_media_type is None or index_media_type in _OCI_INDEX_TYPES)
        ):
            _fail("Docker image save index is invalid")
        _account_metadata(metadata_budget, estimate)
        return _JsonRecord(size=size, digest=hexdigest, raw=raw, value=value, kind=expected_kind)
    if root_config:
        record = _read_json_record(
            raw, size, remaining_memory=MAX_METADATA_BYTES - metadata_budget[0]
        )
        if record is None or record.kind != "config":
            _fail("Docker image save config is invalid")
        tokens, depth = _json_complexity(raw)
        if tokens > MAX_JSON_TOKENS or depth > MAX_JSON_DEPTH:
            _fail("Docker image save config is invalid")
        _account_metadata(metadata_budget, (2 * len(raw)) + (tokens * 128))
        return record
    if blob_match is not None:
        record = _read_json_record(
            raw,
            size,
            hexdigest,
            MAX_METADATA_BYTES - metadata_budget[0],
        )
        if record is not None:
            tokens, _depth = _json_complexity(raw)
            _account_metadata(metadata_budget, (2 * len(raw)) + (tokens * 128))
            return record
    return None


def _account_metadata(budget: list[int], amount: int) -> None:
    if amount < 0 or budget[0] + amount > MAX_METADATA_BYTES:
        _fail("Docker image save metadata exceeds its memory limit")
    budget[0] += amount


def _read_saved_members(stream: BinaryIO):
    reader = _BoundedReader(stream)
    paths: set[str] = set()
    file_records: dict[str, _JsonRecord] = {}
    blobs: dict[str, _JsonRecord] = {}
    member_types: dict[str, str] = {}
    metadata_budget = [0]
    count = 0
    ended = False
    while True:
        header = reader.exact(512)
        if header == b"\0" * 512:
            if reader.exact(512) != b"\0" * 512:
                _fail("Docker image save archive has an invalid end marker")
            ended = True
            break
        count += 1
        if count > MAX_MEMBER_COUNT:
            _fail("Docker image save archive contains too many members")
        _validate_header(header)
        path, is_directory = _member_path(header)
        if path in paths:
            _fail("Docker image save archive contains duplicate paths")
        paths.add(path)
        _account_metadata(metadata_budget, len(path.encode("utf-8")) + _MAX_MEMBER_OVERHEAD)
        typeflag = header[156:157]
        if typeflag in {b"\0", b"0"}:
            if is_directory:
                _fail("Docker image save archive has an invalid file type")
            member_types[path] = "file"
            size = _parse_octal(header[124:136])
            if size > MAX_ARCHIVE_BYTES:
                _fail("Docker image save archive exceeds its size limit")
            record = _read_member(reader, path, size, metadata_budget)
            if record is not None:
                if path.startswith("blobs/sha256/"):
                    blob_name = path.rsplit("/", 1)[1]
                    blobs[blob_name] = record
                else:
                    file_records[path] = record
            padding = (-size) % 512
            if padding and reader.exact(padding) != b"\0" * padding:
                _fail("Docker image save archive has invalid padding")
        elif typeflag == b"5" and is_directory:
            if _parse_octal(header[124:136]) != 0:
                _fail("Docker image save archive has an invalid directory")
            member_types[path] = "directory"
        else:
            _fail("Docker image save archive contains an unsupported member type")
    if not ended:
        _fail("Docker image save archive is truncated")
    while True:
        tail = reader.read(_CHUNK_BYTES)
        if not tail:
            break
        if any(tail):
            _fail("Docker image save archive has trailing data")
    return file_records, blobs, member_types


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail("Docker image save descriptor is invalid")
    return value


def _descriptor(record_map: dict[str, _JsonRecord], descriptor: object, allowed_types: set[str]) -> _JsonRecord:
    if not isinstance(descriptor, dict):
        _fail("Docker image save descriptor is invalid")
    media_type = descriptor.get("mediaType")
    if not isinstance(media_type, str) or media_type not in allowed_types:
        _fail("Docker image save descriptor type is unsupported")
    digest = _digest(descriptor.get("digest"))
    size = descriptor.get("size")
    if type(size) is not int or size < 0 or size > MAX_CONFIG_BYTES:
        _fail("Docker image save descriptor size is invalid")
    record = record_map.get(digest[7:])
    if record is None or record.digest != digest or record.size != size:
        _fail("Docker image save descriptor blob is missing or invalid")
    return record


def _resolve_oci_node(
    record: _JsonRecord,
    blobs: dict[str, _JsonRecord],
    stack: tuple[str, ...],
    depth: int,
    visit_count: list[int],
    resolved_nodes: dict[str, set[str]],
):
    if depth > 32 or record.digest in stack:
        _fail("Docker image save metadata graph is invalid")
    if record.digest in resolved_nodes:
        return resolved_nodes[record.digest]
    if record.kind == "manifest":
        manifest = record.value
        config = manifest.get("config")
        config_record = _descriptor(blobs, config, _OCI_CONFIG_TYPES)
        if config_record.kind != "config":
            _fail("Docker image save config descriptor is invalid")
        resolved_nodes[record.digest] = {config_record.digest}
        return resolved_nodes[record.digest]
    if record.kind != "index":
        _fail("Docker image save metadata graph is invalid")
    descriptors = record.value.get("manifests")
    if not isinstance(descriptors, list) or not descriptors:
        _fail("Docker image save index is empty")
    resolved: set[str] = set()
    for descriptor in descriptors:
        visit_count[0] += 1
        if visit_count[0] > MAX_MEMBER_COUNT:
            _fail("Docker image save metadata graph is too large")
        child = _descriptor(blobs, descriptor, _OCI_INDEX_TYPES | _OCI_MANIFEST_TYPES)
        expected_kind = "index" if descriptor["mediaType"] in _OCI_INDEX_TYPES else "manifest"
        if child.kind != expected_kind:
            _fail("Docker image save descriptor content does not match its type")
        resolved.update(_resolve_oci_node(
            child, blobs, (*stack, record.digest), depth + 1, visit_count, resolved_nodes
        ))
        if len(resolved) > MAX_MEMBER_COUNT:
            _fail("Docker image save metadata graph is too large")
    resolved_nodes[record.digest] = resolved
    return resolved


def _resolve_saved_config(file_records: dict[str, _JsonRecord], blobs: dict[str, _JsonRecord], member_types: dict[str, str], image_id: str) -> str:
    configs: set[str] = set()
    docker_manifest = file_records.get("manifest.json")
    if docker_manifest is not None:
        entries = docker_manifest.value
        if not entries:
            _fail("Docker image save manifest is empty")
        for entry in entries:
            if not isinstance(entry, dict):
                _fail("Docker image save manifest is invalid")
            config_path = entry.get("Config")
            layers = entry.get("Layers")
            if not isinstance(config_path, str) or not isinstance(layers, list):
                _fail("Docker image save manifest is invalid")
            config_path = _safe_reference_path(config_path)
            config_record = file_records.get(config_path)
            if config_record is None or config_record.kind != "config" or member_types.get(config_path) != "file":
                _fail("Docker image save config is missing")
            for layer in layers:
                if not isinstance(layer, str) or member_types.get(_safe_reference_path(layer)) != "file":
                    _fail("Docker image save layer is missing")
            if config_record.digest == image_id:
                configs.add(config_record.digest)

    index_record = file_records.get("index.json")
    if index_record is not None:
        resolved_nodes: dict[str, set[str]] = {}
        root_configs = _resolve_oci_node(index_record, blobs, (), 0, [0], resolved_nodes)
        if image_id in resolved_nodes:
            configs.update(resolved_nodes[image_id])
        if image_id in root_configs:
            configs.add(image_id)

    if not configs:
        _fail("Docker image save did not bind the selected image ID to config bytes")
    if len(configs) != 1:
        _fail("Docker image save config identity is ambiguous")
    return next(iter(configs))


def _safe_reference_path(path: str) -> str:
    try:
        encoded = path.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        _fail("Docker image save manifest path is invalid")
    parts = path.split("/")
    if (not encoded or len(encoded) > _MAX_MEMBER_NAME_BYTES or path.startswith("/")
            or "\\" in path or ":" in path
            or any(part in {"", ".", ".."} for part in parts)):
        _fail("Docker image save manifest path is unsafe")
    return path


def saved_image_config_digest(stream: BinaryIO, image_id: str) -> str:
    """Return the raw config digest bound to one immutable Docker image ID."""
    if not isinstance(image_id, str) or _SHA256.fullmatch(image_id) is None:
        _fail("Docker image ID is invalid")
    try:
        files, blobs, member_types = _read_saved_members(stream)
        return _resolve_saved_config(files, blobs, member_types, image_id)
    except DockerImageIdentityError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, AttributeError):
        raise DockerImageIdentityError("Docker image save archive is invalid") from None


def _validate_images(images: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    for pair in images:
        if len(result) >= MAX_IMAGE_COUNT:
            _fail("local image selection exceeds its limit")
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or not isinstance(pair[0], str) or not pair[0]
                or not isinstance(pair[1], str) or _SHA256.fullmatch(pair[1]) is None):
            _fail("local image selection is invalid")
        result.append((pair[0], pair[1]))
    if not result:
        return []
    names: dict[str, str] = {}
    for name, image_id in result:
        prior = names.setdefault(name, image_id)
        if prior != image_id:
            _fail("local image selection is ambiguous")
    return result


def _export_config_digest(executable: str, image_id: str, timeout: float) -> str:
    from .cancellation import terminate_owned_process

    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            [executable, "image", "save", image_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        _fail("Docker image save could not start")
    if process.stdout is None:
        process.kill()
        process.wait()
        _fail("Docker image save stream is unavailable")

    def stop_owned_process() -> None:
        try:
            terminate_owned_process(process, grace_seconds=0.1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    completed = threading.Event()
    result: list[str] = []
    failure: list[Exception] = []

    def read_stream() -> None:
        try:
            result.append(saved_image_config_digest(process.stdout, image_id))
        except (
            DockerImageIdentityError,
            OSError,
            EOFError,
            ValueError,
            TypeError,
            KeyError,
            OverflowError,
            RecursionError,
            AttributeError,
        ) as error:
            failure.append(error)
        finally:
            completed.set()

    reader = threading.Thread(target=read_stream, name="docker-image-save-reader", daemon=True)
    reader.start()
    try:
        remaining = max(0.0, deadline - time.monotonic())
        if not completed.wait(remaining):
            stop_owned_process()
            reader.join(timeout=1.0)
            _fail("Docker image save timed out")
        if failure:
            if process.poll() is None:
                stop_owned_process()
            if isinstance(failure[0], DockerImageIdentityError):
                raise failure[0]
            _fail("Docker image save archive is invalid")
        try:
            returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            stop_owned_process()
            reader.join(timeout=1.0)
            _fail("Docker image save timed out")
        if returncode != 0:
            _fail("Docker image save failed")
        if not result:
            _fail("Docker image save returned no config identity")
        return result[0]
    finally:
        if process.poll() is None:
            stop_owned_process()
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=1.0)


def docker_image_config_digests(images: Iterable[tuple[str, str]], timeout: float) -> list[tuple[str, str]]:
    """Export each immutable Docker image ID once and return its raw config digest."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_IMAGE_SAVE_TIMEOUT):
        _fail("Docker image save timeout is invalid")
    rows = _validate_images(images)
    if not rows:
        return []
    deadline = time.monotonic() + float(timeout)
    executable = shutil.which("docker")
    if not executable:
        _fail("Docker is unavailable")
    digests: dict[str, str] = {}
    for _name, image_id in rows:
        if image_id in digests:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("Docker image save timed out")
        digests[image_id] = _export_config_digest(executable, image_id, remaining)
    return [(name, digests[image_id]) for name, image_id in rows]
