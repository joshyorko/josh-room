import hashlib
import io
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_OBJECT_SIZE = 8 * 1024 * 1024 * 1024
OBJECT_KEY = re.compile(r"^objects/sha256/([0-9a-f]{64})$")


@dataclass(frozen=True)
class ObjectRef:
    key: str
    sha256: str
    size: int


class ImmutableLocalStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, body: bytes) -> ObjectRef:
        digest = hashlib.sha256(body).hexdigest()
        key = f"objects/sha256/{digest}"
        return self.put_at_key(key, body)

    def put_at_key(self, key: str, body: bytes) -> ObjectRef:
        return self._put_stream(key, io.BytesIO(body), len(body))

    def put_file(self, source: Path) -> ObjectRef:
        return self._put_stream(None, source.open("rb"), source.stat().st_size)

    def _put_stream(self, key: str | None, source, expected_size: int) -> ObjectRef:
        if expected_size > MAX_OBJECT_SIZE:
            raise ValueError("object exceeds maximum size")
        partial_root = self.root / "objects" / "sha256"
        partial_root.mkdir(parents=True, exist_ok=True)
        temp = partial_root / f".object.{uuid.uuid4().hex}.partial"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            with source, os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_OBJECT_SIZE:
                        raise ValueError("object exceeds maximum size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != expected_size:
                raise ValueError("object source size changed during publication")
            observed = digest.hexdigest()
            resolved_key = key or f"objects/sha256/{observed}"
            match = OBJECT_KEY.fullmatch(resolved_key)
            if not match:
                raise ValueError("invalid object key")
            if observed != match.group(1):
                raise ValueError("object key does not match content digest")
            path = self.root / resolved_key
            os.link(temp, path)
            return ObjectRef(resolved_key, observed, size)
        finally:
            temp.unlink(missing_ok=True)

    def download_file(self, key: str, destination: Path, expected_digest: str, expected_size: int) -> None:
        match = OBJECT_KEY.fullmatch(key)
        if not match:
            raise ValueError("invalid object key")
        path = self.root / key
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
            raise ValueError("local object size mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source, os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_OBJECT_SIZE:
                        raise ValueError("object exceeds maximum size")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != expected_size or digest.hexdigest() != expected_digest or expected_digest != match.group(1):
                raise ValueError("local object digest mismatch")
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def get(self, key: str, expected_digest: str | None = None, expected_size: int | None = None) -> bytes:
        if not OBJECT_KEY.fullmatch(key):
            raise ValueError("invalid object key")
        path = self.root / key
        size = path.stat().st_size
        if size > MAX_OBJECT_SIZE or expected_size is not None and size != expected_size:
            raise ValueError("local object size mismatch")
        body = path.read_bytes()
        if expected_digest is not None and hashlib.sha256(body).hexdigest() != expected_digest:
            raise ValueError("local object digest mismatch")
        return body

    def delete(self, key: str) -> None:
        if not OBJECT_KEY.fullmatch(key):
            raise ValueError("invalid object key")
        path = self.root / key
        if path.is_symlink():
            raise ValueError("local object must not be a symlink")
        path.unlink(missing_ok=True)
