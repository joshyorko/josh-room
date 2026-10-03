from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .crypto import decrypt, encrypt
from .encryption_domain import validate_encryption_domain_id
from .local_store import OBJECT_KEY, ObjectRef
from .logical_jat import LogicalJat

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX
    _msvcrt = None

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOGICAL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")
_LOGICAL_FIELDS = {
    "snapshot_id", "payload_kind", "dimension_id", "encryption_domain_id", "room_id",
    "origin_room_id", "object_key", "ciphertext_sha256", "ciphertext_size", "created_at",
    "repository_id", "repository_format", "workspace_snapshot_id", "tree_id",
    "capture_policy_sha256", "workspace_signature", "signature_algorithm", "logical_bytes",
    "data_added", "data_added_packed", "component_refs",
}
_COMPONENT_NAMES = {"rcc_environment", "homebrew_recovery", "hauler_content"}


def _validate_logical_identifier(label: str, value: object) -> None:
    if not isinstance(value, str) or not _LOGICAL_IDENTIFIER.fullmatch(value):
        raise ValueError(f"catalog logical {label} is invalid")


def _validate_digest(label: str, value: object) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"catalog {label} is invalid")


def _validate_logical_snapshot(snapshot: dict) -> None:
    if set(snapshot) - _LOGICAL_FIELDS or _LOGICAL_FIELDS - {"origin_room_id"} - set(snapshot):
        raise ValueError("catalog logical snapshot fields are invalid")
    for name in ("snapshot_id", "dimension_id", "room_id"):
        _validate_logical_identifier(name, snapshot.get(name))
    if snapshot.get("payload_kind") != "room-store-v1":
        raise ValueError("catalog logical payload kind is invalid")
    validate_encryption_domain_id(snapshot.get("encryption_domain_id"))
    origin_room_id = snapshot.get("origin_room_id")
    if origin_room_id is not None:
        _validate_logical_identifier("origin room", origin_room_id)
        if origin_room_id == snapshot["room_id"]:
            raise ValueError("catalog logical origin room must differ from the copied Room")
    if not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
        raise ValueError("catalog contains an invalid object key")
    if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
        raise ValueError("catalog object digest mismatch")
    if type(snapshot.get("ciphertext_size")) is not int or snapshot["ciphertext_size"] < 1:
        raise ValueError("catalog logical ciphertext size is invalid")
    created_at = snapshot.get("created_at")
    if not isinstance(created_at, str) or _RFC3339.fullmatch(created_at) is None:
        raise ValueError("catalog logical created_at is invalid")
    try:
        datetime.fromisoformat(created_at)
    except ValueError as error:
        raise ValueError("catalog logical created_at is invalid") from error
    for name in ("repository_id", "workspace_snapshot_id", "tree_id", "capture_policy_sha256", "workspace_signature"):
        _validate_digest(name, snapshot.get(name))
    if type(snapshot.get("repository_format")) is not int or snapshot["repository_format"] != 2:
        raise ValueError("catalog logical repository format is invalid")
    if snapshot.get("signature_algorithm") != "josh-room-stat-v1":
        raise ValueError("catalog logical signature algorithm is invalid")
    for name in ("logical_bytes", "data_added", "data_added_packed"):
        value = snapshot.get(name)
        if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
            raise ValueError(f"catalog logical {name} is invalid")
    refs = snapshot.get("component_refs")
    if not isinstance(refs, dict) or set(refs) != _COMPONENT_NAMES:
        raise ValueError("catalog logical component refs are invalid")
    for ref in refs.values():
        if ref is None:
            continue
        if not isinstance(ref, dict) or set(ref) != {"snapshot_id", "tree_id"}:
            raise ValueError("catalog logical component ref is invalid")
        _validate_digest("component snapshot id", ref.get("snapshot_id"))
        _validate_digest("component tree id", ref.get("tree_id"))


