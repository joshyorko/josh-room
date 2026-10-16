# Rustic qualification after 0.1.36

This is an isolated qualification of PR #108 for a release after 0.1.36.
Inspected main was `19723c71d64f81bd664cf34491d13014c213773d`; PR #108
was `ce4bca3c4e491fea7247df9572a32966d26f740c`. The original PR branch and
main remain unchanged. Changes are on `qualify/rustic-after-0.1.36-20261016`.
No release version, runtime release pin, release lock or staged 0.1.36 artifact
was changed. All data, identities and catalog callbacks in these tests are
disposable synthetic fixtures. No user bucket, storage or credentials were used.

Restic 0.19.1 remains the default. Set `JOSH_ROOM_STORE_ENGINE=rustic` on
supported Linux x64 to select Rustic 0.11.4 explicitly; unset it or select
`restic` to return to Restic. Windows product selection now rejects Rustic
with `store-engine-platform-unsupported` before runtime installation.
Unknown engines fail closed. There is no automatic retry with another engine
after a failed write. Format 2, repository identities and descriptor schema
remain compatible; the producer token records `rustic-0.11.4`.

## Repairs

- Apply and verify private native permissions to installer directories, probe
  profiles, executables and digest markers. Reject existing public paths.
  POSIX executable mode is 0700 and private files are 0600. Windows uses the
  existing protected owner/SYSTEM ACL contract.
- Translate current main's resolved absolute Restic exclusion literals into
  confined, rooted Rustic globs. Decode only the supported literal escapes,
  reject traversal and outside authorities, preserve Git internals, and honor
  source-only policy. Real native tests cover bracketed directory names.
- Reject confirmed Rustic `forget` and destructive `prune` before spawning with
  `maintenance-unsafe`. Dry-run plans remain available. Confirmation cannot
  establish that remote or external writers have stopped.
- Correct old Linux parent assumptions in native Windows tests. The current
  verified Windows metadata no-op remains `already-saved`; unknown or changed
  metadata takes the forced content path. Same-size restored-mtime changes
  now run through actual Save and staged restore tests.
- Keep Windows product operations on Restic. Native Windows qualification
  reproduced read-only permission loss with bytes preserved: source 0444,
  snapshot mode absent, restored mode 0666. The pinned core omits Windows mode
  capture and makes permission restoration a no-op. The strict read-only
  acceptance assertion remains a FAIL; it is not xfail or suppressed. The
  low-level adapter remains available only for owned qualification probes.
- Add a real production-adapter benchmark and installed managed consumer gates.
  Managed Rustic qualification also age-encrypts synthetic descriptors for
  operational and recovery recipients, reads them back, and performs actual
  portable JAT export and clean-room restore.
- Read the repository directly for Rustic metadata/full/subset integrity checks.
  A 100k-file cold check reproduced native pack-cache rename ENOENT warnings
  despite exit 0. Checks now disable that optional cache and remove its
  conflicting environment setting. Unexpected diagnostics still fail closed.

## Readiness matrix

Results are bound to exact source commits in the receipts below. A fixture
result is not live-provider acceptance. Rustic is not ready for broad release.

| Contract | Linux | Native Windows x64 | Limit |
| --- | --- | --- | --- |
| Installer/cache/private paths | PASS | PASS | Exact official manifest pins |
| Local Save/no-op/restore and restored-mtime edits | PASS | Raw byte cases passed; read-only fidelity FAIL | Windows product admission BLOCKED |
| Owned engine process-tree cancellation | PASS | PASS | Existing detached JAT cancellation failure remains on main |
| Resolved exclusions/Git/source-only | PASS | Path cases PASS; complete Git restore FAIL | Native read-only mode omission reproduced |
| Scoped custom CA | Historical PR fixture only | Rejection before spawn PASS | Windows CA-file support remains unavailable |
| Managed portable JAT and two-recipient age | PASS | Product admission BLOCKED; composition NOT RUN | Earlier raw Git restore failed; actual pinned runtime |
| 100k production-adapter performance | Measured; cold regression | NOT RUN | Application-cold caches, local storage |
| Concurrent mixed-engine destructive maintenance | BLOCKED | BLOCKED | No repository-wide coordinator |
| Live R2 / production MinIO / Windows S3 | NOT RUN | NOT RUN | No user storage or credential use authorized |

## Production benchmark

