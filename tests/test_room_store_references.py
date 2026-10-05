from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from josh_room.catalog import Catalog
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat
from josh_room.restic_store import RepositoryInfo, SnapshotInfo, SnapshotInventoryItem
from josh_room.room_store_references import (
    GracePeriodEvidence,
    LogicalJatIdentity,
    ResticSnapshotRef,
    build_reference_index,
    compare_restic_inventory,
    plan_catalog_record_removal,
    plan_orphan_cleanup,
)

DIMENSION = "archive"
DOMAIN = "00000000-0000-4000-8000-000000000001"
REPOSITORY = "a" * 64


def _component(name: str, snapshot_id: str, tree_id: str) -> dict:
    snapshot = {
        "repository_id": REPOSITORY,
        "repository_format": 2,
        "snapshot_id": snapshot_id,
        "tree_id": tree_id,
    }
    shared = {
        "snapshot": snapshot,
        "archive_sha256": "e" * 64,
        "archive_size": 100,
        "member_basename": {
            "rcc_environment": "rcc-environment.rcca",
            "homebrew_recovery": "homebrew-recovery.tar.zst",
            "hauler_content": "hauler-content.tar.zst",
        }[name],
    }
    if name == "rcc_environment":
        return {
            "kind": "rcca",
            **shared,
            "artifact_digest": "sha256:" + "b" * 64,
            "specification_digest": "sha256:" + "c" * 64,
            "platform": "linux-x64",
            "rcc_version": "v18.19.5",
            "robot_relative_path": "robot.yaml",
        }
    if name == "homebrew_recovery":
        return {"kind": "homebrew-recovery", **shared}
    return {
        "kind": "hauler-content",
        **shared,
        "references": [{
            "digest": "sha256:" + "d" * 64,
            "media_type": "application/vnd.oci.image.manifest.v1+json",
        }],
    }


def _descriptor(
    room_id: str,
    logical_id: str,
    workspace_snapshot: str,
    *,
    workspace_tree: str | None = None,
    components: dict | None = None,
    repository_id: str = REPOSITORY,
    dimension_id: str = DIMENSION,
    domain_id: str = DOMAIN,
) -> LogicalJat:
    workspace_tree = workspace_tree or hashlib.sha256((workspace_snapshot + "tree").encode()).hexdigest()
    component_values = components or {
        "rcc_environment": None,
        "homebrew_recovery": None,
        "hauler_content": None,
    }
    for value in component_values.values():
        if value is not None:
            value["snapshot"]["repository_id"] = repository_id
    return LogicalJat.from_dict({
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": logical_id,
        "dimension_id": dimension_id,
        "encryption_domain_id": domain_id,
        "room_id": room_id,
        "created_at": "2026-10-02T12:00:00Z",
        "capture_policy_sha256": "f" * 64,
        "workspace": {
            "engine": "restic",
            "repository_id": repository_id,
            "repository_format": 2,
            "snapshot_id": workspace_snapshot,
            "tree_id": workspace_tree,
            "source_path": ".",
            "logical_bytes": 100,
            "data_added": 40,
            "data_added_packed": 30,
        },
        "components": component_values,
        "source": {},
        "producer": {
            "josh_room_version": "0.1.0",
            "restic_version": "0.19.1",
            "source_platform": "linux-x64",
            "restore_platforms": ["linux-x64", "win32-x64"],
        },
    })


def _catalog_with(*descriptors: LogicalJat, legacy: bool = False) -> Catalog:
    catalog = Catalog.empty(DIMENSION, DOMAIN)
    for index, descriptor in enumerate(descriptors):
        digest = hashlib.sha256(f"descriptor-{index}".encode()).hexdigest()
        catalog = catalog.add_logical_snapshot(
            descriptor.to_dict()["room_id"],
            descriptor.to_dict()["room_id"],
            descriptor,
            ObjectRef(f"objects/sha256/{digest}", digest, 100),
            "9" * 64,
        )
    if legacy:
        digest = "8" * 64
        catalog = catalog.add_snapshot(
            "legacy-room",
            "Legacy Room",
            {
                "snapshot_id": "legacy-one",
                "object_key": f"objects/sha256/{digest}",
                "ciphertext_sha256": digest,
                "ciphertext_size": 100,
                "created_at": "2026-10-01T12:00:00+00:00",
                "workspace_fingerprint": "7" * 64,
            },
        )
    return catalog


def _descriptor_map(*descriptors: LogicalJat):
    return {
        (body["room_id"], body["logical_jat_id"]): descriptor
        for descriptor in descriptors
        for body in (descriptor.to_dict(),)
    }


