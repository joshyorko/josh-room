# Room Store Phase 0: restic probe

This is a disposable feasibility probe for issue #51. It does not read Josh
Room configuration, update catalogs, or change production code paths. The
default mode creates a synthetic local repository and workspace in a temporary
directory, then removes them.

## Run

On the Linux host with the reviewed binary:

```sh
python3 scripts/room_store_probe.py --restic /home/linuxbrew/.linuxbrew/bin/restic
```

The probe refuses versions other than restic `0.19.1`. It initializes
repository format 2 explicitly, uses a temporary mode-0600 synthetic password
file, and emits one compact JSON receipt. It makes no network calls. The receipt
has a stable schema and sorted keys; snapshot IDs and byte counts are naturally
run-specific. Failures are emitted to stderr as bounded JSON without restic's
path-bearing diagnostics.

Each gate is atomically recorded in `/tmp/josh-room-restic-phase0-evidence.json`
with mode `0600`, including completed summaries when a later gate fails. The
receipt omits filesystem paths, endpoints, bucket names, and provider credentials.

The run exercises initial backup and restore, explicit parent selection from a
new restic process, unchanged-save skipping, one small in-place edit, rename
reuse, cancellation followed by repository checks, and two concurrent backup
writers followed by another repository check. It uses an 8 MiB synthetic file;
the cancellation-only pass uses verbose JSON with 2,048 tiny synthetic files.
Neither pass uses user data. A successful local result says nothing about
remote storage or the packaged runtime.

Cancellation uses SIGINT on POSIX. On Windows, the probe creates a new process
group and sends CTRL_BREAK; if that signal is unavailable or rejected, it calls
the shared bounded process-tree terminator. Evidence clears only the current
Linux or Windows local gate; it leaves the other platform unproved.

## Optional S3-compatible mode

After explicit authorization for a specific MinIO or R2 acceptance run, the
probe can target an existing bucket's dedicated `room-store-phase0` prefix:

```sh
python3 scripts/room_store_probe.py --s3-config-stdin
```

Supply one JSON object through a trusted one-shot stdin source. It contains
`provider` (`minio` or `r2`), `repository` (`s3:https://host/existing-bucket/room-store-phase0`),
`access_key_id`, `secret_access_key`, and optional `session_token` and `region`.
Do not put values in shell arguments, command history, environment declarations,
logs, or receipts. The process passes credentials only through its private
environment to restic. This mode calls no bucket-administration API and runs no
delete, forget, or prune operation; restic only initializes/writes under the
specified prefix. Each run uses a fresh opaque subprefix beneath
`room-store-phase0`; it does not remove objects afterward. Reusing an existing
repository is not attempted.

Do not run this mode until the parent authorizes the target provider, account,
and dedicated prefix. HTTPS is required. The fixed prefix is enforced by the
parser; provider and bucket names never enter evidence.

## Interpretation and required follow-up gates

Local receipts mark both MinIO and R2 unproved. Remote receipts clear only the
provider explicitly selected and only after every probe stage passes. The other
provider, packaged-runtime installation, and non-Linux platforms remain
unproved. The packaged controller needs an exact managed-runtime install/readback.
None is inferred from a local probe.

Do not add provider configuration or real secrets to this script. Keep the
Phase 0 result separate from the broader issue gates: Dimension-scoped password
recovery from issue #50, runtime artifact checksums/platform support, canonical
capture and filesystem safety, descriptor/catalog design, legacy coexistence,
JAT component composition, Save/restore/export flows, maintenance, and the
benchmark matrix remain integration work.

## Recommended later adapter seam

Keep restic subprocess ownership behind one small Python adapter, independent
of `operations.py` orchestration:

```python
class ResticAdapter:
    def version(self) -> str: ...
    def init(self, *, repository_format: int, password_file: Path) -> RepositoryInfo: ...
    def backup(self, *, source: Path, parent_snapshot_id: str | None,
               exclude_file: Path, cancel: CancellationToken) -> BackupResult: ...
    def restore(self, *, snapshot_id: str, target: Path) -> None: ...
    def check(self, *, read_data: bool = False) -> CheckResult: ...
```

Repository location, password-file handoff, and provider environment should be
owned by a Dimension-scoped `RoomStore` boundary. Pass the password by protected
file or the runtime's existing secret handoff, never argv, logs, config, or
receipts. The adapter should return typed summaries and classify exit codes;
it should not publish descriptors/catalog entries or run prune. Normal Save
must use an explicit parent and `--skip-if-unchanged`; transaction ordering and
publication belong to the existing operations layer.

Keep this probe throwaway. Promote only the tested contracts into the
repository-native runtime after provider, packaging, secret-recovery, and
platform gates have owners and evidence.
