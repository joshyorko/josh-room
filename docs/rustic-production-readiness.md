# Rustic production integration readiness

## Qualification after 0.1.36

The isolated `qualify/rustic-after-0.1.36-20261016` branch reapplies PR #108
onto inspected main `19723c71d64f81bd664cf34491d13014c213773d`. PR #108 was
still `ce4bca3c4e491fea7247df9572a32966d26f740c` before this work. Neither
main nor PR #108 is rewritten. This is qualification for a subsequent release;
0.1.36 versions, release locks and staged artifacts are not changed.

Restic 0.19.1 remains the default. Rustic 0.11.4 is explicit opt-in only.
The installer now applies and verifies private native permissions to newly
created runtime directories, version-probe profiles, executables and digest
markers. Existing public directories/files fail closed. On POSIX the executable
is owner-only executable, mode 0700; secret/profile/marker files are 0600.
Windows uses the existing protected owner/SYSTEM ACL contract.

Rustic destructive `forget` and `prune` now fail before spawning with
`maintenance-unsafe`, including when confirmed. Dry-run plans remain available.
Rustic does not honor Restic locks. A local mutex or a lock-list preflight
cannot establish that remote writers stopped. No concurrent destructive
maintenance across engines is supported. To use Restic maintenance, first stop
all Rustic and Restic writers on every host and keep them stopped through the
operation. This operator procedure is not distributed coordination, and is not
a qualification of mixed concurrent maintenance.

Scoped custom CA files remain rejected on Windows before any child starts.
The official pinned Windows executable uses native trust. This branch does not
import certificates into host trust, bypass TLS, rebuild upstream binaries or
claim Windows custom-CA parity.

Reproduction commands:

```sh
uv sync --group dev
uv run python -m josh_room --help
uv run pytest -q tests/test_rustic_store.py tests/test_rustic_runtime.py
# Supply explicitly verified binaries. These touch disposable synthetic data.
PYTHONPATH=src:. uv run python scripts/rustic_production_benchmark.py \
  --restic /path/to/restic --rustic /path/to/rustic --output benchmark.json
```

The benchmark calls production `RoomStoreOperations`, `ResticStore` and
`RusticStore`. It uses synthetic in-memory catalog/keyset callbacks and actual
encrypted local repositories. It records first/repeat metadata scans, cold/warm
authenticated inventory, warm tree blobs, initial/no-op/changed Save, full-data
checks, snapshot inventory and staged restore with complete file-byte comparison.
The default fixture has 100,000 1-KiB files in 250 directories; 1,000 same-size
files change with their mtimes restored. Cold means empty application caches;
OS page caches are not evicted. This is not live-provider performance evidence.

`.github/workflows/rustic-qualification.yml` runs native Linux/Windows tests
and installed managed RCC/JAT/age consumers on the isolated branch. The managed
consumer first preserves the default Restic checks, then separately runs Rustic
Git-fidelity Save/Enter and actual portable JAT export/clean-room restore with
`JOSH_ROOM_QUALIFY_RUSTIC=1`. Results must be read from the exact workflow SHA;
adding the workflow is not evidence that its checks passed. These workflows have
read-only repository permission and no release/publication job.

Fresh results and remaining blockers are recorded below as runs finish.
The old observations in the following section are historical, not rerun proof.

## Original PR evidence, October 6

Draft release-candidate input on `experiment/rustic-production`, based on main
`eaa53f9934854b874ec822709c7d32120109371a` (v0.1.27). This is not release-ready.
No merge, tag, release, infrastructure change, or benchmark-checkout modification
was performed.

## Selection and compatibility

Set `JOSH_ROOM_STORE_ENGINE=rustic` for an explicit Room Store operation.
Unset it or set `restic` to use the existing default/fallback. Unknown values
fail closed. There is no automatic engine retry after a failed write.

`src/josh_room/rustic_store.py:RusticStore` specializes the existing process,
authority-path, cancellation, result-model and maintenance-plan contract.
`src/josh_room/room_store_bridge.py:_restic_store_factory` and
`_verified_restic_executable` select the corresponding verified adapter/runtime.
Repository format 2, repository IDs, catalog/keyset bindings, descriptor schema,
and `workspace.engine=restic` remain the compatibility contract. The legacy
`producer.restic_version` field records `rustic-0.11.4` for Rustic operations;
existing readers accept that version token without a schema migration.

Both official Linux x64 and Windows x64 MSVC archives are pinned by exact URL,
size and SHA-256 in `vscode-extension/runtime/rustic-manifest.json`. The installer
checks bounded tar members, archive hash, executable version and cached binary
hash before atomic promotion. Version probes use an isolated profile/environment.
The template and controller copies are synchronized. Restic remains pinned at
0.19.1 and is unchanged except for small subclass hooks.

