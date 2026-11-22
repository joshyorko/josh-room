# Historical file recovery (draft)

`josh-room recovery tree|inspect|extract ROOM --dimension DIMENSION --snapshot EXACT_ID --path RELATIVE --json`
uses the existing authenticated catalog/descriptor context, including ciphertext
verification and Dimension credentials. `latest` is rejected. The selected
Logical JAT must match its catalog, Dimension, encryption domain and repository.
The Restic snapshot tree must match the descriptor. There are no catalog writes,
Room hydration, migrations or marker updates.

`tree` supports `--offset N --limit N` (1–500 entries per page) within the selected
directory; the returned `next_offset` resumes that same immutable directory.
Directories are read on demand via content-addressed tree blobs. There is no
global workspace item ceiling, but each tree/blob must fit the existing bounded
Restic capture (4 MiB). A larger directory fails closed rather than appearing
complete. Paging rereads the selected directory; this is not a persisted index.

`inspect` reports a SHA-256 and size. Plaintext preview is withheld pending
capture/sensitivity/trust-policy admission. Browsing does not read file content.
Filenames are returned only to the authenticated caller, never written into a
new remote index. The existing Restic cache remains governed by its private
runtime lifecycle.

`extract --destination ABSENT_PATH [--dry-run]` recovers exactly one file or
subtree, never the root workspace. Dry-run reads metadata only. Recovery rejects
links, special files, portable case/Unicode collisions, traversal and Room
markers, with a 10,000-entry / 64 MiB logical content budget. It stages beside
the selected destination privately, verifies content-addressed blobs and staged
file hashes, and uses atomic no-replace promotion. Existing destinations are
never overwritten. Parent aliases/symlinks/reparse points are rejected. Do not
select a destination under a parent concurrently controlled by another user.
No historical code is executed or served.

| Format | Capability |
| --- | --- |
| Logical JAT / Room Store v1 | On-demand tree, digest inspection, selective recovery |
| Portable source-only / Hauler archive | Unsupported; use existing whole-Room restore explicitly |

Receipts distinguish `logical_bytes`, `blob_bytes_read` and `blob_calls` from
`network_bytes_transferred` (null: unmeasured). Restic can download complete
encrypted packs containing unrelated chunks. Plaintext blob counts do **not**
prove wire-transfer savings. Metadata/authentication calls are outside blob
counters. Cancellation between reads and before promotion cleans staging;
individual Restic commands retain the existing deadline.

Initial Linux synthetic acceptance with real pinned Restic 0.19.1: 3 snapshots,
six-byte historical file, 2 MiB unrelated binary: 2,552 plaintext blob bytes,
3 blob calls for listing plus selected recovery. Current file preserved;
Unicode directory recovered separately. This uses synthetic catalog authority
and a real local encrypted Restic repository, not a live object-store credential
or installed VSIX. Windows, wire-byte measurements, installed VSIX, native
RCC/JAT and live R2/MinIO/S3 acceptance remain unproven. v1.0.0 remains on hold.