def _logical_snapshot_record(
    project_id: str,
    descriptor: LogicalJat,
    object_ref: ObjectRef,
    workspace_signature: str,
) -> dict:
    if not isinstance(descriptor, LogicalJat):
        raise TypeError("logical descriptor must be a validated LogicalJat")
    body = descriptor.to_dict()
    _validate_logical_identifier("project", project_id)
    if body["room_id"] != project_id:
        raise ValueError("logical descriptor Room does not match the catalog project")
    if not isinstance(object_ref, ObjectRef):
        raise TypeError("logical descriptor object reference is invalid")
    if (
        not isinstance(object_ref.key, str)
        or not OBJECT_KEY.fullmatch(object_ref.key)
        or object_ref.sha256 != object_ref.key.rsplit("/", 1)[-1]
        or type(object_ref.size) is not int
        or object_ref.size < 1
    ):
        raise ValueError("logical descriptor object reference is invalid")
    _validate_digest("workspace signature", workspace_signature)
    workspace = body["workspace"]
    components = body["components"]
    record = {
        "snapshot_id": body["logical_jat_id"],
        "payload_kind": body["payload_kind"],
        "dimension_id": body["dimension_id"],
        "encryption_domain_id": body["encryption_domain_id"],
        "room_id": body["room_id"],
        "object_key": object_ref.key,
        "ciphertext_sha256": object_ref.sha256,
        "ciphertext_size": object_ref.size,
        "created_at": body["created_at"],
        "repository_id": workspace["repository_id"],
        "repository_format": workspace["repository_format"],
        "workspace_snapshot_id": workspace["snapshot_id"],
        "tree_id": workspace["tree_id"],
        "capture_policy_sha256": body["capture_policy_sha256"],
        "workspace_signature": workspace_signature,
        "signature_algorithm": "josh-room-stat-v1",
        "logical_bytes": workspace["logical_bytes"],
        "data_added": workspace["data_added"],
        "data_added_packed": workspace["data_added_packed"],
        "component_refs": {
            name: None if component is None else {
                "snapshot_id": component["snapshot"]["snapshot_id"],
                "tree_id": component["snapshot"]["tree_id"],
            }
            for name, component in components.items()
        },
    }
    if "origin_room_id" in body:
        record["origin_room_id"] = body["origin_room_id"]
    _validate_logical_snapshot(record)
    return record


def corroborate_logical_snapshot(project_id: str, snapshot: dict, descriptor: LogicalJat) -> LogicalJat:
    """Require the catalog index to agree with its complete encrypted descriptor."""
    if not isinstance(snapshot, dict):
        raise TypeError("catalog logical snapshot is invalid")
    object_ref = ObjectRef(
        snapshot.get("object_key"),
        snapshot.get("ciphertext_sha256"),
        snapshot.get("ciphertext_size"),
    )
    expected = _logical_snapshot_record(
        project_id,
        descriptor,
        object_ref,
        snapshot.get("workspace_signature"),
    )
    if snapshot != expected:
        raise ValueError("catalog logical snapshot does not match its descriptor")
    return descriptor


@contextmanager
def _exclusive_file_lock(handle):
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is None:
        raise RuntimeError("this platform has no supported file-lock implementation")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)
    try:
        yield
    finally:
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


class CatalogConflict(RuntimeError):
    pass


def _validate_identifier(label: str, value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"catalog {label} is invalid")


