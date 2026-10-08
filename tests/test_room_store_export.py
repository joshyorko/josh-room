from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from josh_room.logical_jat import LogicalJat
from josh_room.restic_store import SnapshotEntry, SnapshotInfo
from josh_room.room_store_export import (
    PortableExportError,
    _separate_staged_hardlinks,
    export_portable_jat,
    materialize_components,
)


def test_staged_git_hardlinks_are_copied_without_changing_source(tmp_path):
    source = tmp_path / "source-object"
    source.write_bytes(b"synthetic packed Git objects")
    source.chmod(0o640)
    stage = tmp_path / "private-stage"
    stage.mkdir(mode=0o700)
    first = stage / "first.pack"
    second = stage / "second.pack"
    os.link(source, first)
    os.link(first, second)
    inode = source.stat().st_ino
    _separate_staged_hardlinks(stage, frozenset({"first.pack", "second.pack"}))
    assert source.stat().st_ino == inode
    assert source.read_bytes() == first.read_bytes() == second.read_bytes()
    assert first.stat().st_nlink == second.stat().st_nlink == 1
    assert first.stat().st_mode == source.stat().st_mode == second.stat().st_mode

REPOSITORY_ID = "a" * 64
WORKSPACE_SNAPSHOT = "b" * 64
WORKSPACE_TREE = "c" * 64


def _descriptor(components=None):
    defaults = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "josh_room"
        / "workspace_capture_defaults.json"
    )
    capture_policy_sha256 = hashlib.sha256(
        defaults.read_bytes() + b"\0ignore-absent\0\0active-runtime\0"
    ).hexdigest()

    def snapshot(snapshot_id: str, tree_id: str) -> dict[str, object]:
        return {
            "repository_id": REPOSITORY_ID,
            "repository_format": 2,
            "snapshot_id": snapshot_id,
            "tree_id": tree_id,
        }

    rcc = b"verified-rcca"
    brew = b"verified-brew"
    hauler = b"verified-hauler"
    components = components or {
        "rcc_environment": {
            "kind": "rcca",
            "snapshot": snapshot("d" * 64, "e" * 64),
            "archive_sha256": hashlib.sha256(rcc).hexdigest(),
            "archive_size": len(rcc),
            "member_basename": "rcc-environment.rcca",
            "artifact_digest": "sha256:" + "1" * 64,
            "specification_digest": "sha256:" + "2" * 64,
            "platform": "linux-x64",
            "rcc_version": "18.19.5",
            "robot_relative_path": "robot.yaml",
        },
        "homebrew_recovery": {
            "kind": "homebrew-recovery",
            "snapshot": snapshot("f" * 64, "1" * 64),
            "archive_sha256": hashlib.sha256(brew).hexdigest(),
            "archive_size": len(brew),
            "member_basename": "homebrew-recovery.tar.zst",
        },
        "hauler_content": {
            "kind": "hauler-content",
            "snapshot": snapshot("2" * 64, "3" * 64),
            "archive_sha256": hashlib.sha256(hauler).hexdigest(),
            "archive_size": len(hauler),
            "member_basename": "hauler-content.tar.zst",
            "references": [{"digest": "sha256:" + "4" * 64, "kind": "image"}],
        },
    }
    return LogicalJat.from_dict(
        {
            "format_version": 1,
            "payload_kind": "room-store-v1",
            "logical_jat_id": "logical-test",
            "dimension_id": "dimension-test",
            "encryption_domain_id": "domain-test",
            "room_id": "room-test",
            "created_at": "2026-10-02T12:00:00Z",
            "capture_policy_sha256": capture_policy_sha256,
            "workspace": {
                "engine": "restic",
                "repository_id": REPOSITORY_ID,
                "repository_format": 2,
                "snapshot_id": WORKSPACE_SNAPSHOT,
                "tree_id": WORKSPACE_TREE,
                "source_path": ".",
                "logical_bytes": 7,
                "data_added": 7,
                "data_added_packed": 7,
            },
            "components": components,
            "source": {},
            "producer": {
                "josh_room_version": "0.1.26",
                "restic_version": "0.19.1",
                "source_platform": "linux-x64",
                "restore_platforms": ["linux-x64"],
            },
        }
    )


