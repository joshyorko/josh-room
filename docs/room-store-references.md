# Room Store reference planner

`josh_room.room_store_references` builds read-only reference indexes and cleanup plans for one Dimension. It does not read providers, mutate catalogs, run `forget`, prune packs, or unlock repositories.

## Build a complete reference index

Call `build_reference_index(catalog, descriptors)` with a validated v3 `Catalog` and a mapping keyed by `(room_id, logical_jat_id)`. Supply one validated `LogicalJat` for every logical catalog record.

The planner revalidates the catalog, corroborates every descriptor with its catalog record, and fails closed when a descriptor is missing, unreadable, or inconsistent. A v3 catalog may also contain legacy records. The planner records each legacy object key but does not treat legacy JAT objects as Restic snapshots.

All logical descriptors in one Dimension must bind to the catalog's Dimension and encryption domain and to one Restic repository ID. Snapshot identity includes that repository ID, so a repeated logical JAT ID in another Room remains a separate catalog alias while shared Restic snapshot IDs deduplicate safely.

## Compare a Restic inventory

Call `compare_restic_inventory(index, repository_info, inventory)` with one immutable repository identity and its Restic inventory. It accepts path-free `SnapshotInventoryItem` records from `ResticStore.snapshots()` and the internal `SnapshotInfo` type used by single-snapshot inspection. The result reports catalog references, corroborated descriptor references, Restic-only orphans, missing snapshots, component-only snapshots, and legacy object keys. It rejects duplicate inventory IDs, repository mismatches, and tree IDs that disagree with descriptors.

`component_only` contains snapshots referenced by components but never by a workspace. A snapshot used by both a workspace and a component is not component-only.

## Plan record removal

Call `plan_catalog_record_removal(index, report, identity)` before removing one Room record. The plan includes the expected catalog revision and orders work as:

1. conditionally remove the catalog reference at that revision;
2. delete catalog objects that no remaining record references;
3. forget Restic snapshots that no remaining logical descriptor references.

The plan lists missing Restic snapshots separately and never proposes deleting them. The caller must stop on a catalog revision conflict and rebuild the index. The planner never runs any step and never proposes prune or unlock.

## Plan orphan cleanup

Call `plan_orphan_cleanup(report, evidence, now=..., grace_period=...)` with explicit repository-bound observations. Evidence must show at least two observations spanning the full positive grace period. Unreferenced snapshots without qualifying evidence remain in `missing_evidence` and cannot enter the eligible set.

The returned plan carries the qualifying evidence for each eligible snapshot. It still performs no cleanup and never proposes prune or unlock.
