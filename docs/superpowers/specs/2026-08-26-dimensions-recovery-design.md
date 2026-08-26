# Josh Room Dimensions Recovery Design

## Recovery authority

This design reconstructs issue #17 from `main@d44d4cbb908a3637da5456bf788646685131a58b` on
`patchraptor/coherent-dimensions-rollout-rebuild`. The lost candidate
`4efc672373557d00169e300ec4760ec6e11b502b` and its receipts are historical
requirements evidence only. Its Git objects were not recovered, and none of
its reported test results count as verification for this branch.

## Product model

The user-facing hierarchy is:

```text
Provider -> Dimension -> Room -> JAT
```

A Provider is a concrete storage service. A Dimension is one bucket, one
independent encrypted catalog, non-secret connection settings, and one opaque
credential profile. A Room is a living workspace. A JAT is an immutable saved
Room snapshot. Environment Artifacts remain typed runtime payload metadata
carried by a JAT and are not renamed.

Cloudflare R2 and MinIO remain concrete implementations over the existing
ObjectStore seam. This recovery does not introduce a provider plugin framework,
a shared catalog, or a new credential protocol. Existing top-level `r2` and
`minio` private configuration remains readable while named `dimensions` records
become the forward configuration contract.

## Versioned data contracts

`schemas/private-config.schema.json` continues accepting the legacy top-level
backend records. A named Dimension record contains only its label, concrete
provider, endpoint, bucket, region, catalog key, provider-specific non-secret
options, and opaque keyring credential profile. Access keys, secret keys,
session tokens, age identities, and age private material are never valid
Dimension fields.

The v2 Dimension catalog preserves the proven `projects` and `snapshots`
storage vocabulary for compatibility while binding the encrypted catalog to a
`dimension_id`. Each snapshot records the immutable ciphertext key, SHA-256,
size, creation time, and saved workspace fingerprint. In product surfaces,
projects are Rooms and snapshots are JATs.

The v2 `.josh-room.json` marker binds `dimension_id`, `project_id`, and
`snapshot_id`, plus the friendly Room name, saved workspace fingerprint, and a
SHA-256 of the canonical workspace path. It contains no provider credentials or
absolute path. Catalog and marker fields must corroborate before a binding is
trusted. Marker v1 remains a readable legacy input until migration is complete;
Slice 1 defines v2 but does not change current marker writes.

## Dimension routing

Dimension resolution first reads named records. Legacy top-level R2 and MinIO
records are exposed as synthetic `r2` and `minio` Dimensions when a same-named
record does not override them. The selected Dimension supplies one catalog key
and one bucket to the existing concrete backend constructor. Age encryption
stays above storage and is never delegated to a provider.

No catalog is mirrored or promoted globally. A catalog revision conflict stays
fail-closed. Existing v1 local and remote paths remain operational until a
later slice performs an explicit, tested v2 transition.

## Workspace authority

Local status reads only the marker, canonical path binding, and on-disk
fingerprint. It does not contact storage or require authentication. Storage is
used by Link and Repair to corroborate the marker against the encrypted catalog
and immutable object evidence. Repair may replace a stale local ledger only
when independent catalog and object evidence agree; it must not require the
stale value it is repairing.

The saved baseline comes from the verified marker/catalog JAT, not from the
filesystem at extension startup. Disk changes and unsaved editor buffers are
separate signals. Initialization must merge events observed while the baseline
loads, and a successful Save, Enter, Link, or Repair is the only transition
that resets the baseline.

## Copy as New

Dragging a JAT to another Dimension or destination Room creates a new logical
snapshot ID and timestamp without changing the source. Verified destination
ciphertext is reused byte-for-byte; otherwise ciphertext streams once. Josh
Room never decrypts and re-encrypts for copy.

Before catalog publication, the destination object must match SHA-256 and size.
Writes are create-only or conditional. Catalog conflict leaves an explicit
orphan receipt for safe recovery and never exposes a partial JAT. Copy never
runs JAT Build or rebuilds dependencies.

## Native experience

The VS Code extension is the normal surface. It presents Provider sections,
Dimensions, Rooms, and JATs; routes Save, Enter, Link, Repair, Delete, Serve,
and drag/drop through the selected Dimension; and keeps root/template extension
copies byte-identical. Ordinary operation does not require JSON editing,
`kubectl`, `secret-tool`, or backend flags.

## Security and compatibility invariants

- Existing R2, MinIO, local ObjectStore, age, JAT, and RCC Environment Artifact
  behavior remains compatible.
- Cloudflare OAuth remains R2-only; MinIO credentials remain in the configured
  Secret Service/keyring profile.
- Configuration, markers, catalogs, fixtures, logs, receipts, and docs contain
  no plaintext credentials or private age identities.
- Content identity is SHA-256 plus size; ETags are concurrency observations,
  never content identity.
- Catalog writes remain conditional and object writes remain immutable or
  create-only.
- Marker/catalog corroboration is never weakened for convenience.

## Delivery slices

1. Recover this design and implementation plan; define config, catalog-v2, and
   marker-v2 contracts through RED then GREEN tests.
2. Add named Dimension registry and independent catalog routing while retaining
   legacy R2/MinIO defaults.
3. Implement marker v2, fingerprint, auth-free status, Link, and fail-closed
   Repair.
4. Replace startup-relative dirty tracking with an authoritative disk/buffer
   state machine.
5. Add native Provider/Dimension hierarchy and selected-Dimension commands.
6. Add verified ciphertext Copy as New and folder-drop routing.
7. Run full local, native, MinIO, fresh-DevPod, hosted-CI, template-publication,
   secret-scan, and independent-review acceptance.

## Completion boundary

A slice is complete only on current-branch evidence. The full recovery is
complete only after every issue #17 gate passes on the rebuilt exact head and
independent review has no unresolved Critical or Important finding. Missing or
secret-gated evidence is reported as skipped or blocked, never passed.