`scripts/rustic_production_benchmark.py` calls actual `RoomStoreOperations`,
`ResticStore` and `RusticStore`, with synthetic catalog/keyset callbacks and
real encrypted local repositories. It does not use the old experimental shim.
The fixture has 100,000 1-KiB files in 250 directories. Each run changes 1,000
same-size files and restores their mtimes. The initial run clusters changes in
five directories; both later runs distribute changes across all 250 directories.
Cold means empty application caches. OS page caches are not evicted. These
are synthetic metadata-heavy workloads, not provider throughput claims.

The script records first/repeated metadata scans, a direct warm engine scan,
authenticated cold/warm inventory and tree caches, initial/no-op/changed Save,
full-data checks, snapshot inventory, staged restore and comparison of every
restored file byte. The initial clustered run predates the direct warm-scan
metric. Its recorded source file hashes were independently matched to commit
`56eb5dd6c90cc0f3b4bddb90f7664225b6f7bb40` because it began before that
commit was made. The first distributed run started clean at `01abf73`.

The final complete run started from clean `3ab1d56` on the workspace overlay
filesystem. Both engines passed all restored bytes, inventory equality, no-op
snapshot counts, same-size/restored-mtime edits and full-data checks. The measured
adapter, operation and benchmark source bytes match `13d6d7f`; later changes
only block Windows product selection and strengthen qualification diagnostics.
This binding is recorded in `benchmark-final.json`.

| Final clean 100k distributed run | Restic seconds | Rustic seconds |
| --- | ---: | ---: |
| Metadata scan, first / repeat | 1.675 / 2.046 | 2.435 / 1.790 |
| Initial Save | 26.928 | 220.576 |
| Direct warm engine scan | 4.975 | 5.301 |
| Changed Save | 23.515 | 174.099 |
| Cold inventory | 6.390 | 140.408 |
| Warm inventory | 0.649 | 0.418 |
| Staged restore | 24.352 | 11.523 |

The final run is separate from the earlier tmpfs runs below. It confirms the
data-check fix and the production metadata performance blocker. Raw warm engine
scans are comparable; cold metadata still launches a command for each tree.

| Clustered-change run | Restic seconds | Rustic seconds |
| --- | ---: | ---: |
| Initial Save | 25.002 | 199.376 |
| Changed Save | 20.769 | 23.570 |
| Cold inventory | 5.493 | 138.364 |
| Warm inventory | 0.363 | 0.363 |
| Staged restore | 14.617 | 8.549 |

The distributed run measured initial Save at Restic 21.263s / Rustic 181.764s
and changed Save at Restic 19.152s / Rustic 166.581s. This confirms that changing
many subtrees makes the production inventory cost material on subsequent Save.
Cold inventory took Restic 6.004s / Rustic 137.327s; warm inventory took
0.331s / 0.442s. This run then failed at Rustic's full-data check because of
the native cache-write diagnostics. Its Rustic restore was NOT RUN. The
failure is retained in `benchmark-distributed.json`, not converted to a pass.

The check fix was also separately exercised on a real 100k-file diagnostic repository:
metadata/full/subset checks, all restored bytes and Restic's independent full
check passed. `post-fix-integrity-100k.json` binds that narrower verification
to `3ab1d56`. The earlier failed run remains FAIL alongside the final complete
PASS; no failed result is rewritten.

Rustic cold inventory launched 252 commands versus Restic's 2. Warm inventory
launched none for either engine. This per-tree process cost remains a production
performance blocker; the earlier shim speedup does not establish readiness.
Final distributed timings are recorded in the retained JSON receipt.

## Verification and reproduction

