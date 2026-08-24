import hashlib
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
        match = OBJECT_KEY.fullmatch(key)
        if not match:
            raise ValueError("invalid object key")
        digest = hashlib.sha256(body).hexdigest()
        if digest != match.group(1):
            raise ValueError("object key does not match content digest")
        if len(body) > MAX_OBJECT_SIZE:
            raise ValueError("object exceeds maximum size")
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        return ObjectRef(key, digest, len(body))

    def get(self, key: str) -> bytes:
        if not OBJECT_KEY.fullmatch(key):
            raise ValueError("invalid object key")
        path = self.root / key
        size = path.stat().st_size
        if size > MAX_OBJECT_SIZE:
            raise ValueError("object exceeds maximum size")
        return path.read_bytes()