## Verified locally

- Focused Python adapter, runtime, bridge, operation, lifecycle, export,
  descriptor, inventory-cache and controller-artifact tests: **265 passed**,
  **one skipped** because managed age is unavailable. The available pinned
  Restic binary also passes the existing real local operation vertical.
- Nine real contracts pass: local lifecycle; exclusions; Restic-to-Rustic-to-Restic
  repository round trip; TLS MinIO lifecycle; actual RoomStoreOperations
  Save/no-op/changed Save/staged restore; unreadable-source rejection; progress
  cancellation followed by data check; untrusted-CA rejection; wrong-credential
  rejection. The MinIO fixture is temporary, loopback-only and uses a dedicated
  synthetic CA, leaf certificate, bucket and credentials.
- Snapshot lookup/list, authenticated inventory, restore, metadata/full/subset
  checks, confirmed explicit forget, and dry-run/confirmed prune are exercised.
- Linux managed install/version/cache reuse and both official archive extractions
  are verified. Windows PE extraction is not Windows execution acceptance.
- Ruff, JSON/package-copy checks, secret scanning and `git diff --check` pass.

Two existing metadata fixtures now wait 10ms before editing so their ctime/mtime
changes cross a filesystem timestamp tick. Production scan behavior is unchanged;
these tests do not claim detection when all metadata is identical.

Inventory reads authenticated tree blobs because Rustic 0.11.4's JSON listing
contains only names and its text listing loses mode/link information. Tree blobs
are checked against their SHA-256 identities and cached privately by repository
and tree. Unchanged subtrees are reused across changed saves; corrupt cache
entries are misses. Unsupported/nonportable names and malformed data fail closed.

Rustic/core/backend warnings are drained without logging their contents and
prevent successful publication, including source failures that upstream reports
with exit 0. Dependency error logs remain fatal; expected OpenDAL 404 warnings
from repository existence probes are suppressed. Credentials stay in the child
environment; the temporary S3 profile contains only non-secret settings and
escaped environment placeholders. Host profiles, hooks and arbitrary authority
variables are not inherited. Windows forces content scanning and reports no
parent, matching the existing conservative Restic policy.

## Independent benchmark evidence

Two full 100k-file Devsy runs support the experimental engine choice. The first
same-host run measured Restic changed Save at 11.429s and Rustic at 7.702s;
inventory and restore correctness passed. Its evidence is
`../josh-room-rustic-benchmark/.experiment/results/run-20261006T003448Z/benchmark.json`.

The shared independent run completed at
`../josh-room-rustic-bench-runner/.benchmark-artifacts/runs/run-20261006T005748Z/benchmark.json`.
It used 100,000 files, 250 directories, repository format 2 and the experimental
shim, with full-data checks, no-op snapshot-count checks and complete restored-tree
comparison passing for both engines. Changed Save was 10.253275s for Restic and
7.594442s for Rustic, 25.93% lower. Rustic restore itself was slower
(12.759551s versus 9.930480s). This confirms the experimental direction, not the
performance of this production adapter's exact metadata inventory.

Evidence SHA-256:
`2b81695fc6d27d91d8b6b4deecf77821cfad4873b83c77f2f0a79813532974c1`.

## Remaining promotion blockers

1. **Windows per-operation custom CA files.** The pinned MSVC executable uses
   Windows native certificate verification, which does not consume
   `SSL_CERT_FILE`. The adapter rejects that combination before spawning a child.
   Equivalent scoped CA support is required; silently ignoring the configured CA
   or modifying the OS trust store is not an acceptable replacement.
2. **Real Windows acceptance.** Execute the pinned installer and contracts on a
   supported Windows x64 host: private ACLs, process groups/cancellation,
   same-size/restored-mtime edits, paths/env, source errors, inventory and restore.
   Simulated forced-scan argv and tar extraction do not close this gate.
3. **Production performance reproduction.** Measure the actual adapter with the
   same 100k fixture, including cold and warm authenticated metadata caches,
   changed Save and restore. Cold inventory launches one command per tree; a
   very wide tree exceeding the bounded capture limit fails closed. These costs
   and bounds must be acceptable before replacing the current engine.
4. **Mixed-engine maintenance acceptance.** Rustic is lock-free; Restic uses
   native repository locks. Sequential cross-engine reads/writes are verified,
   but simultaneous mixed-engine forget/prune/writers are not. A supported
   coordination policy or additional evidence is required. Do not run concurrent
   destructive maintenance across engines during this rollout.

Full CI, Robot/RCC artifact rebuilds, marketplace/VSIX acceptance, release
promotion, private R2 acceptance and real JAT/age composition were intentionally
not run. No release manifests or published artifact pins were changed. Managed
age/JAT are unavailable in this checkout; that vertical remains skipped.
