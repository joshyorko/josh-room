import hashlib
import io
import json
import re
import tarfile

MAX_MANIFEST_SIZE = 1024 * 1024
MAX_PAYLOAD_SIZE = 8 * 1024 * 1024 * 1024
MAX_ENVELOPE_SIZE = MAX_PAYLOAD_SIZE + MAX_MANIFEST_SIZE + 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EnvelopeError(ValueError):
    pass


def build_envelope(manifest: dict, payload: bytes) -> bytes:
    if manifest.get("format_version") != 1:
        raise EnvelopeError("unsupported envelope version")
    if set(manifest) - {"format_version", "project_id", "snapshot_id", "created_at", "payload", "source"}:
        raise EnvelopeError("unknown manifest field")
    _validate_manifest(manifest, len(payload), payload)
    manifest_body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, body in (("manifest.json", manifest_body), ("payload.haul.tar.zst", payload)):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def read_envelope(data: bytes) -> tuple[dict, bytes]:
    if len(data) > MAX_ENVELOPE_SIZE:
        raise EnvelopeError("envelope exceeds maximum size")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) != 2 or {member.name for member in members} != {"manifest.json", "payload.haul.tar.zst"}:
            raise EnvelopeError("envelope must contain exactly manifest.json and payload.haul.tar.zst")
        for member in members:
            if not member.isreg():
                raise EnvelopeError(f"unsupported member type: {member.name}")
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise EnvelopeError(f"unsafe member path: {member.name}")
        manifest_member = archive.getmember("manifest.json")
        if manifest_member.size > MAX_MANIFEST_SIZE:
            raise EnvelopeError("manifest too large")
        try:
            manifest = json.loads(archive.extractfile(manifest_member).read(MAX_MANIFEST_SIZE + 1))
        except (json.JSONDecodeError, TypeError) as error:
            raise EnvelopeError("invalid manifest JSON") from error
        if manifest.get("format_version") != 1:
            raise EnvelopeError("unsupported envelope version")
        if set(manifest) - {"format_version", "project_id", "snapshot_id", "created_at", "payload", "source"}:
            raise EnvelopeError("unknown manifest field")
        payload_member = archive.getmember("payload.haul.tar.zst")
        if payload_member.size > MAX_PAYLOAD_SIZE:
            raise EnvelopeError("payload too large")
        payload = archive.extractfile(payload_member).read(MAX_PAYLOAD_SIZE + 1)
        if len(payload) != payload_member.size:
            raise EnvelopeError("payload read is truncated")
        _validate_manifest(manifest, len(payload), payload)
        return manifest, payload


def _validate_manifest(manifest: dict, payload_size: int, payload: bytes | None = None) -> None:
    payload_meta = manifest.get("payload")
    if not isinstance(payload_meta, dict) or not isinstance(payload_meta.get("size"), int) or payload_meta["size"] < 0 or payload_meta["size"] > MAX_PAYLOAD_SIZE:
        raise EnvelopeError("invalid payload size")
    if payload_meta["size"] != payload_size:
        raise EnvelopeError("payload size mismatch")
    if not SHA256.fullmatch(payload_meta.get("sha256", "")):
        raise EnvelopeError("invalid payload digest")
    if payload is not None and payload_meta["sha256"] != hashlib.sha256(payload).hexdigest():
        raise EnvelopeError("payload digest mismatch")
