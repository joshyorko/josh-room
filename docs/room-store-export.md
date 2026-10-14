# Portable export from the Room Store

`export_portable_jat` materializes one already-selected v3 logical JAT as a
self-contained Hauler/JAT capsule. Callers must first open and verify the
Room Store and descriptor through the bridge. The helper accepts that
`LogicalJat`, the opened `ResticStore`, a private operation staging directory,
the selected output path, and the managed JAT root. It does not open a provider,
read a catalog, or perform repository setup.

The export pipeline restores the descriptor's exact workspace snapshot and
every referenced component snapshot into owned temporary directories. It checks
the recorded snapshot tree IDs, validates snapshot paths, and verifies component
archive sizes and SHA-256 digests. RCC metadata is checked against the descriptor
and translated into the saved-metadata format expected by JAT. Hauler metadata
is checked against the archive digest, declared version, and reference
identities. The verified
archives are passed directly to JAT Build as `rcc_archive`, `rcc_metadata`,
`brew_archive`, and `hauler_archive` inputs.

Before publishing, the helper asks JAT to inspect the composed capsule and
restore it into a clean, absent destination. It compares the restored workspace
with the materialized source by relative paths, file contents, modes, and
symlink targets, applying the shared canonical capture exclusions and the
workspace's recorded ignore rules. It also checks RCC identity from the clean-room
restore receipt, saved Hauler reference digests from inspection, and expected
component anchors. JAT's clean-room restore validates saved Homebrew recovery
content. This proves content equivalence for the selected logical JAT; it does
not claim byte identity with a historical monolithic archive.

The output is create-only. JAT builds into an unpublished temporary file, and
Josh Room promotes it with an atomic no-replace link only after inspection,
restore, and identity checks succeed. Cancellation and every failure remove
staging and leave no promoted partial capsule. Disk preflight reserves twice the
descriptor's logical workspace and component archive bytes for materialization
and output work; this is an estimate, since the final Hauler capsule can be
larger than its inputs.

The helper returns a bounded `PortableExportResult`: logical JAT ID, status,
output size and SHA-256, workspace entry count, verified component names, and
preflight byte counts. It does not return filesystem paths. Metadata inspection
should read the descriptor directly and must not invoke this export pipeline.

`materialize_components` exposes the same component snapshot, archive, and
metadata verification for normal Enter. It returns an owned context-managed
lease with optional RCC, Homebrew, and Hauler paths plus a path-free identity
manifest. Callers keep the lease open while applying or consuming those
components; context exit removes its private staging directory. RCC acquisition
remains the managed JAT/RCC runner's responsibility.

Tests inject the native JAT Build, Inspect, and Restore boundaries. The full
managed JAT/Hauler clean-room vertical remains an acceptance check for the
calling application and is not established by those tests.

Git metadata is included in materialization and in the clean-room byte/mode
comparison. Export refuses nonportable Git storage and credential-bearing
Git remote URLs, HTTP or credential configuration; it never strips metadata
to make export succeed. Normal Room Store snapshots remain encrypted and may
contain that configuration. Portable JAT files are plaintext capsules, created
with private permissions, and include local history, hooks and repository
contents. Credential checks are not a general secret scanner: secrets committed
in history or arbitrary files remain sensitive. Keep the capsule private and
encrypt it before sharing or moving it to untrusted storage. Export does not
execute saved hooks or fetch objects from remotes.

Local Git clones can share object files through hardlinks. Restic preserves
those links. Because JAT rejects tar hardlink entries, portable composition
copies linked regular files only inside its owned private staging tree. Every
path's content and mode is preserved; the source and snapshot remain unchanged.

Historical Room Store snapshots that excluded `.git` cannot regain that data
through portable export; the capsule faithfully represents their incomplete
recorded contents. Create a new recovery point from an intact workspace before
relying on Git recovery.