class _Restic:
    def __init__(
        self,
        descriptor: LogicalJat,
        *,
        unsafe_component_path: bool = False,
        workspace_symlink: bool = False,
    ):
        self.descriptor = descriptor.to_dict()
        self.trees = {
            WORKSPACE_SNAPSHOT: WORKSPACE_TREE,
            "d" * 64: "e" * 64,
            "f" * 64: "1" * 64,
            "2" * 64: "3" * 64,
        }
        self.data = {
            WORKSPACE_SNAPSHOT: {"project.txt": b"project"},
            "d" * 64: {
                "rcc-environment.rcca": b"verified-rcca",
                "metadata.json": json.dumps(
                    {
                        "artifact_digest": "sha256:" + "1" * 64,
                        "specification_digest": "sha256:" + "2" * 64,
                        "platform": "linux-x64",
                        "rcc_version": "18.19.5",
                        "archive_sha256": hashlib.sha256(b"verified-rcca").hexdigest(),
                        "archive_size": len(b"verified-rcca"),
                        "robot_relative_path": "robot.yaml",
                        "legacy_blueprint_key": "legacy-test",
                    }
                ).encode(),
            },
            "f" * 64: {"homebrew-recovery.tar.zst": b"verified-brew"},
            "2" * 64: {
                "hauler-content.tar.zst": b"verified-hauler",
                "metadata.json": json.dumps(
                    {
                        "format_version": 1,
                        "archive_sha256": hashlib.sha256(
                            b"verified-hauler"
                        ).hexdigest(),
                        "archive_size": len(b"verified-hauler"),
                        "references": [
                            {"digest": "sha256:" + "4" * 64, "kind": "image"}
                        ],
                    }
                ).encode(),
            },
        }
        self.unsafe_component_path = unsafe_component_path
        self.workspace_symlink = workspace_symlink

    def snapshot(self, snapshot_id: str):
        return SnapshotInfo(
            snapshot_id, self.trees[snapshot_id], None, "2026-10-02T12:00:00Z", ("/",)
        )

    def entries(self, snapshot_id: str):
        rows = [SnapshotEntry(".", "dir", 0, 0o700, None)]
        data = self.data[snapshot_id]
        for name, content in data.items():
            path = (
                "../escape"
                if self.unsafe_component_path and snapshot_id == "f" * 64
                else name
            )
            rows.append(SnapshotEntry(path, "file", len(content), 0o600, None))
        if snapshot_id == WORKSPACE_SNAPSHOT and self.workspace_symlink:
            rows.append(
                SnapshotEntry("project-link", "symlink", 0, 0o777, "project.txt")
            )
        return iter(rows)

    def restore(self, snapshot_id: str, destination: Path, **_kwargs):
        destination.mkdir()
        for name, content in self.data[snapshot_id].items():
            (destination / name).write_bytes(content)
        if snapshot_id == WORKSPACE_SNAPSHOT and self.workspace_symlink:
            (destination / "project-link").symlink_to("project.txt")


def _private_dir(path: Path):
    path.mkdir(mode=0o700)
    return path