- Native source `13d6d7f`: Linux 11 passed / 5 skipped. Windows 1 failed,
  10 passed / 5 skipped in workflow
  [38981680771](https://github.com/joshyorko/josh-room/actions/runs/38981680771).
  The Windows failure is the strict read-only preservation assertion. The
  native product admission guard passed and leaves Restic as the default.
  Linux skips two native Windows cases and three TLS MinIO cases. Windows
  skips the POSIX unreadable-source case, unsupported product Save case and
  three TLS MinIO cases. Ordinary raw Windows byte cases passing does not
  establish Windows recovery fidelity.
- Broad local Python source `01abf73`: 1,993 passed, 12 skipped, 17 failed,
  2 private acceptance cases deselected. All 17 failures reproduced on exact
  main in a separate worktree with the same interpreter/environment. The
  failed node IDs are retained in `evidence/rustic-after-0.1.36/full-suite.json`.
  This is a FAIL, not a green full-suite claim.
- Final broad Python source `3ab1d56`: 1,997 passed, 12 skipped, 16 failed,
  2 private acceptance cases deselected. All 16 failures reproduced on exact
  main under the same final environment. No new failing node was found.
- Node extension suite: 304 passed, 2 skipped. Ruff, module compilation,
  shipped controller byte parity, whitespace checks and Gitleaks 8.30.1 passed.
- The actual local VSIX package test passed with a disposable npm cache after
  the default-cache environment failure. The original failed run is retained.
- Post-cache-fix adapter/runtime tests: 141 passed, including unchanged Restic
  process contracts and the no-cache flag/environment conflict regression.
- Native managed consumers use RCC v18.19.7 and JAT v0.1.14, source
  `fe43065a654d1649ba9ebe5a6f202eac612e4385`, from the unchanged manifest.
  The final code source is `13d6d7f1d8b39cf0af4a8f16a869343e41f9d627`.
- The Windows managed diagnostic found 0 missing files, 0 unexpected files,
  0 byte mismatches and 21 mode mismatches in the synthetic Git restore.
  The final installed Windows consumer confirms the product admission block;
  it records JAT/age composition as NOT RUN, not passed. The Linux installed
  consumer completed Save/Enter, two-recipient encrypted catalogs, Git fidelity
  and actual portable JAT export/clean-room restore at `13d6d7f`.

```sh
uv sync --group dev
uv run python -m josh_room --help
uv run pytest -q tests/test_rustic_store.py tests/test_rustic_runtime.py
PYTHONPATH=src:. uv run python scripts/rustic_production_benchmark.py \
  --restic /path/to/verified/restic --rustic /path/to/verified/rustic \
  --output benchmark.json
# Native real-engine tests require explicit disposable, verified binaries:
uv run pytest -q tests/test_rustic_live.py tests/test_rustic_native.py
# Installed RCC/JAT/age consumer; creates only disposable synthetic fixtures:
JOSH_ROOM_QUALIFY_RUSTIC=1 JOSH_ROOM_QUALIFY_RUSTIC_ONLY=1 \
  node scripts/managed_runtime_acceptance.js
```

`.github/workflows/rustic-qualification.yml` runs only isolated qualification
on this branch with read-only repository permission. It has no publication
or release job. Reports under `docs/evidence/rustic-after-0.1.36/` contain
source digests, commands/counts, failure nodes and native workflow links.

## Remaining blockers

1. Windows read-only mode preservation fails with the official pinned binary.
   `windows-readonly.json` retains the native failure and the failed managed
   Git restore comparison. The pinned `rustic_core` 0.13.0 source archive was
   verified against [Rustic v0.11.4 Cargo.lock](https://github.com/rustic-rs/rustic/blob/v0.11.4/Cargo.lock),
   SHA-256 `ddab48a7f892b4943d9bffe7be44746c443982ae06ffcc3b2be55b31999b1725`.
   Its Windows mapper supplies no mode and its destination permission setter
   does nothing. Previously created Windows snapshots cannot supply permission
   metadata they never recorded. Product Windows selection is blocked.
2. The pinned Windows binary uses OS certificate trust and does not consume
   the per-operation CA file. Rejection is verified; scoped custom-CA parity
   is not implemented. Host trust is never modified and TLS is never bypassed.
3. Cold production inventory and changed-tree costs remain high. Very wide
   individual trees can exceed the bounded 4-MiB capture and fail closed.
   The completed final core benchmark passes correctness, but changed Save
   remains about 7.4 times Restic and cold inventory about 22 times Restic.
4. Concurrent destructive maintenance across engines is unsupported. Rustic
   ignores Restic locks. A local mutex or a lock-list preflight cannot protect
   remote clients. Before external Restic maintenance, stop every Rustic and
   Restic writer on every host and keep them stopped. This operator procedure
   is not distributed coordination and does not qualify concurrent maintenance.
5. Live R2, production MinIO and Windows S3 acceptance are unavailable under
   this task's storage/credential restriction. No current temporary MinIO TLS
   fixture was run: no verified MinIO binary was installed and the official
   checksum endpoint returned HTTP 410. Old PR TLS results are historical.
6. The broad local suite is not green. Its final 16 failures reproduce on main,
   including detached JAT descendant cleanup, PCC/config trust, permission
   fixture assumptions. The initial run also failed on the unavailable default
   npm cache for a VSIX test; the owned-cache retry passed.
   These are retained rather than weakened or hidden.
7. This branch is not promoted. A subsequent release still needs its version
   and exact-source release/packaging gates, plus an explicit platform decision
   for the Windows CA limitation. No merge or publication is authorized.

Original PR and its historical shim/TLS evidence: [PR #108](https://github.com/joshyorko/josh-room/pull/108).
