# Room Store save and restore engine

`josh_room.room_store_operations.RoomStoreOperations` orchestrates complete
Restic snapshots and validated logical JAT descriptors. It is intentionally
not wired into `operations.py`, `catalog.py`, or the CLI until the Phase 0
provider gates admit the Room Store catalog format.

The constructor receives the selected repository locator, private cache and
credential directories, Dimension/backend, descriptor scope and component
references, plus three catalog callbacks:

- `read_latest() -> (LogicalJat | None, etag | None)` returns the validated
  current recovery point and the exact catalog concurrency token.
- `read_snapshot_entries(snapshot_id)` is the read-only path Preview uses to
  inspect the selected parent tree. It must not upgrade the keyset, initialize
  a repository, or mutate provider state.
- `publish_descriptor(descriptor, expected_etag=..., workspace_signature=...,
  signature_algorithm=...)` owns the existing age
  encryption material and concrete backend. It must publish immutable data
  first, then conditionally replace the catalog using the supplied token. The
  callback reports known failure through an exception with `published=False`,
  a known applied write with `published=True`, or leaves the outcome
  unclassified. An unclassified outcome is uncertain and must be reconciled;
  it is never treated as a rejection.
- `write_marker(descriptor, clean=..., workspace_signature=...,
  signature_algorithm=..., capture_policy_sha256=...)` updates the workspace
  marker only after the catalog commit is known. Its source signature uses the
  explicit `josh-room-stat-v1` algorithm over relative paths, entry types,
  modes, file sizes, and filesystem timestamps. This is not the legacy
  content-hash fingerprint; integration must store it in a versioned marker
  seam and preserve existing v1/v2 marker behavior. Edits observed after
  publication return `saved-but-dirty` and pass `clean=False`.

`ensure_room_store_keyset(dimension, backend)` runs before Restic starts. The
returned canonical base64url secret is written as one ASCII password line in a
short-lived mode-0600 file, removed on every exit path. `initialize()` must return format 2. An
unbound keyset is conditionally bound with its exact generation after
initialization; an existing different repository binding fails closed.
The Restic cache and password directory must be private, separate, and outside
the workspace. The repository locator is injected because endpoint and bucket
selection belong to the provider adapter.

Save loads the canonical `CapturePolicy`, scans file metadata and path types,
rejects special files, filesystem-boundary crossings, Windows-reserved names,
NFC/casefold path collisions, and unsafe symlinks, then calls Restic with the explicit
parent snapshot and policy-generated exclude file. Restic exit 3, cancellation,
malformed output, or source/policy drift prevents descriptor publication. The
saved tree is checked again through Restic's entry stream. A no-op creates no
descriptor or marker update only when the selected latest descriptor has the
same policy, components, source, producer, Room scope, and Restic parent.
`SaveResult.scanned_bytes` is the source file-size total; `data_added_bytes` is
Restic's `data_added` count and is not a network-transfer measurement. Preview
does not claim that scanned bytes were added. Suspicious deletions (at least 25 paths and at least one quarter of
the prior entry count) require the exact token returned by the current preview.

Restore validates the descriptor's Room Store scope, repository and tree
identity, and complete entry graph before Restic restore. It restores into a
new sibling staging directory, validates the materialized tree, then promotes
with a no-replace rename. Existing destinations are never overwritten. The
operation does not apply parent JATs, run JAT build/Hauler, prune, unlock, or
garbage collect repository content.

The injected tests use a fake Restic adapter and fake catalog. They prove
orchestration and failure ordering only; they do not pass MinIO/R2 CAS, Windows
runtime, age encryption, RCC/JAT capture, or packaged CLI acceptance.