def _native_stubs(
    *,
    mismatch: bool = False,
    workspace_mismatch: bool = False,
    extraction_mismatch: str | None = None,
    hauler_kind_mismatch: bool = False,
    restore_metadata_mismatch: str | None = None,
    on_build=None,
):
    state = {"events": []}

    def build(jat_root, source, output, **kwargs):
        state["events"].append("build")
        if on_build:
            on_build(source, kwargs)
        state["source"] = source
        state["kwargs"] = kwargs
        state["component_bytes"] = {
            key: value.read_bytes()
            for key, value in kwargs.items()
            if key in {"rcc_archive", "brew_archive", "hauler_archive"}
        }
        state["rcc_metadata"] = (
            json.loads(kwargs["rcc_metadata"].read_text())
            if "rcc_metadata" in kwargs
            else None
        )
        state["rcc_metadata_bytes"] = (
            (json.dumps(state["rcc_metadata"], indent=2, sort_keys=True) + "\n").encode()
            if "rcc_metadata" in kwargs else None
        )
        output.write_bytes(b"complete-capsule")
        return {"success": True, "operation": "build"}

    def inspect(_jat_root, _haul):
        state["events"].append("inspect")
        hauler_row = {
            "reference": "example.test/image:latest",
            "type": "image",
            "digest": "sha256:" + "4" * 64,
            "platform": "linux/amd64",
            "size": 4096,
        }
        if hauler_kind_mismatch:
            hauler_row["type"] = "file"
        return {
            "success": True,
            "operation": "inspect",
            "inventory": [
                {
                    "reference": "hauler/joshs-all-the-things-workspace.tar.zst:latest",
                    "type": "file",
                    "digest": None,
                },
                {
                    "reference": "hauler/homebrew-recovery.tar.zst:latest",
                    "type": "file",
                    "digest": hashlib.sha256(b"verified-brew").hexdigest(),
                    "size": len(b"verified-brew"),
                },
                {
                    "reference": "hauler/rcc-environment.rcca:latest",
                    "type": "file",
                    "digest": hashlib.sha256(b"verified-rcca").hexdigest(),
                    "size": len(b"verified-rcca"),
                },
                {
                    "reference": "hauler/rcc-environment-metadata.json:latest",
                    "type": "file",
                    "digest": hashlib.sha256(state["rcc_metadata_bytes"]).hexdigest(),
                    "size": len(state["rcc_metadata_bytes"]),
                },
                hauler_row,
            ],
            "anchors": {
                "workspace": True,
                "brew": True,
                "rcc_environment": True,
                "rcc_metadata": True,
            },
        }

    def extract(_jat_root, _haul, reference, destination):
        state["events"].append(("extract", reference))
        destination.mkdir()
        name = reference.rsplit("/", 1)[-1].removesuffix(":latest")
        data = {
            "homebrew-recovery.tar.zst": state["component_bytes"]["brew_archive"],
            "rcc-environment.rcca": state["component_bytes"]["rcc_archive"],
            "rcc-environment-metadata.json": state["rcc_metadata_bytes"],
        }[name]
        if extraction_mismatch == name:
            data += b"-changed"
        path = destination / name
        path.write_bytes(data)
        return {
            "success": True,
            "operation": "extract",
            "payloads": [
                {
                    "path": str(path),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        }

    def restore(_jat_root, _haul, destination):
        state["events"].append("restore")
        destination.mkdir()
        (destination / "workspace").mkdir()
        restored_workspace = destination / "workspace" / state["source"].name
        shutil.copytree(state["source"], restored_workspace, symlinks=True)
        if workspace_mismatch:
            (restored_workspace / "project.txt").write_text(
                "different", encoding="utf-8"
            )
        (restored_workspace / ".josh-room.json").write_text("generated marker")
        (destination / "homebrew-recovery").mkdir()
        (destination / "homebrew-recovery" / "restored.txt").write_text("ok")
        expected_rcc = state["rcc_metadata"]
        environment_artifact = {
            key: expected_rcc[key]
            for key in (
                "artifact",
                "specification_digest",
                "legacy_blueprint_key",
                "archive",
                "archive_sha256",
                "archive_size",
                "rcc_version",
                "platform",
                "robot",
            )
        }
        if mismatch:
            environment_artifact["artifact"] = "sha256:" + "9" * 64
        if restore_metadata_mismatch:
            environment_artifact[restore_metadata_mismatch] = "changed"
        (destination / "environment_artifact.json").write_text(
            json.dumps(environment_artifact)
        )
        return {
            "success": True,
            "operation": "restore",
            "environment_artifact": json.loads(
                (destination / "environment_artifact.json").read_text()
            ),
        }

    return build, inspect, restore, extract, state


def test_export_reserves_composition_and_clean_restore_space_before_materializing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from josh_room import room_store_export

    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    restic = _Restic(descriptor)
    restic.snapshot = lambda *_args: pytest.fail("low-space export must not materialize data")
    body = descriptor.to_dict()
    logical = body["workspace"]["logical_bytes"] + sum(
        component["archive_size"] for component in body["components"].values() if component is not None
    )
    monkeypatch.setattr(room_store_export.shutil, "disk_usage", lambda _path: SimpleNamespace(free=3 * logical))
    with pytest.raises(PortableExportError, match="free disk space"):
        export_portable_jat(descriptor=descriptor, restic=restic, staging_parent=parent,
                            output=tmp_path / "portable.haul", jat_root=tmp_path / "jat")
    assert list(parent.iterdir()) == []
    assert not (tmp_path / "portable.haul").exists()


def test_export_passes_verified_components_to_jat_and_promotes_only_after_clean_restore(
    tmp_path,
):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, state = _native_stubs()

    result = export_portable_jat(
        descriptor=descriptor,
        restic=_Restic(descriptor),
        staging_parent=parent,
        output=output,
        jat_root=tmp_path / "jat",
        run_build_fn=build,
        run_inspect_fn=inspect,
        run_restore_fn=restore,
        run_extract_fn=extract,
    )

    assert result.logical_jat_id == "logical-test"
    assert result.output_size == len(b"complete-capsule")
    assert output.read_bytes() == b"complete-capsule"
    assert state["component_bytes"] == {
        "rcc_archive": b"verified-rcca",
        "brew_archive": b"verified-brew",
        "hauler_archive": b"verified-hauler",
    }
    assert state["rcc_metadata"]["artifact"] == "sha256:" + "1" * 64
    assert state["rcc_metadata"]["platform"] == "linux_amd64"
    assert state["events"][0:2] == ["build", "inspect"]
    assert [event[0] for event in state["events"] if isinstance(event, tuple)] == [
        "extract",
        "extract",
        "extract",
    ]
    assert state["events"][-1] == "restore"
    assert not list(parent.iterdir())


def test_export_preserves_and_compares_workspace_symlink_targets(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs()

    result = export_portable_jat(
        descriptor=descriptor,
        restic=_Restic(descriptor, workspace_symlink=True),
        staging_parent=parent,
        output=output,
        jat_root=tmp_path / "jat",
        run_build_fn=build,
        run_inspect_fn=inspect,
        run_restore_fn=restore,
        run_extract_fn=extract,
    )

    assert result.workspace_entry_count == 2
    assert output.read_bytes() == b"complete-capsule"
    assert not list(parent.iterdir())


def test_export_fails_closed_on_unsafe_component_snapshot_path(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    build, inspect, restore, extract, _state = _native_stubs()

    with pytest.raises(PortableExportError, match="unsafe"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor, unsafe_component_path=True),
            staging_parent=parent,
            output=tmp_path / "portable.haul.tar.zst",
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not (tmp_path / "portable.haul.tar.zst").exists()
    assert not list(parent.iterdir())


def test_materialize_components_accepts_native_entries_without_root_row(tmp_path):
    descriptor = _descriptor()
    restic = _Restic(descriptor)
    entries = restic.entries
    restic.entries = lambda snapshot_id: (
        row for row in entries(snapshot_id) if row.path != "."
    )
    with materialize_components(descriptor, restic, _private_dir(tmp_path / "private")) as components:
        assert components.rcc_archive.read_bytes() == b"verified-rcca"
        assert components.brew_archive.read_bytes() == b"verified-brew"
        assert components.hauler_archive.read_bytes() == b"verified-hauler"


def test_materialize_components_returns_owned_verified_paths_and_manifest(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    restic = _Restic(descriptor)

    with materialize_components(descriptor, restic, parent) as result:
        assert result.rcc_archive.read_bytes() == b"verified-rcca"
        assert (
            json.loads(result.rcc_metadata.read_text())["legacy_blueprint_key"]
            == "legacy-test"
        )
        assert result.brew_archive.read_bytes() == b"verified-brew"
        assert result.hauler_archive.read_bytes() == b"verified-hauler"
        assert set(result.manifest) == {
            "rcc_environment",
            "homebrew_recovery",
            "hauler_content",
        }
        assert (
            result.manifest["hauler_content"]["archive_sha256"]
            == hashlib.sha256(b"verified-hauler").hexdigest()
        )
        assert result.rcc_archive.exists()

    assert not list(parent.iterdir())


def test_export_rejects_capsule_whose_clean_restore_has_different_component_identity(
    tmp_path,
):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs(mismatch=True)

    with pytest.raises(PortableExportError, match="identity"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())


@pytest.mark.parametrize(
    "field",
    [
        "legacy_blueprint_key",
        "archive_sha256",
        "archive_size",
        "rcc_version",
        "platform",
        "robot",
    ],
)
def test_export_rejects_clean_restore_with_changed_rcc_metadata(tmp_path, field):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs(
        restore_metadata_mismatch=field
    )

    with pytest.raises(PortableExportError, match="RCC component identity"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())


def test_export_rejects_saved_hauler_digest_with_changed_native_kind(tmp_path):
    descriptor = _descriptor(
        components={
            **_descriptor().to_dict()["components"],
            "hauler_content": {
                **_descriptor().to_dict()["components"]["hauler_content"],
                "references": [{"digest": "sha256:" + "4" * 64, "kind": "image"}],
            },
        }
    )
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs(hauler_kind_mismatch=True)

    with pytest.raises(PortableExportError, match="Hauler component identities"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())


@pytest.mark.parametrize(
    "artifact",
    [
        "homebrew-recovery.tar.zst",
        "rcc-environment.rcca",
        "rcc-environment-metadata.json",
    ],
)
def test_export_rejects_saved_anchor_bytes_changed_in_composed_capsule(
    tmp_path, artifact
):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs(
        extraction_mismatch=artifact
    )

    with pytest.raises(PortableExportError, match="component identity"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())


def test_export_rejects_capsule_whose_clean_restore_has_different_workspace_content(
    tmp_path,
):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs(workspace_mismatch=True)

    with pytest.raises(PortableExportError, match="content identity"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())


def test_export_is_create_only_and_preserves_existing_output(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    output.write_bytes(b"user data")
    build, inspect, restore, extract, _state = _native_stubs()

    with pytest.raises(PortableExportError, match="already exists"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert output.read_bytes() == b"user data"
    assert not list(parent.iterdir())


def test_export_atomic_promotion_does_not_replace_output_created_during_build(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"

    def create_collision(_source, _kwargs):
        output.write_bytes(b"concurrent output")

    build, inspect, restore, extract, _state = _native_stubs(on_build=create_collision)

    with pytest.raises(PortableExportError, match="already exists"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            run_build_fn=build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert output.read_bytes() == b"concurrent output"
    assert not list(parent.iterdir())


def test_export_cancellation_cleans_all_staging_without_promoting_output(tmp_path):
    descriptor = _descriptor()
    parent = _private_dir(tmp_path / "private")
    output = tmp_path / "portable.haul.tar.zst"
    build, inspect, restore, extract, _state = _native_stubs()

    class CancelAfterBuild:
        cancelled = False

    cancellation = CancelAfterBuild()

    def cancel_build(jat_root, source, staged_output, **kwargs):
        build(jat_root, source, staged_output, **kwargs)
        cancellation.cancelled = True
        return {"success": True, "operation": "build"}

    with pytest.raises(PortableExportError, match="cancel"):
        export_portable_jat(
            descriptor=descriptor,
            restic=_Restic(descriptor),
            staging_parent=parent,
            output=output,
            jat_root=tmp_path / "jat",
            cancellation=cancellation,
            run_build_fn=cancel_build,
            run_inspect_fn=inspect,
            run_restore_fn=restore,
            run_extract_fn=extract,
        )

    assert not output.exists()
    assert not list(parent.iterdir())
