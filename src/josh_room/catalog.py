import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .crypto import decrypt, encrypt
from .local_store import OBJECT_KEY


class CatalogConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Catalog:
    body: dict

    def __post_init__(self):
        if self.body.get("format_version") != 1 or not isinstance(self.body.get("revision"), int):
            raise ValueError("unsupported catalog format")
        for project in self.body.get("projects", {}).values():
            for snapshot in project.get("snapshots", {}).values():
                if not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
                    raise ValueError("catalog contains an invalid object key")
                if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
                    raise ValueError("catalog object digest mismatch")

    @classmethod
    def empty(cls):
        return cls({"format_version": 1, "revision": 0, "projects": {}})

    def add_snapshot(self, project_id: str, display_name: str, snapshot: dict):
        if not OBJECT_KEY.fullmatch(snapshot.get("object_key", "")):
            raise ValueError("catalog contains an invalid object key")
        if snapshot.get("ciphertext_sha256") != snapshot["object_key"].rsplit("/", 1)[-1]:
            raise ValueError("catalog object digest mismatch")
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

    def remove_project(self, project_id: str):
        if project_id not in self.body["projects"]:
            raise ValueError("room is not present in the encrypted catalog")
        body = json.loads(json.dumps(self.body))
        removed = body["projects"].pop(project_id)
        referenced = {
            snapshot["object_key"]
            for project in body["projects"].values()
            for snapshot in project["snapshots"].values()
        }
        removable = sorted({
            snapshot["object_key"]
            for snapshot in removed["snapshots"].values()
        } - referenced)
        body["revision"] += 1
        return Catalog(body), removable, len(removed["snapshots"])

    def update_if_revision(self, expected_revision: int, body: dict):
        if self.body["revision"] != expected_revision:
            raise CatalogConflict("stale catalog revision")
        return Catalog(body)


class CatalogFile:
    def __init__(self, path: Path, identity: Path | None = None):
        self.path = Path(path)
        self.identity = identity
        self.lock_path = self.path.with_name(".catalog.jroom.lock")

    def read(self) -> Catalog:
        if not self.path.exists():
            return Catalog.empty()
        if not self.identity:
            raise ValueError("catalog identity is required")
        return Catalog(json.loads(decrypt(self.path, [self.identity])))

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
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