def _validate_v2_snapshot(snapshot: dict) -> None:
    for name in ("snapshot_id", "created_at"):
        if not isinstance(snapshot.get(name), str) or not snapshot[name]:
            raise ValueError(f"catalog snapshot {name} is invalid")
    _validate_identifier("snapshot", snapshot["snapshot_id"])
    try:
        datetime.fromisoformat(snapshot["created_at"])
    except ValueError as error:
        raise ValueError("catalog snapshot created_at is invalid") from error
    fingerprint = snapshot.get("workspace_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("catalog snapshot workspace fingerprint is invalid")
    origin = snapshot.get("origin_project_id")
    if origin is not None:
        _validate_identifier("origin project", origin)
    size = snapshot.get("ciphertext_size")
    if type(size) is not int or size < 1:
        raise ValueError("catalog snapshot ciphertext size is invalid")


@dataclass(frozen=True)
class Catalog:
    body: dict
    encryption_domain_id: str | None = None

    def __post_init__(self):
        body_domain = self.body.get("encryption_domain_id")
        if body_domain is not None:
            validate_encryption_domain_id(body_domain)
        if self.encryption_domain_id is not None:
            validate_encryption_domain_id(self.encryption_domain_id)
            if body_domain is not None and body_domain != self.encryption_domain_id:
                raise ValueError("catalog encryption domain mismatch")
            if body_domain is None:
                copied = json.loads(json.dumps(self.body))
                copied["encryption_domain_id"] = self.encryption_domain_id
                object.__setattr__(self, "body", copied)
        elif body_domain is not None:
            object.__setattr__(self, "encryption_domain_id", body_domain)
        version = self.body.get("format_version")
        revision = self.body.get("revision")
        if type(version) is not int or version not in {1, 2, 3} or type(revision) is not int or revision < 0:
            raise ValueError("unsupported catalog format")
        if not isinstance(self.body.get("projects"), dict):
            raise TypeError("catalog projects are invalid")
        if version in {2, 3}:
            _validate_identifier("dimension", self.body.get("dimension_id"))
        if version == 3:
            if set(self.body) != {"format_version", "dimension_id", "encryption_domain_id", "revision", "projects"}:
                raise ValueError("catalog v3 fields are invalid")
            validate_encryption_domain_id(self.body.get("encryption_domain_id"))
        for project_id, project in self.body["projects"].items():
            if version in {2, 3}:
                _validate_identifier("project", project_id)
            if not isinstance(project, dict) or not isinstance(project.get("snapshots"), dict):
                raise TypeError("catalog room is invalid")
            if version == 3:
                if (
                    set(project) != {"display_name", "latest", "snapshots"}
                    or not isinstance(project.get("display_name"), str)
                    or not project["display_name"]
                ):
                    raise ValueError("catalog v3 room fields are invalid")
                latest = project.get("latest")
                snapshots = project["snapshots"]
                if snapshots and (not isinstance(latest, str) or latest not in snapshots):
                    raise ValueError("catalog v3 latest snapshot is invalid")
                if not snapshots and latest is not None:
                    raise ValueError("catalog v3 latest snapshot is invalid")
            for snapshot_id, snapshot in project["snapshots"].items():
                if version in {2, 3}:
                    _validate_identifier("snapshot", snapshot_id)
                if not isinstance(snapshot, dict) or not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
                    raise ValueError("catalog contains an invalid object key")
                if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
                    raise ValueError("catalog object digest mismatch")
                if version == 2:
                    _validate_v2_snapshot(snapshot)
                elif version == 3:
                    if "payload_kind" in snapshot:
                        _validate_logical_snapshot(snapshot)
                        if snapshot["snapshot_id"] != snapshot_id:
                            raise ValueError("catalog logical snapshot id mismatch")
                        if snapshot["dimension_id"] != self.body["dimension_id"] or snapshot["encryption_domain_id"] != self.body["encryption_domain_id"]:
                            raise ValueError("catalog logical Dimension binding mismatch")
                        if snapshot["room_id"] != project_id:
                            raise ValueError("catalog logical Room binding mismatch")
                    else:
                        _validate_v2_snapshot(snapshot)

    @classmethod
    def empty(cls, dimension_id: str | None = None, encryption_domain_id: str | None = None):
        if encryption_domain_id is not None:
            validate_encryption_domain_id(encryption_domain_id)
            if dimension_id is None:
                return cls({"format_version": 1, "revision": 0, "projects": {}}, encryption_domain_id=encryption_domain_id)
            _validate_identifier("dimension", dimension_id)
            return cls({"format_version": 2, "dimension_id": dimension_id, "revision": 0, "projects": {}}, encryption_domain_id=encryption_domain_id)
        if dimension_id is None:
            return cls({"format_version": 1, "revision": 0, "projects": {}})
        _validate_identifier("dimension", dimension_id)
        return cls({"format_version": 2, "dimension_id": dimension_id, "revision": 0, "projects": {}})

    @classmethod
    def from_body(cls, body: dict, dimension_id: str | None = None, encryption_domain_id: str | None = None):
        value = json.loads(json.dumps(body))
        if value.get("format_version") == 3 and "encryption_domain_id" not in value:
            raise ValueError("catalog v3 encryption domain is required")
        if encryption_domain_id is not None:
            validate_encryption_domain_id(encryption_domain_id)
            existing_domain = value.get("encryption_domain_id")
            if existing_domain is not None and existing_domain != encryption_domain_id:
                raise ValueError("catalog encryption domain mismatch")
            value["encryption_domain_id"] = encryption_domain_id
        if type(value.get("format_version")) is int and value.get("format_version") == 1 and dimension_id is not None:
            _validate_identifier("dimension", dimension_id)
            value["format_version"] = 2
            value["dimension_id"] = dimension_id
            for project in value.get("projects", {}).values():
                for snapshot in project.get("snapshots", {}).values():
                    snapshot.setdefault("created_at", "1970-01-01T00:00:00+00:00")
                    snapshot.setdefault("workspace_fingerprint", "0" * 64)
        catalog = cls(value)
        if dimension_id is not None and catalog.dimension_id != dimension_id:
            raise ValueError("catalog Dimension mismatch")
        return catalog

    @property
    def dimension_id(self) -> str | None:
        return self.body.get("dimension_id")

    def add_snapshot(self, project_id: str, display_name: str, snapshot: dict):
        if not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
            raise ValueError("catalog contains an invalid object key")
        if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
            raise ValueError("catalog object digest mismatch")
        if self.body["format_version"] in {2, 3}:
            _validate_identifier("project", project_id)
            _validate_v2_snapshot(snapshot)
        body = json.loads(json.dumps(self.body))
        if body["format_version"] in {2, 3}:
            snapshot = dict(snapshot)
            snapshot.setdefault("origin_project_id", project_id)
        project = body["projects"].setdefault(project_id, {"display_name": display_name, "latest": None, "snapshots": {}})
        project["display_name"] = display_name
        project["snapshots"][snapshot["snapshot_id"]] = snapshot
        project["latest"] = snapshot["snapshot_id"]
        body["revision"] += 1
        return Catalog(body)

    def add_logical_snapshot(
        self,
        project_id: str,
        display_name: str,
        descriptor: LogicalJat,
        object_ref: ObjectRef,
        workspace_signature: str,
    ) -> Catalog:
        if self.body["format_version"] not in {2, 3}:
            raise ValueError("logical snapshots require a Dimension catalog")
        if self.dimension_id is None or self.encryption_domain_id is None:
            raise ValueError("logical snapshots require an encryption domain binding")
        record = _logical_snapshot_record(project_id, descriptor, object_ref, workspace_signature)
        if record["dimension_id"] != self.dimension_id:
            raise ValueError("logical descriptor Dimension does not match the catalog")
        if record["encryption_domain_id"] != self.encryption_domain_id:
            raise ValueError("logical descriptor encryption domain does not match the catalog")
        body = json.loads(json.dumps(self.body))
        body["format_version"] = 3
        body["encryption_domain_id"] = self.encryption_domain_id
        project = body["projects"].setdefault(project_id, {"display_name": display_name, "latest": None, "snapshots": {}})
        project["display_name"] = display_name
        project["snapshots"][record["snapshot_id"]] = record
        project["latest"] = record["snapshot_id"]
        body["revision"] += 1
        return Catalog(body)

    def latest(self, project_id: str) -> dict:
        project = self.body["projects"][project_id]
        return project["snapshots"][project["latest"]]

    def resolve_snapshot(self, project_id: str, snapshot_id: str) -> dict:
        project = self.body["projects"][project_id]
        resolved = project["latest"] if snapshot_id == "latest" else snapshot_id
        try:
            return project["snapshots"][resolved]
        except KeyError as error:
            raise ValueError("snapshot is not present in the encrypted catalog") from error

    def remove_project(self, project_id: str):
        if project_id not in self.body["projects"]:
            raise ValueError("room is not present in the encrypted catalog")
        body = json.loads(json.dumps(self.body))
        removed = body["projects"].pop(project_id)
        referenced = {snapshot["object_key"] for project in body["projects"].values() for snapshot in project["snapshots"].values()}
        removable = sorted({snapshot["object_key"] for snapshot in removed["snapshots"].values()} - referenced)
        body["revision"] += 1
        return Catalog(body), removable, len(removed["snapshots"])

    def remove_snapshot(self, project_id: str, snapshot_id: str):
        if project_id not in self.body["projects"]:
            raise ValueError("room is not present in the encrypted catalog")
        project = self.body["projects"][project_id]
        if snapshot_id not in project["snapshots"]:
            raise ValueError("snapshot is not present in the encrypted catalog")
        body = json.loads(json.dumps(self.body))
        if len(project["snapshots"]) == 1:
            removed = body["projects"].pop(project_id)["snapshots"][snapshot_id]
            referenced = {candidate["object_key"] for remaining in body["projects"].values() for candidate in remaining["snapshots"].values()}
            removable = [] if removed["object_key"] in referenced else [removed["object_key"]]
            body["revision"] += 1
            return Catalog(body), removable, True
        changed = body["projects"][project_id]
        removed = changed["snapshots"].pop(snapshot_id)
        if changed["latest"] == snapshot_id:
            changed["latest"] = next(reversed(changed["snapshots"]))
        referenced = {snapshot["object_key"] for candidate in body["projects"].values() for snapshot in candidate["snapshots"].values()}
        removable = [] if removed["object_key"] in referenced else [removed["object_key"]]
        body["revision"] += 1
        return Catalog(body), removable, False

    def update_if_revision(self, expected_revision: int, body: dict):
        if type(expected_revision) is not int or expected_revision < 0 or self.body["revision"] != expected_revision:
            raise CatalogConflict("stale catalog revision")
        return Catalog.from_body(body, self.dimension_id, self.encryption_domain_id)


class CatalogFile:
    def __init__(self, path: Path, identity: Path | None = None, dimension_id: str | None = None):
        self.path = Path(path)
        self.identity = identity
        self.dimension_id = dimension_id
        self.lock_path = self.path.with_name(".catalog.jroom.lock")

    def read(self) -> Catalog:
        if not self.path.exists():
            return Catalog.empty(self.dimension_id)
        if not self.identity:
            raise ValueError("catalog identity is required")
        return Catalog.from_body(json.loads(decrypt(self.path, [self.identity])), self.dimension_id)

    def write(self, catalog: Catalog, recipients: list[str]) -> None:
        self._publish(catalog, recipients)

    def update_if_revision(self, expected_revision: int, catalog: Catalog, recipients: list[str]) -> Catalog:
        if type(expected_revision) is not int or expected_revision < 0:
            raise CatalogConflict("stale catalog revision")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock, _exclusive_file_lock(lock):
            current = self.read()
            if current.body["revision"] != expected_revision:
                raise CatalogConflict("stale catalog revision")
            self._publish(catalog, recipients)
            return catalog

    def _publish(self, catalog: Catalog, recipients: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".catalog.jroom.", dir=self.path.parent)
        os.close(fd)
        temp = Path(temp_name)
        try:
            encrypt(json.dumps(catalog.body, sort_keys=True).encode(), recipients, temp)
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