def _inventory(*snapshot_ids: str) -> list[SnapshotInfo]:
    trees = {}
    return _inventory_with_trees(*snapshot_ids, trees=trees)


def _inventory_with_trees(*snapshot_ids: str, trees: dict[str, str]) -> list[SnapshotInfo]:
    return [
        SnapshotInfo(
            snapshot_id=snapshot_id,
            tree_id=trees.get(snapshot_id, hashlib.sha256((snapshot_id + "tree").encode()).hexdigest()),
            parent_snapshot_id=None,
            time="2026-10-02T12:00:00Z",
            paths=("/workspace",),
        )
        for snapshot_id in snapshot_ids
    ]


def _path_free_inventory(*snapshot_ids: str) -> tuple[SnapshotInventoryItem, ...]:
    return tuple(
        SnapshotInventoryItem(
            snapshot_id=snapshot_id,
            tree_id=hashlib.sha256((snapshot_id + "tree").encode()).hexdigest(),
            time="2026-10-02T12:00:00Z",
            parent_snapshot_id=None,
        )
        for snapshot_id in snapshot_ids
    )


def test_index_counts_shared_workspace_and_component_refs_across_rooms():
    shared_component = "2" * 64
    component = _component("rcc_environment", shared_component, "3" * 64)
    first = _descriptor(
        "room-a", "jat-a1", "1" * 64,
        components={"rcc_environment": component, "homebrew_recovery": None, "hauler_content": None},
    )
    second = _descriptor(
        "room-a", "jat-a2", "4" * 64,
        components={"rcc_environment": _component("rcc_environment", shared_component, "3" * 64), "homebrew_recovery": None, "hauler_content": None},
    )
    alias = _descriptor(
        "room-b", "jat-b1", "2" * 64, workspace_tree="3" * 64,
        components={"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
    )
    catalog = _catalog_with(first, second, alias, legacy=True)

    index = build_reference_index(catalog, _descriptor_map(first, second, alias))
    report = compare_restic_inventory(
        index,
        RepositoryInfo(REPOSITORY, 2),
        _inventory_with_trees(
            "1" * 64, "2" * 64, "4" * 64, "5" * 64,
            trees={"1" * 64: first.to_dict()["workspace"]["tree_id"], "2" * 64: "3" * 64, "4" * 64: second.to_dict()["workspace"]["tree_id"]},
        ),
    )

    shared = ResticSnapshotRef(REPOSITORY, shared_component)
    assert report.catalog_referenced == report.descriptor_referenced
    assert len(index.references[shared].catalog_records) == 3
    assert len(index.references[shared].descriptor_records) == 3
    assert report.component_only == frozenset()
    assert report.restic_only_orphans == frozenset({ResticSnapshotRef(REPOSITORY, "5" * 64)})
    assert report.legacy_objects == frozenset({"objects/sha256/" + "8" * 64})


def test_component_only_and_missing_snapshot_are_distinct_from_orphans():
    component_id = "2" * 64
    descriptor = _descriptor(
        "room-a",
        "jat-a1",
        "1" * 64,
        components={"rcc_environment": _component("rcc_environment", component_id, "3" * 64), "homebrew_recovery": None, "hauler_content": None},
    )
    missing = "4" * 64
    missing_descriptor = _descriptor("room-a", "jat-a2", missing)
    catalog = _catalog_with(descriptor, missing_descriptor)
    index = build_reference_index(catalog, _descriptor_map(descriptor, missing_descriptor))

    report = compare_restic_inventory(
        index,
        RepositoryInfo(REPOSITORY, 2),
        _inventory_with_trees(
            "1" * 64, component_id, "5" * 64,
            trees={"1" * 64: descriptor.to_dict()["workspace"]["tree_id"], component_id: "3" * 64},
        ),
    )

    assert report.component_only == frozenset({ResticSnapshotRef(REPOSITORY, component_id)})
    assert report.missing_from_restic == frozenset({ResticSnapshotRef(REPOSITORY, missing)})
    assert report.restic_only_orphans == frozenset({ResticSnapshotRef(REPOSITORY, "5" * 64)})


def test_inventory_comparison_accepts_the_path_free_native_inventory_type():
    descriptor = _descriptor("room-a", "jat-a1", "1" * 64)
    index = build_reference_index(_catalog_with(descriptor), _descriptor_map(descriptor))

    report = compare_restic_inventory(
        index,
        RepositoryInfo(REPOSITORY, 2),
        _path_free_inventory("1" * 64),
    )

    assert report.catalog_referenced == frozenset({ResticSnapshotRef(REPOSITORY, "1" * 64)})


def test_index_fails_closed_when_any_logical_descriptor_is_missing_or_unreadable():
    first = _descriptor("room-a", "jat-a1", "1" * 64)
    second = _descriptor("room-b", "jat-b1", "2" * 64)
    catalog = _catalog_with(first, second)

    with pytest.raises(ValueError, match="descriptor set is incomplete"):
        build_reference_index(catalog, _descriptor_map(first))
    with pytest.raises(TypeError, match="descriptor is invalid"):
        build_reference_index(catalog, {**_descriptor_map(first, second), ("room-b", "jat-b1"): None})


def test_index_rejects_descriptor_catalog_corroboration_mismatch():
    descriptor = _descriptor("room-a", "jat-a1", "1" * 64)
    catalog = _catalog_with(descriptor)
    wrong_descriptor = _descriptor("room-a", "jat-a1", "2" * 64)

    with pytest.raises(ValueError, match="does not corroborate the catalog"):
        build_reference_index(catalog, _descriptor_map(wrong_descriptor))


def test_index_rejects_unknown_payload_and_mixed_repository_bindings():
    descriptor = _descriptor("room-a", "jat-a1", "1" * 64)
    unknown_catalog = _catalog_with(descriptor)
    unknown_catalog.body["projects"]["room-a"]["snapshots"]["jat-a1"]["payload_kind"] = "future-v9"
    with pytest.raises(ValueError, match="catalog is invalid"):
        build_reference_index(unknown_catalog, _descriptor_map(descriptor))

    other_repo = "b" * 64
    differently_bound = _descriptor("room-b", "jat-b1", "2" * 64, repository_id=other_repo)
    catalog = _catalog_with(descriptor, differently_bound)
    with pytest.raises(ValueError, match="repository binding"):
        build_reference_index(catalog, _descriptor_map(descriptor, differently_bound))


def test_same_logical_id_in_different_rooms_keeps_distinct_aliases():
    first = _descriptor("room-a", "jat-shared-name", "1" * 64)
    second = _descriptor("room-b", "jat-shared-name", "2" * 64)
    catalog = _catalog_with(first, second)

    index = build_reference_index(catalog, _descriptor_map(first, second))

    assert set(index.records) == {
        LogicalJatIdentity("room-a", "jat-shared-name"),
        LogicalJatIdentity("room-b", "jat-shared-name"),
    }


def test_duplicate_snapshot_refs_are_deduplicated_but_keep_each_role():
    repeated = "1" * 64
    descriptor = _descriptor(
        "room-a",
        "jat-a1",
        repeated,
        workspace_tree="2" * 64,
        components={
            "rcc_environment": _component("rcc_environment", repeated, "2" * 64),
            "homebrew_recovery": _component("homebrew_recovery", repeated, "2" * 64),
            "hauler_content": None,
        },
    )
    index = build_reference_index(_catalog_with(descriptor), _descriptor_map(descriptor))
    reference = ResticSnapshotRef(REPOSITORY, repeated)

    assert len(index.references) == 1
    assert index.references[reference].catalog_records == frozenset({LogicalJatIdentity("room-a", "jat-a1")})
    assert index.references[reference].roles == frozenset({"workspace", "rcc_environment", "homebrew_recovery"})


def test_delete_plan_removes_catalog_reference_before_only_unshared_snapshots():
    shared_component = "2" * 64
    first = _descriptor(
        "room-a", "jat-a1", "1" * 64,
        components={"rcc_environment": _component("rcc_environment", shared_component, "3" * 64), "homebrew_recovery": None, "hauler_content": None},
    )
    second = _descriptor(
        "room-b", "jat-b1", "4" * 64,
        components={"rcc_environment": _component("rcc_environment", shared_component, "3" * 64), "homebrew_recovery": None, "hauler_content": None},
    )
    catalog = _catalog_with(first, second)
    index = build_reference_index(catalog, _descriptor_map(first, second))
    report = compare_restic_inventory(
        index,
        RepositoryInfo(REPOSITORY, 2),
        _inventory_with_trees(
            "1" * 64, shared_component, "4" * 64,
            trees={"1" * 64: first.to_dict()["workspace"]["tree_id"], shared_component: "3" * 64, "4" * 64: second.to_dict()["workspace"]["tree_id"]},
        ),
    )

    plan = plan_catalog_record_removal(index, report, LogicalJatIdentity("room-a", "jat-a1"))

    assert plan.catalog_reference_to_remove == LogicalJatIdentity("room-a", "jat-a1")
    assert plan.expected_catalog_revision == catalog.body["revision"]
    assert plan.order == (
        "remove-catalog-reference",
        "delete-unreferenced-catalog-objects",
        "forget-unreachable-restic-snapshots",
    )
    assert plan.snapshot_ids_to_forget == frozenset({ResticSnapshotRef(REPOSITORY, "1" * 64)})
    assert plan.object_keys_to_delete == frozenset({catalog.body["projects"]["room-a"]["snapshots"]["jat-a1"]["object_key"]})
    assert plan.never_prune is True
    assert plan.never_unlock is True


def test_delete_plan_rejects_a_report_from_another_catalog_index():
    first = _descriptor("room-a", "jat-a1", "1" * 64)
    other = _descriptor("room-a", "jat-b1", "2" * 64)
    first_index = build_reference_index(_catalog_with(first), _descriptor_map(first))
    other_index = build_reference_index(_catalog_with(other), _descriptor_map(other))
    report = compare_restic_inventory(
        first_index,
        RepositoryInfo(REPOSITORY, 2),
        _inventory("1" * 64, "2" * 64),
    )

    with pytest.raises(ValueError, match="another catalog index"):
        plan_catalog_record_removal(
            other_index,
            report,
            LogicalJatIdentity("room-a", "jat-b1"),
        )


def test_delete_plan_keeps_a_catalog_object_still_referenced_by_another_room():
    first = _descriptor("room-a", "jat-a1", "1" * 64)
    second = _descriptor("room-b", "jat-b1", "2" * 64)
    catalog = _catalog_with(first, second)
    first_record = catalog.body["projects"]["room-a"]["snapshots"]["jat-a1"]
    second_record = catalog.body["projects"]["room-b"]["snapshots"]["jat-b1"]
    second_record["object_key"] = first_record["object_key"]
    second_record["ciphertext_sha256"] = first_record["ciphertext_sha256"]
    catalog = Catalog.from_body(catalog.body, DIMENSION, DOMAIN)
    index = build_reference_index(catalog, _descriptor_map(first, second))
    report = compare_restic_inventory(
        index,
        RepositoryInfo(REPOSITORY, 2),
        _inventory("1" * 64, "2" * 64),
    )

    plan = plan_catalog_record_removal(index, report, LogicalJatIdentity("room-a", "jat-a1"))

    assert not plan.object_keys_to_delete


def test_orphan_cleanup_requires_explicit_repository_bound_grace_evidence():
    descriptor = _descriptor("room-a", "jat-a1", "1" * 64)
    index = build_reference_index(_catalog_with(descriptor), _descriptor_map(descriptor))
    orphan = ResticSnapshotRef(REPOSITORY, "5" * 64)
    report = compare_restic_inventory(index, RepositoryInfo(REPOSITORY, 2), _inventory("1" * 64, "5" * 64))
    now = datetime(2026, 10, 10, tzinfo=UTC)

    blocked = plan_orphan_cleanup(report, {}, now=now, grace_period=timedelta(days=7))
    assert blocked.eligible_snapshot_ids == frozenset()
    assert blocked.missing_evidence == frozenset({orphan})

    evidence = GracePeriodEvidence(
        repository_id=REPOSITORY,
        snapshot_id=orphan.snapshot_id,
        first_observed_at=now - timedelta(days=8),
        last_observed_at=now - timedelta(hours=1),
        observation_count=2,
    )
    eligible = plan_orphan_cleanup(
        report,
        {orphan: evidence},
        now=now,
        grace_period=timedelta(days=7),
    )
    assert eligible.eligible_snapshot_ids == frozenset({orphan})
    assert eligible.evidence[orphan] == evidence
    assert eligible.never_prune is True
    assert eligible.never_unlock is True


@pytest.mark.parametrize(
    "evidence",
    [
        lambda ref, now: GracePeriodEvidence(REPOSITORY, ref.snapshot_id, now, now, 1),
        lambda ref, now: GracePeriodEvidence("b" * 64, ref.snapshot_id, now - timedelta(days=9), now, 2),
    ],
)
def test_orphan_cleanup_rejects_unqualified_grace_evidence(evidence):
    descriptor = _descriptor("room-a", "jat-a1", "1" * 64)
    index = build_reference_index(_catalog_with(descriptor), _descriptor_map(descriptor))
    orphan = ResticSnapshotRef(REPOSITORY, "5" * 64)
    report = compare_restic_inventory(index, RepositoryInfo(REPOSITORY, 2), _inventory("1" * 64, orphan.snapshot_id))
    now = datetime(2026, 10, 10, tzinfo=UTC)

    plan = plan_orphan_cleanup(report, {orphan: evidence(orphan, now)}, now=now, grace_period=timedelta(days=7))

    assert not plan.eligible_snapshot_ids
    assert plan.missing_evidence == frozenset({orphan})
