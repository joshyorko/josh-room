import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path

MAX_MANIFEST_SIZE = 1024 * 1024
MAX_PAYLOAD_SIZE = 8 * 1024 * 1024 * 1024
MAX_ENVELOPE_SIZE = MAX_PAYLOAD_SIZE + MAX_MANIFEST_SIZE + 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EnvelopeError(ValueError):
    pass


def build_envelope(manifest: dict, payload: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        payload_path = root / "payload.haul.tar.zst"
        payload_path.write_bytes(payload)
        output = root / "envelope.tar"
        build_envelope_file(manifest, payload_path, output)
        return output.read_bytes()


def build_envelope_file(manifest: dict, payload: Path, output: Path) -> None:
    if manifest.get("format_version") != 1:
        raise EnvelopeError("unsupported envelope version")
    if set(manifest) - {"format_version", "project_id", "snapshot_id", "created_at", "payload", "source", "environment_artifact"}:
        raise EnvelopeError("unknown manifest field")
    payload_size = payload.stat().st_size
    payload_digest = _file_digest(payload)
    _validate_manifest(manifest, payload_size, digest=payload_digest)
    manifest_body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with tarfile.open(temp, mode="w") as archive:
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_body)
            manifest_info.mode = 0o600
            archive.addfile(manifest_info, io.BytesIO(manifest_body))
            payload_info = tarfile.TarInfo("payload.haul.tar.zst")
            payload_info.size = payload_size
            payload_info.mode = 0o600
            with payload.open("rb") as source:
                archive.addfile(payload_info, source)
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        temp.chmod(0o600)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)


def read_envelope(data: bytes) -> tuple[dict, bytes]:
    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        envelope = root / "envelope.tar"
        envelope.write_bytes(data)
        payload = root / "payload.haul.tar.zst"
        manifest = read_envelope_file(envelope, payload)
        return manifest, payload.read_bytes()


def read_envelope_file(envelope: Path, payload_output: Path) -> dict:
    if envelope.stat().st_size > MAX_ENVELOPE_SIZE:
        raise EnvelopeError("envelope exceeds maximum size")
    payload_output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{payload_output.name}.", dir=payload_output.parent)
    temp = Path(temp_name)
    try:
        with tarfile.open(envelope, mode="r:") as archive:
            manifest, payload_member = _read_headers(archive)
            source = archive.extractfile(payload_member)
            if source is None:
                raise EnvelopeError("payload is unreadable")
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PAYLOAD_SIZE:
                        raise EnvelopeError("payload too large")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total != payload_member.size:
                raise EnvelopeError("payload read is truncated")
            _validate_manifest(manifest, total, digest=digest.hexdigest())
        temp.chmod(0o600)
        os.replace(temp, payload_output)
        return manifest
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)


def _read_headers(archive: tarfile.TarFile) -> tuple[dict, tarfile.TarInfo]:
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
        if set(manifest) - {"format_version", "project_id", "snapshot_id", "created_at", "payload", "source", "environment_artifact"}:
            raise EnvelopeError("unknown manifest field")
        payload_member = archive.getmember("payload.haul.tar.zst")
        if payload_member.size > MAX_PAYLOAD_SIZE:
            raise EnvelopeError("payload too large")
        return manifest, payload_member


def _validate_manifest(
    manifest: dict, payload_size: int, payload: bytes | None = None, digest: str | None = None
) -> None:
    payload_meta = manifest.get("payload")
    artifact = manifest.get("environment_artifact")
    if artifact is not None:
        required = {"artifact", "specification_digest", "legacy_blueprint_key", "archive", "archive_sha256", "archive_size", "rcc_version", "robot", "provider", "acquired"}
        paths = ("archive", "robot")
        if (set(artifact) != required or any(not isinstance(artifact[k], str) for k in required - {"archive_size", "acquired"}) or not isinstance(artifact["archive_size"], int) or artifact["archive_size"] <= 0 or not isinstance(artifact["acquired"], bool) or not SHA256.fullmatch(artifact["artifact"].removeprefix("sha256:")) or not SHA256.fullmatch(artifact["specification_digest"].removeprefix("sha256:")) or not SHA256.fullmatch(artifact["archive_sha256"]) or artifact["rcc_version"] != "v18.19.2" or not artifact["legacy_blueprint_key"] or any(not artifact[k] or Path(artifact[k]).is_absolute() or "." in Path(artifact[k]).parts or ".." in Path(artifact[k]).parts for k in paths) or artifact["provider"] != "local"):
            raise EnvelopeError("invalid environment artifact metadata")
    if not isinstance(payload_meta, dict) or not isinstance(payload_meta.get("size"), int) or payload_meta["size"] < 0 or payload_meta["size"] > MAX_PAYLOAD_SIZE:
        raise EnvelopeError("invalid payload size")
    if payload_meta["size"] != payload_size:
        raise EnvelopeError("payload size mismatch")
    if not SHA256.fullmatch(payload_meta.get("sha256", "")):
        raise EnvelopeError("invalid payload digest")
    if payload is not None and payload_meta["sha256"] != hashlib.sha256(payload).hexdigest():
        raise EnvelopeError("payload digest mismatch")
    if digest is not None and payload_meta["sha256"] != digest:
        raise EnvelopeError("payload digest mismatch")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
