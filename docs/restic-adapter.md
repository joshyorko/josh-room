# Restic adapter boundary

`josh_room.restic_store.ResticStore` owns subprocess access to one pinned
restic repository. It is an engine adapter, not a Room Store/catalog
implementation. Its only accepted restic version is `0.19.1`, and it only
initializes repository format 2.

## Lifecycle and credentials

Construct a store with a repository locator, cache directory, private password
file, and optional S3 provider environment. Open it as a context manager, then
call `initialize()` before snapshot operations:

```python
from josh_room.restic_store import ResticStore

with ResticStore(
    repository=repository_locator,
    cache_dir=private_cache,
    password_file=private_password_file,
    ca_bundle=dimension_ca_bundle,
    provider_env=provider_environment,
) as store:
    repository_info = store.open_existing()
```

The caller supplies the password file; the adapter never writes or reads its
contents. The file must be a regular, non-symlink file with mode `0600`; the
cache is created privately with mode `0700`. Provider credentials are accepted
only through the small allowlisted S3 environment mapping. The child receives an
allowlisted environment plus repository, password-file, cache, and progress
settings. It does not inherit HOME/XDG auth files, AWS profiles, or arbitrary
prompt/configuration variables. Secrets and repository paths never enter argv,
error text, or progress values.

An optional CA bundle must be a bounded regular file, not a symlink. It is
passed through `RESTIC_CACERT`; TLS verification remains enabled. Arbitrary
`RESTIC_*` variables are never inherited.

Local repository locators are resolved to absolute filesystem paths. S3
locators must use HTTPS and cannot contain URL user information, query
parameters, or fragments; pass access material through `provider_env`.

The context owns child-process lifetime. It does not delete the caller's
repository, cache, or password file. Backup paths, cache, repository, policy
file, and password file must be separate so backup cannot capture its own
authorities.

## Repository initialization

`open_existing()` first checks the pinned executable and reads `cat config`. A
missing repository returns a stable `repository-missing` error and never runs
`init`. `validate_existing()` is its explicit alias for read-only callers.
Both existing-open and initialization expose the validated repository identity
through the read-only `repository_info` property for callers that need to bind
components to that repository.

`initialize()` first checks the pinned executable and reads `cat config`. It
initializes format 2 only when restic reports the repository does not exist,
then reads back and validates the repository ID and format. If another process
wins the initial creation race, the adapter accepts only the repository it can
read back as format 2. A repository with another format fails closed; the
adapter never migrates format, unlocks, or repairs a repository.

## Snapshot operations

`backup(workspace, parent=..., excludes=..., on_progress=..., cancellation=...)`
runs from the workspace root against relative `.`. It adds an explicit parent
only when one is supplied, always enables `--skip-if-unchanged` and JSON, and
returns a typed summary. A no-op is `BackupSummary(snapshot_id=None)`. Progress
callbacks receive only bounded counts and byte totals; restic paths and raw
diagnostics are discarded.

On Windows, the adapter adds `--force` because restic 0.19.1 maps Windows
`ChangeTime` to `ModTime` and lacks a stable inode in its quickcheck. This
forces content reads so a same-size edit with a restored mtime cannot reuse
stale bytes. Restic's `--force` disables parent selection and its parent-based
`--skip-if-unchanged` behavior, so `BackupSummary.force_scan` is true and
`effective_parent_id` is null. Restic can still deduplicate stored data
content-addressably, but it rereads the workspace and may create a new snapshot
even when content is unchanged. Linux retains the explicit parent and no-op
path; a trusted no-event UI may avoid invoking the controller entirely. A
Windows caller may claim a metadata no-op only after its native ChangeTime
signature and saved descriptor bindings are verified. Unknown metadata must
run the forced content scan; this adapter does not implement that preflight.

Only exit status 0 plus one terminal, supported, zero-error summary returns a
backup result. Exit 3 is incomplete; cancellation, unknown exit codes, malformed
or oversized JSON, unknown message types, and missing/multiple summaries fail
closed. Failures carry a stable error code and may carry a validated bounded
orphan snapshot ID. They never carry restic's raw diagnostic text.

`snapshot()` validates one exact snapshot, tree identity, timestamp, and source
paths. Treat `SnapshotInfo.paths` as private workspace metadata.
`snapshots()` returns a bounded tuple of path-free `SnapshotInventoryItem`
records with only full ID, tree ID, time, and parent ID; malformed, duplicate,
short-ID, and oversized inventories fail closed. `entries()`
streams bounded `restic ls --json` node records and checks the header's
snapshot ID, tree ID, and source paths against that metadata. Restic's
virtual-root absolute node paths are returned as relative paths (`/a/b` becomes
`a/b`, `/` becomes `.`); traversal, duplicate normalized paths, malformed
roots, and mismatched message types fail closed. Each row includes entry type,
mode, size, and link target for the caller's filesystem-policy validator. The
adapter does not decide which entry types are safe. Closing the iterator early
cancels the owned command through the shared process cleanup helper.

`restore()` requires a new destination path and delegates staging/promotion
policy to its caller. `check(read_data=False, read_data_subset=None)` runs only
`restic check`; the legacy full-read boolean remains supported, or callers may
request a positive `n/d` subset fraction (with `n <= d`), never both.

Maintenance is always explicit. `plan_forget(ids)` first validates IDs against
the current inventory and runs restic `forget --dry-run`. `forget(ids, plan=...,
confirmed=True)` revalidates the repository, inventory IDs, and tree bindings
before removing only those snapshot references; it never runs prune. `prune()`
defaults to `--dry-run`; a destructive prune requires `dry_run=False` and
`confirmed=True`. The adapter adds no parallel lock/retry system and exposes no
unlock or repair operation. Restic's native locking errors are sanitized and
returned without retry.

## Not proven by this adapter

The adapter tests use synthetic process results. They do not prove the actual
restic executable, MinIO/R2 provider behavior, managed controller packaging,
cross-platform runtime behavior, Dimension key recovery, catalog publication,
capture-policy safety, or portable JAT composition. Those remain separate Phase
0 or integration gates. No catalog or operation flow should call this adapter
until the parent admits those changes.
