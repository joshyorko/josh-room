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
staging and leave no promoted partial capsule. Disk preflight reserves eight times the
descriptor's logical workspace and component archive bytes, plus 512 MiB, for materialization
and output work; this is an estimate, since the final Hauler capsule can be
larger than its inputs.

Export and Serve failures retain the `portable-export-failed` error code and
include the path-free export reason in `diagnostic` in the controller JSON result.

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

## Native serving endpoints

`josh-room snapshot serve <room>`, legacy `josh-room serve <room>`, and
`josh-room jat serve --haul <file>` forward `--fileserver-port`, `--registry-port`
and `--startup-file` to JAT. Port `0` (the default) requests automatic allocation;
explicit nonzero ports must be honored by JAT or fail. Room never probes a free
port and then asks JAT to bind it.

The extension uses a unique private directory per Serve invocation. JAT publishes
an atomic new-file-only startup receipt after its owned listeners are ready:
`{schema_version: 1, state: "ready", operation: "serve", pid, serve: {mode,
fileserver_url, registry_url, fileserver_bind, registry_bind}}`. Mode is resolved
(`files`, `registry`, `both`); inactive URLs are null and active bind values are
`loopback`. Room requires explicit `http://127.0.0.1:<port>` URLs, validates the
mode and endpoints, and reads the registry catalog only from that session's URL.
Status, copy actions and image references use those URLs, never 8080/5000 guesses.
The startup file is removed by JAT on shutdown. Room watches the per-session
controller result, RCC receipt, terminal lifecycle and an interruption-safe
command completion marker during startup, surfacing
export/unsupported-runtime failure immediately rather than waiting on a default
port. There is no browser action service or Room-owned port allocator.

This is a source-level integration with the JAT dynamic-serving branch. Current
released pins lack startup reporting; source preflight fails before Room
materialization when that support is absent. Pinned Hauler 2.1.1 files serving
also lacks loopback binding. A qualified JAT and Hauler update, paired managed
artifacts, and installed-extension acceptance are required before promoting
pins. Fixtures or tests against a locally patched Hauler are not evidence that
the released runtime supports this feature. No pin, release or live service is
changed here. The original `portable-export-failed` incident remains separate:
a port collision has not been established as its cause.
