"""Real local Restic + age, with synthetic SQLite S3 object transport.

These tests establish actual encrypted capture/restore, not live-provider or
installed VSIX qualification. All source names and identities are synthetic.
"""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from josh_room.composition import MANIFEST
from josh_room.composition_authority import CompositionAuthority
from josh_room.composition_lifecycle import (
    capture_plan,
    hydrate_composition,
    save_composition,
)
from josh_room.config import DimensionConfig
from josh_room.crypto import derive_recipient, generate_identity
from josh_room.restic_store import ResticStore

NAMESPACE = "1" * 32
DOMAIN = "00000000-0000-4000-8000-000000000001"
pytestmark = pytest.mark.integration


class SQLiteS3:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "create table if not exists objects(bucket text, key text, body blob, primary key(bucket,key))"
        )
        self.writes = 0

    def get_object(self, Bucket, Key):
        row = self.db.execute(
            "select body from objects where bucket=? and key=?", (Bucket, Key)
        ).fetchone()
        if row is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(row[0]), "ETag": hashlib.sha256(row[0]).hexdigest()}

    def list_objects_v2(self, Bucket, Prefix, Delimiter, MaxKeys, ContinuationToken=None):
        keys = [key for (key,) in self.db.execute("select key from objects where bucket=?", (Bucket,))
                if key.startswith(Prefix)]
        prefixes = sorted({Prefix + key[len(Prefix):].split(Delimiter, 1)[0] + Delimiter
                           for key in keys if Delimiter in key[len(Prefix):]})
        offset = int(ContinuationToken or 0)
        end = offset + MaxKeys
        return {"CommonPrefixes": [{"Prefix": prefix} for prefix in prefixes[offset:end]],
                "IsTruncated": end < len(prefixes), "NextContinuationToken": str(end)}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None):
        with self.db:
            row = self.db.execute(
                "select body from objects where bucket=? and key=?", (Bucket, Key)
            ).fetchone()
            if (IfNoneMatch and row) or (
                IfMatch and (not row or hashlib.sha256(row[0]).hexdigest() != IfMatch)
            ):
                raise ClientError(
                    {"Error": {"Code": "PreconditionFailed"}}, "PutObject"
                )
            self.db.execute(
                "insert or replace into objects values(?,?,?)", (Bucket, Key, Body)
            )
        self.writes += 1

    def delete_object(self, Bucket, Key):
        with self.db:
            self.db.execute(
                "delete from objects where bucket=? and key=?", (Bucket, Key)
            )


@pytest.fixture
def runtime(tmp_path):
    if not all(shutil.which(tool) for tool in ("restic", "age", "age-keygen")):
        pytest.skip("requires actual local Restic and age")
    executable = os.environ.get('JOSH_ROOM_TEST_RESTIC', shutil.which('restic'))
    version = subprocess.run([executable, 'version'], capture_output=True, text=True, check=False)
    if version.returncode or version.stdout.split()[:2] != ['restic', '0.19.1']:
        pytest.skip('requires the repository-pinned Restic 0.19.1; set JOSH_ROOM_TEST_RESTIC')
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    identity = generate_identity(private / "operational.age")
    recovery = generate_identity(private / "recovery.age")
    material = SimpleNamespace(
        identity=identity,
        recipient=derive_recipient(identity),
        encryption_domain_id=DOMAIN,
        keyset=SimpleNamespace(recovery_recipients=(derive_recipient(recovery),)),
    )
    dimension = DimensionConfig(
        "dimension",
        "Synthetic Dimension",
        "minio",
        "https://storage.example.invalid",
        "synthetic-bucket",
        "synthetic-profile",
        encryption_domain_id=DOMAIN,
        connection_id="connection",
    )
    transport = SQLiteS3(tmp_path / "objects.sqlite")
    authority = CompositionAuthority(
        SimpleNamespace(client=transport), dimension, NAMESPACE, material, private
    )

    def factory(secret):
        password = private / "password"
        password.write_text(secret)
        password.chmod(0o600)
        return ResticStore(
            repository=tmp_path / "composition-repository",
            password_file=password,
            cache_dir=private / "cache",
            executable=os.environ.get("JOSH_ROOM_TEST_RESTIC", shutil.which("restic")),
        )

    source = tmp_path / "source"
    (source / "a").mkdir(parents=True)
    (source / "b").mkdir()
    (source / "local").mkdir()
    (source / "a/file.txt").write_text("child a original\n")
    (source / "b/file.txt").write_text("child b original\n")
    (source / "local/unprotected.txt").write_text("local only synthetic\n")
    (source / "notes.txt").write_text("parent note\n")
    assignments = [
        {"path": ".", "outcome": "local-only"},
        {"path": "a", "outcome": "child"},
        {"path": "b", "outcome": "child"},
        {"path": "notes.txt", "outcome": "parent"},
    ]
    yield SimpleNamespace(
        source=source,
        private=private,
        dimension=dimension,
        authority=authority,
        factory=factory,
        transport=transport,
        assignments=assignments,
        tmp=tmp_path,
    )
    transport.db.close()


