"""Pure reference planning for a Dimension's shared Room Store repository."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from .catalog import Catalog, corroborate_logical_snapshot
from .logical_jat import LogicalJat
from .restic_store import RepositoryInfo, SnapshotInfo, SnapshotInventoryItem

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMPONENTS = frozenset({"rcc_environment", "homebrew_recovery", "hauler_content"})


@dataclass(frozen=True, order=True, slots=True)
class LogicalJatIdentity:
    room_id: str
    logical_jat_id: str


@dataclass(frozen=True, order=True, slots=True)
class ResticSnapshotRef:
    repository_id: str
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class SnapshotReferences:
    catalog_records: frozenset[LogicalJatIdentity]
    descriptor_records: frozenset[LogicalJatIdentity]
    roles: frozenset[str]
    tree_id: str


@dataclass(frozen=True, slots=True)
class CatalogRecordReferences:
    identity: LogicalJatIdentity
    payload_kind: str
    object_key: str
    restic_snapshots: frozenset[ResticSnapshotRef]


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    dimension_id: str
    encryption_domain_id: str
    catalog_revision: int
    repository_id: str | None
    fingerprint: str
    records: Mapping[LogicalJatIdentity, CatalogRecordReferences]
    references: Mapping[ResticSnapshotRef, SnapshotReferences]
    legacy_objects: frozenset[str]

    @property
    def catalog_referenced(self) -> frozenset[ResticSnapshotRef]:
        return frozenset(
            reference
            for reference, value in self.references.items()
            if value.catalog_records
        )

    @property
    def descriptor_referenced(self) -> frozenset[ResticSnapshotRef]:
        return frozenset(
            reference
            for reference, value in self.references.items()
            if value.descriptor_records
        )


@dataclass(frozen=True, slots=True)
class ResticReachabilityReport:
    repository_id: str
    catalog_revision: int
    reference_index_fingerprint: str
    catalog_referenced: frozenset[ResticSnapshotRef]
    descriptor_referenced: frozenset[ResticSnapshotRef]
    inventory_snapshot_ids: frozenset[ResticSnapshotRef]
    restic_only_orphans: frozenset[ResticSnapshotRef]
    missing_from_restic: frozenset[ResticSnapshotRef]
    component_only: frozenset[ResticSnapshotRef]
    legacy_objects: frozenset[str]


@dataclass(frozen=True, slots=True)
class CatalogRecordRemovalPlan:
    catalog_reference_to_remove: LogicalJatIdentity
    expected_catalog_revision: int
    order: tuple[str, str, str]
    object_keys_to_delete: frozenset[str]
    snapshot_ids_to_forget: frozenset[ResticSnapshotRef]
    missing_snapshot_ids: frozenset[ResticSnapshotRef]
    never_prune: bool = True
    never_unlock: bool = True


@dataclass(frozen=True, slots=True)
class GracePeriodEvidence:
    repository_id: str
    snapshot_id: str
    first_observed_at: datetime
    last_observed_at: datetime
    observation_count: int


@dataclass(frozen=True, slots=True)
class OrphanCleanupPlan:
    eligible_snapshot_ids: frozenset[ResticSnapshotRef]
    missing_evidence: frozenset[ResticSnapshotRef]
    evidence: Mapping[ResticSnapshotRef, GracePeriodEvidence]
    grace_period: timedelta
    never_prune: bool = True
    never_unlock: bool = True


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _mutable_catalog(catalog: Catalog) -> Catalog:
    if not isinstance(catalog, Catalog):
        raise TypeError("Dimension catalog is invalid")
    try:
        return Catalog.from_body(
            catalog.body,
            catalog.dimension_id,
            catalog.encryption_domain_id,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Dimension catalog is invalid") from error


def build_reference_index(
    catalog: Catalog,
    descriptors: Mapping[tuple[str, str], LogicalJat],
) -> ReferenceIndex:
    """Index a complete v3 catalog only after every logical descriptor corroborates."""
    catalog = _mutable_catalog(catalog)
    if catalog.body["format_version"] != 3:
        raise ValueError("Room Store reference index requires catalog v3")
    dimension_id = catalog.dimension_id
    encryption_domain_id = catalog.encryption_domain_id
    if not isinstance(dimension_id, str) or not isinstance(encryption_domain_id, str):
        raise TypeError("Room Store Dimension and encryption-domain bindings are invalid")
    if not isinstance(descriptors, Mapping):
        raise TypeError("logical descriptor mapping is invalid")

    expected_keys: set[tuple[str, str]] = set()
    for room_id, project in catalog.body["projects"].items():
        for snapshot_id, snapshot in project["snapshots"].items():
            if "payload_kind" in snapshot:
                if snapshot["payload_kind"] != "room-store-v1":
                    raise ValueError("catalog contains an unsupported payload kind")
                expected_keys.add((room_id, snapshot_id))
    supplied_keys = set(descriptors)
    if supplied_keys != expected_keys:
        raise ValueError("logical descriptor set is incomplete or contains unknown entries")

    records: dict[LogicalJatIdentity, CatalogRecordReferences] = {}
    catalog_records_by_snapshot: dict[ResticSnapshotRef, set[LogicalJatIdentity]] = {}
    descriptor_records_by_snapshot: dict[ResticSnapshotRef, set[LogicalJatIdentity]] = {}
    roles_by_snapshot: dict[ResticSnapshotRef, set[str]] = {}
    trees_by_snapshot: dict[ResticSnapshotRef, str] = {}
    repository_ids: set[str] = set()
    legacy_objects: set[str] = set()

    def add_reference(
        reference: ResticSnapshotRef,
        tree_id: str,
        identity: LogicalJatIdentity,
        role: str,
    ) -> None:
        previous_tree = trees_by_snapshot.setdefault(reference, tree_id)
        if previous_tree != tree_id:
            raise ValueError("Room Store snapshot tree binding mismatch")
        catalog_records_by_snapshot.setdefault(reference, set()).add(identity)
        descriptor_records_by_snapshot.setdefault(reference, set()).add(identity)
        roles_by_snapshot.setdefault(reference, set()).add(role)

    for room_id, project in catalog.body["projects"].items():
        for snapshot_id, record in project["snapshots"].items():
            identity = LogicalJatIdentity(room_id, snapshot_id)
            object_key = record["object_key"]
            if "payload_kind" not in record:
                legacy_objects.add(object_key)
                records[identity] = CatalogRecordReferences(
                    identity=identity,
                    payload_kind="legacy-jat-hauler-v1",
                    object_key=object_key,
                    restic_snapshots=frozenset(),
                )
                continue

            descriptor = descriptors[(room_id, snapshot_id)]
            if not isinstance(descriptor, LogicalJat):
                raise TypeError("logical descriptor is invalid")
            try:
                corroborate_logical_snapshot(room_id, record, descriptor)
            except (TypeError, ValueError) as error:
                raise ValueError("logical descriptor does not corroborate the catalog") from error
            body = descriptor.to_dict()
            if body["dimension_id"] != dimension_id or body["encryption_domain_id"] != encryption_domain_id:
                raise ValueError("logical descriptor Dimension binding mismatch")
            workspace = body["workspace"]
            repository_id = workspace["repository_id"]
            if not _valid_digest(repository_id):
                raise ValueError("logical descriptor repository binding is invalid")
            repository_ids.add(repository_id)
            workspace_ref = ResticSnapshotRef(repository_id, workspace["snapshot_id"])
            add_reference(workspace_ref, workspace["tree_id"], identity, "workspace")
            record_refs = {workspace_ref}
            for component_name in _COMPONENTS:
                component = body["components"][component_name]
                if component is None:
                    continue
                snapshot = component["snapshot"]
                component_ref = ResticSnapshotRef(snapshot["repository_id"], snapshot["snapshot_id"])
                add_reference(component_ref, snapshot["tree_id"], identity, component_name)
                record_refs.add(component_ref)
            records[identity] = CatalogRecordReferences(
                identity=identity,
                payload_kind="room-store-v1",
                object_key=object_key,
                restic_snapshots=frozenset(record_refs),
            )

    if len(repository_ids) > 1:
        raise ValueError("logical descriptors use different Room Store repository bindings")
    references = {
        reference: SnapshotReferences(
            catalog_records=frozenset(catalog_records_by_snapshot[reference]),
            descriptor_records=frozenset(descriptor_records_by_snapshot[reference]),
            roles=frozenset(roles_by_snapshot[reference]),
            tree_id=trees_by_snapshot[reference],
        )
        for reference in trees_by_snapshot
    }
    fingerprint_body = {
        "dimension_id": dimension_id,
        "encryption_domain_id": encryption_domain_id,
        "catalog_revision": catalog.body["revision"],
        "repository_id": next(iter(repository_ids), None),
        "records": [
            {
                "room_id": identity.room_id,
                "logical_jat_id": identity.logical_jat_id,
                "payload_kind": record.payload_kind,
                "object_key": record.object_key,
                "restic_snapshots": [
                    [reference.repository_id, reference.snapshot_id]
                    for reference in sorted(record.restic_snapshots)
                ],
            }
            for identity, record in sorted(records.items())
        ],
        "references": [
            {
                "repository_id": reference.repository_id,
                "snapshot_id": reference.snapshot_id,
                "tree_id": trees_by_snapshot[reference],
                "roles": sorted(roles_by_snapshot[reference]),
                "catalog_records": [
                    [identity.room_id, identity.logical_jat_id]
                    for identity in sorted(catalog_records_by_snapshot[reference])
                ],
                "descriptor_records": [
                    [identity.room_id, identity.logical_jat_id]
                    for identity in sorted(descriptor_records_by_snapshot[reference])
                ],
            }
            for reference in sorted(trees_by_snapshot)
        ],
        "legacy_objects": sorted(legacy_objects),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReferenceIndex(
        dimension_id=dimension_id,
        encryption_domain_id=encryption_domain_id,
        catalog_revision=catalog.body["revision"],
        repository_id=next(iter(repository_ids), None),
        fingerprint=fingerprint,
        records=MappingProxyType(records),
        references=MappingProxyType(references),
        legacy_objects=frozenset(legacy_objects),
    )


def compare_restic_inventory(
    index: ReferenceIndex,
    repository: RepositoryInfo,
    inventory: list[SnapshotInfo | SnapshotInventoryItem]
    | tuple[SnapshotInfo | SnapshotInventoryItem, ...],
) -> ResticReachabilityReport:
    if not isinstance(index, ReferenceIndex):
        raise TypeError("Room Store reference index is invalid")
    if not isinstance(repository, RepositoryInfo) or not _valid_digest(repository.repository_id):
        raise ValueError("Restic repository identity is invalid")
    if type(repository.repository_format) is not int or repository.repository_format != 2:
        raise ValueError("Restic repository format is unsupported")
    if index.repository_id is not None and index.repository_id != repository.repository_id:
        raise ValueError("Restic inventory belongs to another Room Store repository")
    if not isinstance(inventory, (list, tuple)):
        raise TypeError("Restic snapshot inventory is invalid")
    inventory_trees: dict[ResticSnapshotRef, str] = {}
    for snapshot in inventory:
        if not isinstance(snapshot, (SnapshotInfo, SnapshotInventoryItem)):
            raise TypeError("Restic snapshot inventory contains an invalid entry")
        if not _valid_digest(snapshot.snapshot_id) or not _valid_digest(snapshot.tree_id):
            raise ValueError("Restic snapshot inventory identity is invalid")
        reference = ResticSnapshotRef(repository.repository_id, snapshot.snapshot_id)
        if reference in inventory_trees:
            raise ValueError("Restic snapshot inventory contains duplicate IDs")
        inventory_trees[reference] = snapshot.tree_id
    for reference, value in index.references.items():
        observed_tree = inventory_trees.get(reference)
        if observed_tree is not None and observed_tree != value.tree_id:
            raise ValueError("Restic snapshot tree does not match the logical descriptor")

    catalog_referenced = index.catalog_referenced
    descriptor_referenced = index.descriptor_referenced
    referenced = catalog_referenced | descriptor_referenced
    inventory_ids = frozenset(inventory_trees)
    component_only = frozenset(
        reference
        for reference, value in index.references.items()
        if "workspace" not in value.roles and value.roles & _COMPONENTS
    )
    return ResticReachabilityReport(
        repository_id=repository.repository_id,
        catalog_revision=index.catalog_revision,
        reference_index_fingerprint=index.fingerprint,
        catalog_referenced=catalog_referenced,
        descriptor_referenced=descriptor_referenced,
        inventory_snapshot_ids=inventory_ids,
        restic_only_orphans=inventory_ids - referenced,
        missing_from_restic=referenced - inventory_ids,
        component_only=component_only,
        legacy_objects=index.legacy_objects,
    )


def plan_catalog_record_removal(
    index: ReferenceIndex,
    report: ResticReachabilityReport,
    identity: LogicalJatIdentity,
) -> CatalogRecordRemovalPlan:
    if not isinstance(index, ReferenceIndex) or not isinstance(report, ResticReachabilityReport):
        raise TypeError("Room Store deletion inputs are invalid")
    if (
        report.catalog_revision != index.catalog_revision
        or report.reference_index_fingerprint != index.fingerprint
    ):
        raise ValueError("Restic reachability report belongs to another catalog index")
    if not isinstance(identity, LogicalJatIdentity) or identity not in index.records:
        raise ValueError("catalog record is not present in the reference index")
    if report.repository_id != (index.repository_id or report.repository_id):
        raise ValueError("Restic inventory belongs to another Room Store repository")
    target = index.records[identity]
    remaining = {key: record for key, record in index.records.items() if key != identity}
    remaining_snapshots = {
        reference
        for record in remaining.values()
        for reference in record.restic_snapshots
    }
    unreachable = target.restic_snapshots - remaining_snapshots
    existing = unreachable & report.inventory_snapshot_ids
    missing = unreachable - report.inventory_snapshot_ids
    object_still_referenced = any(
        record.object_key == target.object_key
        for record in remaining.values()
    )
    object_keys = frozenset() if object_still_referenced else frozenset({target.object_key})
    return CatalogRecordRemovalPlan(
        catalog_reference_to_remove=identity,
        expected_catalog_revision=index.catalog_revision,
        order=(
            "remove-catalog-reference",
            "delete-unreferenced-catalog-objects",
            "forget-unreachable-restic-snapshots",
        ),
        object_keys_to_delete=object_keys,
        snapshot_ids_to_forget=frozenset(existing),
        missing_snapshot_ids=frozenset(missing),
    )


def plan_orphan_cleanup(
    report: ResticReachabilityReport,
    evidence: Mapping[ResticSnapshotRef, GracePeriodEvidence],
    *,
    now: datetime,
    grace_period: timedelta,
) -> OrphanCleanupPlan:
    if not isinstance(report, ResticReachabilityReport):
        raise TypeError("Restic reachability report is invalid")
    if not isinstance(evidence, Mapping):
        raise TypeError("orphan grace-period evidence is invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("orphan cleanup time must include a timezone")
    if not isinstance(grace_period, timedelta) or grace_period <= timedelta(0):
        raise ValueError("orphan cleanup requires a positive grace period")
    unexpected = set(evidence) - report.restic_only_orphans
    if unexpected:
        raise ValueError("grace-period evidence names a non-orphan snapshot")
    eligible: set[ResticSnapshotRef] = set()
    missing: set[ResticSnapshotRef] = set()
    eligible_evidence: dict[ResticSnapshotRef, GracePeriodEvidence] = {}
    for reference in report.restic_only_orphans:
        item = evidence.get(reference)
        if not isinstance(item, GracePeriodEvidence):
            missing.add(reference)
            continue
        timestamps = (item.first_observed_at, item.last_observed_at)
        if any(not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            missing.add(reference)
            continue
        qualified = (
            item.repository_id == report.repository_id
            and item.snapshot_id == reference.snapshot_id
            and type(item.observation_count) is int
            and item.observation_count >= 2
            and item.first_observed_at <= item.last_observed_at <= now
            and now - item.first_observed_at >= grace_period
        )
        if qualified:
            eligible.add(reference)
            eligible_evidence[reference] = item
        else:
            missing.add(reference)
    return OrphanCleanupPlan(
        eligible_snapshot_ids=frozenset(eligible),
        missing_evidence=frozenset(missing),
        evidence=MappingProxyType(eligible_evidence),
        grace_period=grace_period,
    )
