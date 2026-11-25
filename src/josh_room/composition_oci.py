"""Verify a Hauler export against the exact OCI manifests approved for a child."""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile

from .room_store_hauler import MAX_ARCHIVE_BYTES
from .room_store_hauler_runner import _brew_decoded_stream

_BLOB = re.compile(r"blobs/sha256/([a-f0-9]{64})\Z")
_TOP = {"oci-layout", "index.json", "manifest.json", "audit.log", "store.json", "store.json.lock"}
_MAX_MEMBERS = 100_000
_MAX_JSON = 4 * 1024 * 1024
_MANIFEST = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
_INDEX = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


def _edges(document, media_type):
    if not isinstance(document, dict) or document.get("schemaVersion") != 2:
        raise ValueError("invalid OCI manifest")
    if media_type in _INDEX:
        rows = document.get("manifests")
    else:
        layers = document.get("layers")
        if not isinstance(layers, list):
            raise ValueError("invalid OCI layers")
        rows = [document.get("config"), *layers]
    if not isinstance(rows, list):
        raise ValueError("invalid OCI descriptors")  # noqa: TRY004 - invalid archive data
    if "subject" in document:
        rows = [*rows, document["subject"]]
    out = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("digest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["digest"])
                or type(row.get("size")) is not int or row["size"] < 0
                or not isinstance(row.get("mediaType"), str) or "urls" in row):
            raise ValueError("invalid or external OCI descriptor")
        out.append((row["digest"], row["size"], row["mediaType"]))
    return out