def save(runtime):
    r = runtime
    preview = capture_plan(r.source, r.dimension, NAMESPACE, "parent", r.assignments)
    return save_composition(
        r.source,
        r.dimension,
        NAMESPACE,
        "parent",
        r.assignments,
        preview["approval_sha256"],
        r.authority,
        r.factory,
        r.private,
    )


def test_actual_child_parent_save_and_whole_child_hydration(runtime, monkeypatch):
    r = runtime
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_VERSION", "0.1.52")
    result = save(r)
    assert result["status"] == "saved" and result["child_count"] == 2
    envelope, _ = r.authority.read()
    record = envelope["state"]["dimension_catalog"]["projects"]["parent"]["snapshots"][
        result["parent_snapshot_id"]
    ]
    assert (
        r.authority.descriptor(record).to_dict()["producer"]["josh_room_version"]
        == "0.1.52"
    )
    assert (r.source / "a/file.txt").read_text() == "child a original\n"
    assert not (r.source / MANIFEST).exists()
    target = r.tmp / "entered-parent"
    entered = hydrate_composition(
        r.dimension,
        NAMESPACE,
        "parent",
        result["parent_snapshot_id"],
        target,
        r.authority,
        r.factory,
        r.private,
    )
    assert entered["marker_verified"]
    assert (target / "notes.txt").read_text() == "parent note\n"
    assert (
        not (target / "a").exists()
        and not (target / "b").exists()
        and not (target / "local").exists()
    )
    hydrated = hydrate_composition(
        r.dimension,
        NAMESPACE,
        "parent",
        result["parent_snapshot_id"],
        target,
        r.authority,
        r.factory,
        r.private,
        child_path="a",
    )
    assert hydrated["unit"] == "whole-child"
    assert (target / "a/file.txt").read_text() == "child a original\n"
    assert not (target / "b").exists()
    assert (
        hydrate_composition(
            r.dimension,
            NAMESPACE,
            "parent",
            result["parent_snapshot_id"],
            target,
            r.authority,
            r.factory,
            r.private,
            child_path="a",
        )["status"]
        == "loaded"
    )
    from josh_room.composition import require_uncomposed_workspace
    from josh_room.composition_transaction import canonical

    (target / "a/.josh-room-split.json").unlink()
    with pytest.raises(ValueError, match="composed Save"):
        require_uncomposed_workspace(target / "a")
    with pytest.raises((ValueError, FileNotFoundError)):
        hydrate_composition(
            r.dimension,
            NAMESPACE,
            "parent",
            result["parent_snapshot_id"],
            target,
            r.authority,
            r.factory,
            r.private,
            child_path="a",
        )
    (target / "a/.josh-room-split.json").write_bytes(
        canonical(
            {
                "namespace": NAMESPACE,
                "room_id": hydrated["room_id"],
                "snapshot_id": hydrated["snapshot_id"],
            }
        )
    )
    (target / "a/file.txt").write_text("local child edit")
    with pytest.raises(ValueError, match="local changes"):
        hydrate_composition(
            r.dimension,
            NAMESPACE,
            "parent",
            result["parent_snapshot_id"],
            target,
            r.authority,
            r.factory,
            r.private,
            child_path="a",
        )


