# ADR 0012: Native Room observation and Hauler artifact serving

- Status: implemented in draft PR #205; no merge or release acceptance
- Date: 2026-11-20
- Scope: #202, native workspace previews and existing JAT serving discoverability

## Decision

Keep three responsibilities distinct:

1. **Live workspace observation**: `RoomWorkspaceProvider` uses Room identity,
   capture policy, `workspace_onboarding._directory`, and mount/alias primitives
   from `directory_inventory` and `composition`. It adds bounded regular-file
   previews because `DirectoryInventory` enumerates directories, reads no payload
   bytes and is not a content API. It does not start a second file server.
2. **Saved recovery points**: `SavedCapsuleManifestProvider` validates canonical
   `LogicalJat` descriptors and displays recorded anchors. It does not verify
   archive bytes, restore snapshots, or expose nested capsule contents. Existing
   `snapshot serve` remains the native JAT/Hauler artifact-serving path.
3. **Viewer presentation**: `view/server.py` retains its strictly `127.0.0.1`,
   GET/HEAD-only transport for the bundled viewer, sanitized `/state.json`,
   `/export-preview.json`, `/export.html`, `/content/<id>/<slot>` and allowlisted
   `/attachments/<id>/<name>`. No arbitrary filesystem path handler is added.

Live workspace reads are descriptor-relative and no-follow, stop at mount and
reserved ownership boundaries, omit alias collisions, enforce policy before
reads, and withhold incomplete sibling sets. Secret-named files are not opened;
sensitive text/JSON is redacted per file. PDF/image attachments are binary
artifacts, not sanitized text, and verify bytes and current policy on retrieval.
Native providers default to `internal`, requiring explicit export acknowledgment.
A live observation is not Save authority or an atomic snapshot. Traversal caps
are explicit omissions; resumable paging is not implemented.

## Real Hauler 2.1.1 experiment

`python scripts/verify_hauler_fileserver_spike.py --mode live` uses the pinned
binary only inside a separate Linux user/network namespace with loopback up.
The parent refuses unavailable isolation/version mismatch; the worker rejects
the host network namespace. All source files and projections are synthetic;
process groups and temporary stores are cleaned on exit or timeout.

The machine receipt is
[hauler-fileserver-spike-receipt.json](../receipts/issue-202/hauler-fileserver-spike-receipt.json).
It records the executable SHA-256 and the following observed results:

| Experiment | Actual result |
| --- | --- |
| Archive-shaped JAT workspace artifact | Archive bytes served; nested `/src/app.py` returns 404. JAT capture itself was not run. |
| Individually stored files | Exact file bytes and directory listings work. `--name nested/app.py` preserves a nested path. |
| Source edit after projection | Served bytes remain the original one-shot projection. It does not mirror live edits. |
| Two artifacts with the same title | Second file replaces the first; independent paths are not preserved. |
| Foreign Host header | Accepted (200). No Room-style DNS-rebinding guard. |
| Symlink pre-existing in projection | Target outside projection is served. |
| Encoded parent traversal | Outside sentinel was not served in the tested request. This is not exhaustive path-safety qualification. |
| Listener binding | Wildcard listener, confined to the isolated namespace. |

The fixture mode is a Python loopback simulation only; it is not evidence of
Hauler behavior. The live test establishes native file serving, not safe live
workspace observation. Hauler owns artifact bytes; Room owns workspace scope,
policy and sanitized previews. The experiment used isolation to contain its
synthetic exposure tests; that test setup is not a product requirement for
existing Hauler artifact serving.

## Artifact listener-scope correction

The localhost-only rule applies to `josh-room view`, not to every native Hauler
artifact listener. Unpatched Hauler 2.1.1 supports artifact files, registry,
both and automatic mode selection with JAT-chosen nonzero ports. Room accepts
truthful fileserver `all-interfaces` metadata while keeping connection URLs
strictly `http://127.0.0.1:<port>` and registry binding strictly `loopback`.
Startup notifications, status and logs name the fileserver listener scope;
a localhost connection URL is not described as private or localhost-only.
Optional fileserver `loopback` metadata remains valid. Unknown binding values,
wildcard registry binding and non-loopback connection URLs remain rejected.

Hauler's optional upstream bind-host feature (#963) is independent, not a
blanket dependency for #202 or supported artifact serving. No container/proxy
subsystem or new approval flow is added. Matching JAT startup-receipt artifacts
and installed-runtime qualification remain real gates; no released-runtime
support or pin promotion is claimed here.

[Correction evidence](../receipts/issue-202/artifact-scope-correction.json) records
real unpatched 2.1.1 Files/Registry/Both/Auto sessions consumed by Room, with
occupied defaults, concurrency and shutdown cleanup. This source integration
is distinct from published managed-runtime and installed-VSIX acceptance.

## Extension discovery

`Josh: Serve Room Content…` replaces the misleading Images label in Room menus,
command palette and quick pick. `Josh: Serve this Logical JAT…` reuses the same
handler on logical recovery points, preserving selected Room, Dimension,
snapshot and native Workspace Trust/encryption gates. Logical snapshots already
support Files / Registry / Both / Auto without portable export. Legacy snapshots
keep their existing registry path. The files notification names the endpoint
and provides Copy Fileserver URL. Root/template sources remain identical.

## Remaining gates

- Installed extension and released pinned JAT runtime acceptance.
- Actual JAT capture → portable export → serve vertical, including export failure.
- Windows/native-platform artifact-serving and listener-scope qualification.
- Resumable workspace pagination and bounded extraction of nested saved contents.
- Human five-second recognition/lookup studies and physical GPU timing.
