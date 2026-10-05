# Dimension Catalog v3

Catalog v3 stores Room Store descriptors beside legacy snapshot records in the same Dimension catalog. The [v3 schema](../schemas/dimension-catalog-v3.schema.json) defines the encrypted catalog's closed JSON shape.

## Version and upgrade

The catalog keeps `dimension_id` and `revision`, and requires the Dimension's UUID4 `encryption_domain_id`. Reading a v1 or v2 catalog does not change its version. `Catalog.add_logical_snapshot(...)` is the only operation that upgrades a v2 catalog to v3. It requires the descriptor's Dimension and encryption domain to match the catalog.

V3 legacy records keep their v2 fields, including `workspace_fingerprint`. A Room Store record uses `payload_kind: "room-store-v1"` and does not contain `workspace_fingerprint`.

## Logical snapshot records

Each logical record points to an age-encrypted, complete LogicalJat JSON object under `objects/sha256/<ciphertext-sha256>`. Its `snapshot_id` equals the descriptor's `logical_jat_id`. The catalog index repeats the descriptor's Dimension, domain, Room, timestamp, repository, workspace snapshot, tree, capture-policy digest, component snapshot and tree references, and byte counts. The index stores the save-time `workspace_signature` with `signature_algorithm: "josh-room-stat-v1"`.

The descriptor remains authoritative. `corroborate_logical_snapshot(project_id, snapshot, descriptor)` checks every repeated index value against the validated descriptor before restore uses it. The index never treats `workspace_signature` as a content fingerprint.

## Same-Dimension copies

A copy receives a new `logical_jat_id` and `room_id`. Its descriptor sets `origin_room_id` to the source Room and retains the workspace and component restic snapshot and tree IDs. The copy writes a new small encrypted descriptor object. It does not transfer the referenced restic payloads.

Catalog removal counts descriptor object keys across all records. It returns an object key for deletion only after the last catalog reference is removed. Restic snapshot and pack identifiers are not object keys and never enter that deletion list.