def test_root_files_only_synthetic_transport_parent_restore_and_child_enter(runtime):
    """Use real Restic/age with SQLite S3 transport; this is not live-provider evidence."""
    r = runtime
    (r.source / "a/grandchild").mkdir()
    (r.source / "a/grandchild/deep.txt").write_text("whole child payload\n")
    assignments = [
        {"path": "a", "outcome": "child"},
        {"path": "b", "outcome": "child"},
    ]
    preview = capture_plan(
        r.source, r.dimension, NAMESPACE, "parent", assignments,
        root_files_only=True, parent_name="Synthetic Projects",
    )
    assert preview["request"]["root_files_only"] is True
    assert preview["request"]["assignments"] == assignments
    assert preview["request"]["parent_name"] == "Synthetic Projects"
    with pytest.raises(ValueError, match="approval is stale"):
        save_composition(
            r.source, r.dimension, NAMESPACE, "parent", assignments,
            preview["approval_sha256"], r.authority, r.factory, r.private,
            root_files_only=True, parent_name="Changed Catalog Name",
        )
    saved = save_composition(
        r.source, r.dimension, NAMESPACE, "parent", assignments,
        preview["approval_sha256"], r.authority, r.factory, r.private,
        root_files_only=True, parent_name="Synthetic Projects",
    )
    from josh_room.composition_discovery import discover_catalogs

    other_namespace = "2" * 32
    bucket = r.dimension.bucket
    original = r.transport.get_object(Bucket=bucket, Key=f"composition/v1/{NAMESPACE}/authority.age")["Body"].read()
    r.transport.put_object(Bucket=bucket, Key=f"composition/v1/{other_namespace}/authority.age", Body=original)
    writes = r.transport.writes
    discovery = discover_catalogs(SimpleNamespace(client=r.transport), r.dimension, r.authority.material)
    assert discovery["catalog_discovery_partial"] is True
    assert [(row["namespace"], row["parent_room_id"]) for row in discovery["catalogs"]] == [(NAMESPACE, "parent")]
    assert discovery["catalogs"][0]["recovery_points"][0]["snapshot_id"] == saved["parent_snapshot_id"]
    assert discovery["catalogs"][0]["display_name"] == "Synthetic Projects"
    assert discovery["catalogs"][0]["recovery_points"][0]["children"] == ["a", "b"]
    assert r.transport.writes == writes
    target = r.tmp / "root-files-only-entered"
    parent = hydrate_composition(
        r.dimension, NAMESPACE, "parent", saved["parent_snapshot_id"],
        target, r.authority, r.factory, r.private,
    )
    assert parent["marker_verified"]
    assert json.loads((target / ".josh-room.json").read_text())["display_name"] == "Synthetic Projects"
    assert (target / "notes.txt").read_text() == "parent note\n"
    assert not any((target / path).exists() for path in ("a", "b", "local"))

    child = hydrate_composition(
        r.dimension, NAMESPACE, "parent", saved["parent_snapshot_id"],
        target, r.authority, r.factory, r.private, child_path="a",
    )
    assert child["unit"] == "whole-child"
    assert (target / "a/grandchild/deep.txt").read_text() == "whole child payload\n"
    assert not (target / "b").exists()
    assert not (target / "local").exists()

    args = (r.dimension, NAMESPACE, "parent", saved["parent_snapshot_id"],
            target, r.authority, r.factory, r.private)
    with pytest.raises(ValueError, match="not in the selected authority"):
        hydrate_composition(*args, child_paths=["b", "absent"])
    assert not (target / "b").exists()
    (target / "b").mkdir()
    (target / "b/user.txt").write_text("keep this")
    partial = hydrate_composition(*args, child_paths=["a", "b"])
    assert partial["status"] == "partial"
    assert partial["failed_index"] == 1
    assert partial["restored"] == [{**child, "status": "loaded"}]
    assert (target / "b/user.txt").read_text() == "keep this"
    (target / "b/user.txt").unlink()
    (target / "b").rmdir()
    batch = hydrate_composition(*args, child_paths=["a", "b"])
    assert batch["status"] == "hydrated"
    assert [row["status"] for row in batch["restored"]] == ["loaded", "hydrated"]
    assert (target / "b").is_dir()

    from josh_room.composition_updates import open_catalog_snapshot, refresh_parent
    from josh_room.room_store_lifecycle import inspect_catalog_room_store

    (target / "root-edit.txt").write_text("catalog edit\n")
    (target / "local-only").mkdir()
    (target / "local-only/private.txt").write_text("local only\n")
    (target / "a/README.md").write_text("independent child edit\n")
    parent_save = (r.dimension, NAMESPACE, "parent", saved["parent_snapshot_id"], "catalog-save",
                   {}, r.authority, r.factory, r.private)
    save_options = {"workspace": target, "save_root": True}
    preview = refresh_parent(*parent_save, **save_options)
    (target / "root-edit.txt").write_text("changed after review\n")
    writes = r.transport.writes
    with pytest.raises(ValueError, match="approval differs"):
        refresh_parent(*parent_save, **save_options, approval=preview["approval_sha256"])
    assert r.transport.writes == writes
    (target / "root-edit.txt").write_text("catalog edit\n")
    updated = refresh_parent(*parent_save, **save_options, approval=preview["approval_sha256"])
    assert updated["marker_verified"]
    assert json.loads((target / ".josh-room.json").read_text())["snapshot_id"] == updated["snapshot_id"]
    recovered = r.tmp / "updated-catalog"
    hydrate_composition(r.dimension, NAMESPACE, "parent", updated["snapshot_id"],
                        recovered, r.authority, r.factory, r.private)
    assert (recovered / "root-edit.txt").read_text() == "catalog edit\n"
    assert (recovered / ".josh-room-composition.json").read_bytes() == (target / ".josh-room-composition.json").read_bytes()
    assert not (recovered / "a").exists()
    assert not (recovered / "local-only").exists()
    with open_catalog_snapshot(r.dimension, NAMESPACE, child["room_id"], child["snapshot_id"],
                               r.authority, r.factory, r.private) as context:
        assert inspect_catalog_room_store(context)["logical_jat_id"] == child["snapshot_id"]

    from josh_room.composition_retirement import preview_retirement, retire

    backend = SimpleNamespace(client=r.transport)
    catalog = discovery["catalogs"][0]
    assert [room["path"] for room in catalog["child_rooms"]] == ["a", "b"]
    from josh_room.cli import _bounded_json_result

    wire = _bounded_json_result({"ok": True, "dimensions": [discovery]})
    wire_catalog = wire["dimensions"][0]["catalogs"][0]
    assert wire_catalog["child_recovery_points"] == catalog["child_recovery_points"]
    assert len(wire_catalog["child_recovery_points"]) == 2
    assert all(point["pinned"] and point["created_at"] for point in wire_catalog["child_recovery_points"])

    def remove(kind, room=None, snapshot=None):
        args = r.authority, NAMESPACE, kind, room, snapshot
        preview = preview_retirement(*args)
        assert preview["can_apply"]
        return retire(*args, approved_plan_sha256=preview["approval_sha256"])

    remove("recovery-point", "parent", saved["parent_snapshot_id"])
    remove("recovery-point", "parent", updated["snapshot_id"])
    catalog = discover_catalogs(backend, r.dimension, r.authority.material)["catalogs"][0]
    assert catalog["recovery_points"] == []
    assert len(catalog["child_rooms"]) == 2
    with pytest.raises(ValueError, match="retired"):
        hydrate_composition(r.dimension, NAMESPACE, "parent", saved["parent_snapshot_id"],
                            r.tmp / "retired-parent", r.authority, r.factory, r.private)
    for point in catalog["child_recovery_points"]:
        assert not point["pinned"]
        remove("recovery-point", point["room_id"], point["snapshot_id"])
    for room in catalog["child_rooms"]:
        remove("room", room["room_id"])
    remove("catalog")
    assert discover_catalogs(backend, r.dimension, r.authority.material)["catalogs"] == []
    with (pytest.raises(ValueError, match="retired"),
          open_catalog_snapshot(r.dimension, NAMESPACE, child["room_id"], child["snapshot_id"],
                                r.authority, r.factory, r.private)):
        pytest.fail("retired Catalog point exposed a store")


