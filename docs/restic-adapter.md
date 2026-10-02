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
    provider_env=provider_environment,
) as store:
    repository_info = store.initialize()
```

The caller supplies the password file; the adapter never writes or reads its
contents. The file must be a regular, non-symlink file with mode `0600`; the
cache is created privately with mode `0700`. Provider credentials are accepted
only through the small allowlisted S3 environment mapping. The child receives an
allowlisted environment plus repository, password-file, cache, and progress
settings. It does not inherit HOME/XDG auth files, AWS profiles, or arbitrary
prompt/configuration variables. Secrets and repository paths never enter argv,
error text, or progress values.

Local repository locators are resolved to absolute filesystem paths. S3
locators must use HTTPS and cannot contain URL user information, query
parameters, or fragments; pass access material through `provider_env`.

The context owns child-process lifetime. It does not delete the caller's
repository, cache, or password file. Backup paths, cache, repository, policy
file, and password file must be separate so backup cannot capture its own
authorities.

## Repository initialization

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

Only exit status 0 plus one terminal, supported, zero-error summary returns a
backup result. Exit 3 is incomplete; cancellation, unknown exit codes, malformed
or oversized JSON, unknown message types, and missing/multiple summaries fail
closed. Failures carry a stable error code and may carry a validated bounded
orphan snapshot ID. They never carry restic's raw diagnostic text.

`snapshot()` validates one exact snapshot, tree identity, timestamp, and source
paths. Treat `SnapshotInfo.paths` as private workspace metadata. `entries()`
streams bounded `restic ls --json` node records and checks the header's
snapshot ID, tree ID, and source paths against that metadata. Restic's
virtual-root absolute node paths are returned as relative paths (`/a/b` becomes
`a/b`, `/` becomes `.`); traversal, duplicate normalized paths, malformed
roots, and mismatched message types fail closed. Each row includes entry type,
mode, size, and link target for the caller's filesystem-policy validator. The
adapter does not decide which entry types are safe. Closing the iterator early
cancels the owned command through the shared process cleanup helper.

`restore()` requires a new destination path and delegates staging/promotion
policy to its caller. `check(read_data=False)` runs only `restic check` and may
optionally request `--read-data`; it never runs forget, prune, unlock, or repair.

## Not proven by this adapter

The adapter tests use synthetic process results. They do not prove the actual
restic executable, MinIO/R2 provider behavior, managed controller packaging,
cross-platform runtime behavior, Dimension key recovery, catalog publication,
capture-policy safety, or portable JAT composition. Those remain separate Phase
0 or integration gates. No catalog or operation flow should call this adapter
until the parent admits those changes.