def _approved_index(document, references):
    manifest_rows = []
    matched = set()
    for row in document["manifests"]:
        digest = row["digest"]
        annotations = row.get("annotations", {})
        if not isinstance(annotations, dict):
            raise ValueError("invalid OCI manifest annotations")  # noqa: TRY004 - invalid archive data
        names = (annotations.get(key) for key in (
            "org.opencontainers.image.ref.name", "io.containerd.image.name", "hauler.dev/original-ref"
        ))
        by_hash = {hashlib.sha256(name.encode()).hexdigest(): name for name in names
                   if isinstance(name, str) and len(name) <= 4096}
        platform = row.get("platform")
        selected_platform = None
        if platform:
            if (not isinstance(platform, dict) or any(not isinstance(platform.get(key), str)
                    for key in ("os", "architecture")) or not isinstance(platform.get("variant", ""), str)):
                raise ValueError("invalid OCI manifest platform")
            selected_platform = {key: platform[key] for key in ("os", "architecture", "variant") if key in platform}
        platform_name = "/".join(selected_platform.values()) if selected_platform else "-"
        platform_hash = hashlib.sha256(platform_name.encode()).hexdigest()
        candidates = [(i, ref) for i, ref in enumerate(references) if ref["digest"] == digest
                      and ("reference_sha256" not in ref or ref["reference_sha256"] in by_hash)
                      and ("platform_sha256" not in ref or ref["platform_sha256"] == platform_hash)
                      and ("media_type" not in ref or ref["media_type"] == row["mediaType"])]
        if len(candidates) != 1 or candidates[0][0] in matched:
            raise ValueError("OCI tag, media type or platform differs from approval")
        index, ref = candidates[0]
        matched.add(index)
        entry = {key: row[key] for key in ("digest", "size", "mediaType")}
        if "reference_sha256" in ref:
            entry["annotations"] = {"org.opencontainers.image.ref.name": by_hash[ref["reference_sha256"]]}
        if selected_platform:
            if "platform_sha256" not in ref:
                raise ValueError("unapproved OCI manifest platform")
            entry["platform"] = selected_platform
        manifest_rows.append(entry)
    if len(matched) != len(references):
        raise ValueError("OCI archive omits approved references")
    return {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": sorted(manifest_rows, key=lambda row: json.dumps(row, sort_keys=True))}


def canonicalize_oci_archive(path, references):
    """Write only the content-addressed graph and its approved OCI names."""
    import zstandard

    verify_oci_archive(path, references, producer_output=True)
    output = path.with_suffix(".normalized")
    with (_brew_decoded_stream(path) as decoded, tarfile.open(fileobj=decoded, mode="r|") as source,
          output.open("xb") as raw, zstandard.ZstdCompressor().stream_writer(raw) as compressed,
          tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as target):
        for member in source:
            if member.isdir() or member.name not in {"index.json", "oci-layout"} and not _BLOB.fullmatch(member.name):
                continue
            info = tarfile.TarInfo(member.name)
            info.size = member.size
            data = source.extractfile(member)
            if member.name in {"index.json", "oci-layout"}:
                document = json.load(data)
                if member.name == "index.json":
                    document = _approved_index(document, references)
                encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
                info.size = len(encoded)
                data = io.BytesIO(encoded)
            target.addfile(info, data)
    output.replace(path)


def verify_oci_archive(path, references, *, producer_output=False):
    """Hash each blob, walk the index transitively and reject unapproved payload.

    Read ordinary tar records directly so an archive cannot allocate unbounded
    extension headers, extract host paths or smuggle an appended tar stream.
    """
    seen, blobs, children = set(), {}, {}
    roots = None
    total = edge_count = 0
    with _brew_decoded_stream(path) as decoded:
        while True:
            header = decoded.read(512)
            if header == bytes(512):
                while block := decoded.read(65536):
                    total += len(block)
                    if total > MAX_ARCHIVE_BYTES or any(block):
                        raise ValueError("unexpected trailing OCI archive data")
                break
            try:
                member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (ValueError, tarfile.HeaderError, UnicodeError):
                raise ValueError("invalid OCI archive header") from None
            name, size = member.name, member.size
            total += 512 + ((size + 511) // 512) * 512
            if size < 0 or total > MAX_ARCHIVE_BYTES or name in seen or len(seen) >= _MAX_MEMBERS:
                raise ValueError("invalid or oversized OCI archive inventory")
            seen.add(name)
            if member.isdir() and name in {"blobs", "blobs/sha256"} and not size:
                continue
            blob = _BLOB.fullmatch(name)
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or (not blob and name not in (_TOP if producer_output else {"index.json", "oci-layout"})):
                raise ValueError("unexpected OCI archive member")
            if name in _TOP and size > _MAX_JSON:
                raise ValueError("OCI export metadata exceeds the limit")
            digest = hashlib.sha256()
            content = bytearray() if size <= _MAX_JSON and (name in {"index.json", "oci-layout"} or blob) else None
            remaining = size
            while remaining:
                block = decoded.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("incomplete OCI blob")
                remaining -= len(block)
                digest.update(block)
                if content is not None:
                    content.extend(block)
            padding = (-size) % 512
            if decoded.read(padding) != bytes(padding):
                raise ValueError("invalid OCI archive padding")
            if blob and digest.hexdigest() != blob[1]:
                raise ValueError("OCI blob digest differs from its name")
            if blob:
                blobs["sha256:" + blob[1]] = size
            if content is None:
                continue
            try:
                document = json.loads(content)
            except (ValueError, UnicodeError, RecursionError):
                if not blob:
                    raise ValueError("invalid OCI layout metadata") from None
                continue
            if name == "oci-layout" and document != {"imageLayoutVersion": "1.0.0"}:
                raise ValueError("unsupported OCI layout")
            if name == "index.json":
                roots = _edges(document, "application/vnd.oci.image.index.v1+json")
                approved_index = _approved_index(document, references)
                if not producer_output and approved_index != document:
                    raise ValueError("OCI index includes unapproved metadata")
            elif blob and isinstance(document, dict) and document.get("schemaVersion") == 2:
                media_type = document.get("mediaType")
                if isinstance(media_type, str) and media_type in _MANIFEST | _INDEX:
                    edges = _edges(document, media_type)
                    edge_count += len(edges)
                    if edge_count > _MAX_MEMBERS:
                        raise ValueError("OCI manifest inventory exceeds limit")
                    children["sha256:" + blob[1]] = (media_type, edges)
    if "oci-layout" not in seen or not roots or {row[0] for row in roots} != {ref["digest"] for ref in references}:
        raise ValueError("OCI root manifests differ from approved inventory")
    if any(row[2] not in _MANIFEST | _INDEX for row in roots):
        raise ValueError("unsupported OCI root media type")
    reached = set()
    pending = list(roots)
    while pending:
        identity, size, kind = pending.pop()
        if blobs.get(identity) != size:
            raise ValueError("OCI referenced blob is missing or has a different size")
        if kind in _MANIFEST | _INDEX:
            node = children.get(identity)
            if node is None or node[0] != kind:
                raise ValueError("missing or invalid nested OCI manifest")
            if identity not in reached:
                pending.extend(node[1])
        reached.add(identity)
    if reached != set(blobs):
        raise ValueError("OCI archive contains unreferenced payload")
    return approved_index["manifests"]