def test_git_only_child_update_and_historical_cold_hydration(runtime):
    from josh_room.composition_updates import refresh_parent, save_child
    from scripts.git_recovery_acceptance import (
        advance_catalog_git,
        catalog_repository,
        contents,
        identity,
    )

    r = runtime
    for name in ("a", "b"):
        catalog_repository(r.source / name)
    original = contents(r.source)
    expected = identity(r.source / "a")
    preview = capture_plan(r.source, r.dimension, NAMESPACE, "parent", r.assignments)
    saved = save(r)
    initial_parent = saved["parent_snapshot_id"]

    def hydrate(label, parent):
        root = r.tmp / label
        args = (r.dimension, NAMESPACE, "parent", parent, root, r.authority, r.factory, r.private)
        hydrate_composition(*args)
        assert (root / "notes.txt").is_file()
        assert all(not (root / path).exists() for path in ("a", "b", "local", ".git"))
        receipt = hydrate_composition(*args, child_path="a")
        assert not (root / "b").exists()
        return root / "a", receipt

    child, receipt = hydrate("initial", initial_parent)
    assert (child / ".git").is_dir(), "Catalog child lost repository metadata"
    assert identity(child) == expected
    assert contents(child / ".git") == contents(r.source / "a/.git")
    assert all(row["capture_mode"] == "full-repository" for row in preview["request"]["child_evidence"].values())
    room = receipt["room_id"]
    old_pin = receipt["snapshot_id"]
    envelope, _ = r.authority.read()
    record = envelope["state"]["dimension_catalog"]["projects"][room]["snapshots"][old_pin]
    assert r.authority.descriptor(record).to_dict()["capture_mode"] == "full-repository"

    advance_catalog_git(child)
    changed = identity(child)
    assert changed != expected

    def update(pin, operation, approval=None):
        return save_child(child, r.dimension, NAMESPACE, room, pin, operation,
                          r.authority, r.factory, r.private, approval=approval)

    plan = update(old_pin, "git-only")
    assert plan["changed"]
    updated = update(old_pin, "git-only", plan["approval_sha256"])
    new_pin = updated["snapshot_id"]
    assert updated["marker_verified"] and old_pin != new_pin
    envelope, _ = r.authority.read()
    projects = envelope["state"]["dimension_catalog"]["projects"]
    assert projects["parent"]["latest"] == initial_parent
    assert r.authority.descriptor(projects[room]["snapshots"][new_pin]).to_dict()["parent_logical_jat_id"] == old_pin
    noop = update(new_pin, "noop")
    assert not noop["changed"]
    assert update(new_pin, "noop", noop["approval_sha256"])["catalog_writes"] is False

    args = (r.dimension, NAMESPACE, "parent", initial_parent, "refresh", {"a": new_pin}, r.authority, r.factory, r.private)
    refresh = refresh_parent(*args)
    new_parent = refresh_parent(*args, approval=refresh["approval_sha256"])["snapshot_id"]
    for label, parent, pin, state in (("old", initial_parent, old_pin, expected), ("new", new_parent, new_pin, changed)):
        restored, entered = hydrate(label, parent)
        assert entered["snapshot_id"] == pin
        assert identity(restored) == state
        assert (restored / "unstaged.txt").read_bytes() == (child / "unstaged.txt").read_bytes()
        assert (restored / "untracked.txt").read_bytes() == (child / "untracked.txt").read_bytes()
    assert contents(r.source) == original


