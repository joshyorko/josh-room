# Historical file recovery (draft)

`josh-room recovery tree|inspect|diff|extract ROOM --dimension DIMENSION --snapshot EXACT_ID --path RELATIVE --json`
uses the existing authenticated catalog/descriptor context, including ciphertext
verification and Dimension credentials. `latest` is rejected. The selected
Logical JAT must match its catalog, Dimension, encryption domain and repository.
The Restic snapshot tree must match the descriptor. There are no catalog writes,
Room hydration, migrations or marker updates.

`tree` supports `--offset N --limit N` (1–500 entries per page) within the selected
directory; the returned `next_offset` resumes that same immutable directory.
Directories are read on demand via content-addressed tree blobs. There is no
global workspace item ceiling, but each tree/blob must fit the existing bounded
Restic capture (4 MiB), with a 128 MiB aggregate read budget per request. A larger directory fails closed rather than appearing
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

## Comparison and native navigation

`diff --compare-snapshot EXACT_OTHER_ID` compares the same path within the
same authenticated Room/Dimension. File comparisons use verified SHA-256;
directory comparisons use immutable Restic subtree identities (no recursive
text diff). Added/removed/modified/unchanged are relative to the historical
source. `diff --workspace EXPLICIT_ROOT` compares a file to an explicitly
selected current workspace on Linux using no-follow directory handles and the
current capture exclusions. Windows current-file comparison fails closed;
A↔B comparison is platform-independent source logic but not Windows-qualified.
No content is displayed, and text diffs remain `withheld-policy`.

The native Logical JAT row offers **Browse Recovery Point…**. QuickPick keeps
its initial Room/Dimension/point while paging, navigating, inspecting checksums,
recovering to a Save Dialog destination, or comparing digests. Canceling a
picker makes no extraction call. Untrusted workspaces can browse metadata but
cannot recover or compare. Results open as JSON metadata, never executable
historical documents. This is not yet the requested syntax preview/diff editor.

Final validation: 135 focused Python tests passed with real Restic 0.19.1.
The same synthetic six-byte recovery measured 2,412 blob bytes / 3 calls on the
final run (tree metadata varies with synthetic file timestamps). The earlier
2,552-byte observation is retained above; neither includes wire overhead or
authentication. Security/controller suites: 115 passed; the real symlink case
initially failed on host Restic 0.20.0, then passed with pinned 0.19.1.
Node suite: 375 passed / 4 skipped / 1 failed due to missing system-Python
botocore; the single failed case passed with the existing project venv on PATH.
The four new native-navigation cases passed. The VSIX packaged and installed
successfully into disposable isolated extension/user directories; installed
controller help and source byte checks (package JSON ignores VS Code-added
installation metadata) are separate from UI acceptance.
No installed interactive QuickPick/diff or live provider acceptance is claimed.

## Installed native helper acceptance, 2026-11-22

At source `a112b891bcc429e688b870498decc23c067e3347`, the built VSIX was
installed in disposable user/extension directories under VS Code Insiders
1.148.0 (`0d15d4d60248e35fac1a3fe234d74ed780e15691`, Linux x64). A private
Xvfb display with TCP disabled hosted the real native QuickPick and Save
Dialog. An acceptance-driver extension loaded the byte-matched **installed**
`recovery-actions.js`, passed real VS Code APIs, and dispatched its requests
through the installed CLI parser/recovery implementation against the real
pinned encrypted Restic fixture. Catalog authority was injected synthetic
fixture context, not a live provider/keyring or the registered row command.

Observed and inspected screens: pinned `jat-ui-old` directory listing,
`changed.txt` actions, current-file digest comparison (`modified`), native
absent-destination Save Dialog, and recovered JSON receipt. The recovered
file was exactly six bytes with SHA-256
`b640e840b19d378660b32fb51ae18d67dccb4a8596a29e7bd72c1b2ae5928f41`.
Its extraction reported one file, 1,212 plaintext blob bytes and two blob
calls. All three current-workspace file hashes (including the 2 MiB unrelated
binary) matched the pre-recovery inventory. Zero staging directories remained.
Canceling the subsequent picker ended the session without another write.
These are native **installed helper** results, not production row-command,
managed RCC/controller, provider ciphertext-authority or keyring acceptance.
Text diffs are still withheld; the comparison surface is JSON metadata.

The initial disposable D-Bus probe found no `org.freedesktop.secrets` service.
A bounded task-owned container setup is being evaluated independently; the
user's real keyring and credentials are never used for this acceptance.
