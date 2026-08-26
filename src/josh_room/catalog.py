import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .crypto import decrypt, encrypt
from .local_store import OBJECT_KEY

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    size = snapshot.get("ciphertext_size")
    if type(size) is not int or size < 1:
        raise ValueError("catalog snapshot ciphertext size is invalid")


@dataclass(frozen=True)
class Catalog:
    body: dict

    def __post_init__(self):
        version = self.body.get("format_version")
        if version not in {1, 2} or not isinstance(self.body.get("revision"), int):
            raise ValueError("unsupported catalog format")
        if not isinstance(self.body.get("projects"), dict):
            raise TypeError("catalog projects are invalid")
        if version == 2:
            _validate_identifier("dimension", self.body.get("dimension_id"))
        for project_id, project in self.body["projects"].items():
            if version == 2:
                _validate_identifier("project", project_id)
            if not isinstance(project, dict) or not isinstance(project.get("snapshots"), dict):
                raise TypeError("catalog room is invalid")
            for snapshot_id, snapshot in project["snapshots"].items():
                if version == 2:
                    _validate_identifier("snapshot", snapshot_id)
                if not isinstance(snapshot, dict) or not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
                    raise ValueError("catalog contains an invalid object key")
                if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
                    raise ValueError("catalog object digest mismatch")
                if version == 2:
                    _validate_v2_snapshot(snapshot)

    @classmethod
    def empty(cls, dimension_id: str | None = None):
        if dimension_id is None:
            return cls({"format_version": 1, "revision": 0, "projects": {}})
        _validate_identifier("dimension", dimension_id)
        return cls({"format_version": 2, "dimension_id": dimension_id, "revision": 0, "projects": {}})

    @classmethod
    def from_body(cls, body: dict, dimension_id: str | None = None):
        value = json.loads(json.dumps(body))
        if value.get("format_version") == 1 and dimension_id is not None:
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
        if self.body["format_version"] == 2:
            _validate_identifier("project", project_id)
            _validate_v2_snapshot(snapshot)
        body = json.loads(json.dumps(self.body))
        project = body["projects"].setdefault(project_id, {"display_name": display_name, "latest": None, "snapshots": {}})
        project["display_name"] = display_name
        project["snapshots"][snapshot["snapshot_id"]] = snapshot
        project["latest"] = snapshot["snapshot_id"]
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
        if self.body["revision"] != expected_revision:
            raise CatalogConflict("stale catalog revision")
        return Catalog.from_body(body, self.dimension_id)


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
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
            with temp.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