def test_stale_approval_and_unassigned_capture_are_zero_provider_writes(runtime):
    r = runtime
    with pytest.raises(ValueError, match="approval"):
        save_composition(
            r.source,
            r.dimension,
            NAMESPACE,
            "parent",
            r.assignments,
            "0" * 64,
            r.authority,
            r.factory,
            r.private,
        )
    assert r.transport.writes == 0
    with pytest.raises(ValueError, match="explicit subtree"):
        capture_plan(r.source, r.dimension, NAMESPACE, "parent", r.assignments[1:])
    assert r.transport.writes == 0


def test_restore_after_parent_edit_keeps_edits_and_checks_manifest(runtime, monkeypatch):
    from josh_room import composition_lifecycle as lifecycle

    r = runtime
    result = save(r)
    target = r.tmp / "entered-parent"
    hydrate_composition(
        r.dimension,
        NAMESPACE,
        "parent",
        result["parent_snapshot_id"],
        target,
        r.authority,
        r.factory,
        r.private,
    )
    (target / "notes.txt").write_text("unsaved changes")
    original_scan = lifecycle.scan_workspace_for_status
    def scan_after_editor_write(workspace):
        if workspace == target / "a":
            (workspace / "editor.txt").write_text("edit after promotion")
        return original_scan(workspace)
    monkeypatch.setattr(lifecycle, "scan_workspace_for_status", scan_after_editor_write)
    args = (r.dimension, NAMESPACE, "parent", result["parent_snapshot_id"],
            target, r.authority, r.factory, r.private)
    receipt = hydrate_composition(*args, child_paths=["a", "b"])
    assert receipt["status"] == "hydrated"
    assert len(receipt["restored"]) == 2
    assert receipt["restored"][0]["local_changes"]
    assert receipt["restored"][0]["marker_verified"]
    assert (target / "notes.txt").read_text() == "unsaved changes"
    assert (target / "a/editor.txt").read_text() == "edit after promotion"
    from josh_room.composition_updates import catalog_workspace_status

    status = catalog_workspace_status(target)
    assert status["state"] == "changed"
    assert [(row["path"], row["state"]) for row in status["catalog_children"]] == [("a", "changed"), ("b", "clean")]
    assert (target / "b/.josh-room.json").is_file()
    broken = r.tmp / "edited-manifest"
    hydrate_composition(*args[:4], broken, *args[5:])
    (broken / ".josh-room-composition.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest, or capture policy changed"):
        hydrate_composition(*args[:4], broken, *args[5:], child_path="b")
    assert not (broken / "b").exists()
    other = CompositionAuthority(
        SimpleNamespace(client=r.transport),
        r.dimension,
        "2" * 32,
        r.authority.material,
        r.private,
    )
    with pytest.raises(ValueError, match="unavailable"):
        hydrate_composition(
            r.dimension,
            "2" * 32,
            "parent",
            result["parent_snapshot_id"],
            target,
            other,
            r.factory,
            r.private,
            child_path="a",
        )


def test_inherited_exclusions_nested_child_and_legacy_optimize(runtime):
    from josh_room.minio import MinioBackend, MinioConfig
    from josh_room.room_store_lifecycle import optimize_room_store

    r = runtime
    (r.source / "projects").mkdir()
    (r.source / "a").rename(r.source / "projects/a")
    (r.source / "projects/a/private").mkdir()
    (r.source / "projects/a/private/never.txt").write_text("excluded synthetic payload")
    from scripts.git_recovery_acceptance import git

    git(r.source, "init", "-q")
    git(r.source, "add", ".")
    r.assignments[1]["path"] = "projects/a"
    with pytest.raises(ValueError, match="cohesive Git repository"):
        capture_plan(r.source, r.dimension, NAMESPACE, "parent", r.assignments)
    assert r.transport.writes == 0
    (r.source / ".josh-roomignore").write_text("# josh-room capture-mode: source-only\nprojects/a/private/**\n")
    (r.source / "projects/a/.josh-roomignore").write_text("generated/**\n")
    (r.source / "projects/a/restricted").mkdir(mode=0o700)
    (r.source / "projects/a/restricted/file.txt").write_text("permission fixture")
    result = save(r)
    envelope, _ = r.authority.read()
    records = envelope["state"]["dimension_catalog"]["projects"]
    with r.factory(envelope["secret"]) as store:
        store.open_existing()
        for project in records.values():
            record = project["snapshots"][project["latest"]]
            entries = list(store.entries(record["workspace_snapshot_id"]))
            assert not any(".git" in row.path.split("/") for row in entries)
            assert not any("never.txt" in row.path for row in entries)
    # Pause a legacy deletion after its catalog decision, then resume its actual
    # root-key cleanup after new namespace publication. Same ciphertext digest.
    first = next(iter(records.values()))
    record = next(iter(first["snapshots"].values()))
    raw = r.authority._read(record["object_key"])[0]
    r.transport.put_object(
        Bucket=r.dimension.bucket, Key=record["object_key"], Body=raw
    )
    legacy = MinioBackend(
        MinioConfig(r.dimension.endpoint, r.dimension.bucket, "synthetic"),
        client=r.transport,
    )
    legacy.delete_object(record["object_key"])
    assert r.authority._read(record["object_key"])[0] == raw
    with pytest.raises(ValueError, match="opaque object key"):
        legacy.delete_object(r.authority.prefix + record["object_key"])
    password = r.private / "legacy-password"
    password.write_text("synthetic-legacy-password")
    password.chmod(0o600)
    with ResticStore(
        repository=r.tmp / "legacy-repository",
        cache_dir=r.private / "legacy-cache",
        password_file=password,
        executable=os.environ["JOSH_ROOM_TEST_RESTIC"],
    ) as store:
        legacy_repo = store.initialize()
        store.backup(r.source / "b")
        assert optimize_room_store(store, confirmed=True)["status"] == "completed"
        assert store.open_existing() == legacy_repo
    target = r.tmp / "entered-parent"
    hydrate_composition(
        r.dimension,
        NAMESPACE,
        "parent",
        result["parent_snapshot_id"],
        target,
        r.authority,
        r.factory,
        r.private,
    )
    assert (target / "projects").is_dir() and not (target / "projects/a").exists()
    hydrate_composition(
        r.dimension,
        NAMESPACE,
        "parent",
        result["parent_snapshot_id"],
        target,
        r.authority,
        r.factory,
        r.private,
        child_path="projects/a",
    )
    assert (target / "projects/a/file.txt").read_text() == "child a original\n"
    assert not (target / "projects/a/private").exists()
    assert (target / "projects/a/restricted").stat().st_mode & 0o777 == 0o700


def test_unsupported_parent_override_and_repointed_destination_are_refused(runtime):
    from dataclasses import replace

    r = runtime
    assignments = [
        *r.assignments,
        {"path": "local", "outcome": "local-only"},
        {"path": "local/unprotected.txt", "outcome": "parent"},
    ]
    with pytest.raises(ValueError, match="overlap"):
        capture_plan(r.source, r.dimension, NAMESPACE, "parent", assignments)
    before = capture_plan(r.source, r.dimension, NAMESPACE, "parent", r.assignments)
    after = capture_plan(
        r.source,
        replace(r.dimension, bucket="other-bucket"),
        NAMESPACE,
        "parent",
        r.assignments,
    )
    assert before["approval_sha256"] != after["approval_sha256"]
    assert r.transport.writes == 0


@pytest.mark.parametrize(
    "point", ["bootstrap", "child-snapshot", "parent-snapshot", "parent-commit"]
)
def test_process_loss_retry_preserves_exact_slots_and_no_duplicate_snapshots(
    runtime, monkeypatch, point
):
    class ProcessLoss(BaseException):
        pass

    r = runtime
    original_put = r.authority.put
    original_descriptor = r.authority.put_descriptor
    hit = False
    descriptor_count = 0
    original_accounting = {}

    def put(body, expected):
        nonlocal hit
        result = original_put(body, expected)
        if not hit and (
            point == "bootstrap"
            and body["state"] is None
            or point == "parent-commit"
            and body["state"]
            and body["state"]["operations"][NAMESPACE]["phase"] == "committed"
        ):
            hit = True
            raise ProcessLoss()
        return result

    def descriptor(value):
        nonlocal descriptor_count, hit
        body = value.to_dict()
        original_accounting.setdefault(body["logical_jat_id"], (body["workspace"]["data_added"], body["workspace"]["data_added_packed"]))
        result = original_descriptor(value)
        descriptor_count += 1
        if (
            not hit
            and descriptor_count == (1 if point == "child-snapshot" else 3)
            and point in ("child-snapshot", "parent-snapshot")
        ):
            hit = True
            raise ProcessLoss()
        return result

    monkeypatch.setattr(r.authority, "put", put)
    monkeypatch.setattr(r.authority, "put_descriptor", descriptor)
    with pytest.raises(ProcessLoss):
        save(r)
    assert hit
    result = save(r)
    assert result["status"] == "saved"
    envelope, _ = r.authority.read()
    with r.factory(envelope["secret"]) as store:
        store.open_existing()
        assert len(store.snapshots()) == 3
    for project in envelope["state"]["dimension_catalog"]["projects"].values():
        record = project["snapshots"][project["latest"]]
        descriptor = r.authority.descriptor(record).to_dict()
        workspace = descriptor["workspace"]
        assert (workspace["data_added"], workspace["data_added_packed"]) == original_accounting[descriptor["logical_jat_id"]]
        assert workspace["data_added"] > 0
    assert save(r)["parent_snapshot_id"] == result["parent_snapshot_id"]


def test_provider_runtime_uses_actual_binding_and_composition_locator(
    runtime, monkeypatch
):
    from contextlib import contextmanager

    from josh_room import cli, composition_lifecycle, room_store_bridge

    r = runtime

    @contextmanager
    def operation_directory():
        yield r.private

    monkeypatch.setattr(
        room_store_bridge,
        "_resolve_material",
        lambda dimension, material, session, **kwargs: (r.authority.material, None),
    )
    monkeypatch.setattr(room_store_bridge, "_scope", lambda *_: DOMAIN)
    monkeypatch.setattr(
        room_store_bridge, "_private_operation_directory", operation_directory
    )
    monkeypatch.setattr(
        room_store_bridge,
        "_create_backend",
        lambda *_: SimpleNamespace(client=r.transport),
    )
    monkeypatch.setattr(
        room_store_bridge,
        "_verified_restic_executable",
        lambda **_: os.environ["JOSH_ROOM_TEST_RESTIC"],
    )
    monkeypatch.setattr(room_store_bridge, "_provider_environment", lambda _: {})
    locators = []

    def factory(**kwargs):
        locators.append(kwargs["repository"])
        kwargs["repository"] = r.tmp / "composition-repository"
        kwargs.pop("s3_bucket_lookup", None)
        return ResticStore(**kwargs)

    monkeypatch.setattr(room_store_bridge, "_restic_store_factory", factory)
    monkeypatch.setattr(cli, "_effective_dimension", lambda _: r.dimension)
    base = [
        "composition",
        "capture-plan",
        "--namespace",
        NAMESPACE,
        "--dimension",
        "dimension",
        "--parent-room",
        "parent",
        "--workspace",
        str(r.source),
    ]
    for row in r.assignments:
        base.extend(["--assign", row["path"] + "=" + row["outcome"]])
    preview = cli.dispatch(cli.build_parser().parse_args(base), r.tmp / "instance")
    base[1] = "save"
    args = cli.build_parser().parse_args(
        [*base, "--approve", preview["approval_sha256"], "--ack-local-only"]
    )
    args._selected_encryption_material = r.authority.material
    result = cli.dispatch(args, r.tmp / "instance")
    assert result["status"] == "saved"
    assert locators == [
        f"s3:{r.dimension.endpoint}/{r.dimension.bucket}/composition/v1/{NAMESPACE}/payload-restic-v1"
    ]
    assert composition_lifecycle.namespace_prefix(NAMESPACE) in locators[0]
