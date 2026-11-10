# Portable Room Store execution trail

Base `f1e77164a41b545060b574dfb07e41043e79fbe2`, fresh `origin/main`
checkout. No prior implementation branch or thread was used.

| Decision | Evidence | Result |
| --- | --- | --- |
| Keep one actor implementation | `RoomStore` imported by both deployment adapters | Shared business logic, not parallel ports |
| Preserve production R2 identity | Unchanged Wrangler binding, class, migration and named-object function | No destructive migration |
| Reproduce generation rejection bug first | Added test fails on baseline; focused commit `8eebb56` passes 11 client tests | Explicit rejection no longer becomes success through readback |
| Qualify the runtime independently | Pinned celld v0.6.3 binary digest matches release; exact source tag resolved | Runtime version bound to source |
| Use a real MinIO binary | Public image pulls refused; archived binary URL returned 410; pinned source built successfully | No substitute mock storage |
| Test durable fleet behavior | Two celld nodes, same contract as workerd; kill both, remove caches, cold restore | Disposable MinIO recovery verified |
| Preserve existing backup data during custody change | Actual age, MinIO conditional publication, Restic incremental backup/restore/check, unchanged catalog bytes | Disposable migration and permanent-seal rollback verified |
| Fix pre-existing local RCC fixture drift separately | Six failures report stale RCC 18.19.9; main requires 18.19.10; fixture now reads source pin | Focused commit `ada261b`, six cases pass |
| Retain legacy writer only for compatibility window | Existing consumers must not acquire a daemon dependency without migration | Explicit deletion gate, no outage fallback |
| Reject permanent multiple authority designs | Marker fences old clients; rollback seals DO first | One active writer per selected Dimension |

## Verification receipts

Commands below ran in the new Linux checkout. Disposable runtime tests bind
only loopback interfaces and use generated lab credentials. No production
provider state was used.

- PASS baseline `node --test worker/test/index.test.js`, 12 tests.
- PASS baseline focused Python client suite, 10 tests.
- FAIL new generation regression on baseline, then PASS after focused repair.
- PASS `node --test worker/test/*.test.js vscode-extension/*.test.js scripts/verify_node_version.test.js`, 370 passed, 2 skipped.
- PASS `node --experimental-strip-types --test omp-extension/tests/*.test.mjs`, 49 passed.
- PASS `ROOM_STORE_TEST_TOOLS=... node scripts/portable-room-store/workerd.mjs`, actual workerd contract plus corrupt-record rejection.
- PASS `python scripts/portable-room-store/fleet.py --celld ... --minio ... --esbuild ...`, same contract on two nodes, MinIO cold restore, real age/Restic migration and rollback.
- PASS focused material/migration tests, 27 tests.
- FAIL intermediate full suite, 2052 passed, 10 skipped, six stale-RCC-fixture failures. Those six cases pass after the separate correction.
- PASS candidate VSIX packaging, 106 entries. Four canonical authority modules match exact packaged bytes.

Live Cloudflare, installed VSIX interaction, native Windows, production TLS
and OS keyring enrollment are SKIPPED. Local fleet migration deliberately
injects a loopback HTTP transport and disables local keyring cache writes.
Default fleet-mode partitions and production MinIO remain NOT QUALIFIED.
No merge, release, production deployment or real-data migration occurred.

- PASS final complete `pytest -q --tb=short`, 2058 passed, 10 skipped.
- PASS `ruff check src tests scripts/portable-room-store/*.py` and `git diff --check`.
- PASS `npm audit --prefix worker`, zero findings with patched Undici/Sharp overrides.
- PASS detect-secrets 1.5.0 scan of changed authority source, adapters and operational docs, zero findings.

The source principle **Model the Domain** chose one transactional actor rather
than duplicated reducers and storage orchestration. **Prove It Works** required
actual workerd, storage-backed celld recovery, real age/Restic and exact VSIX
bytes before reporting those lanes as verified.

- PASS independent read-only final review of migration fencing, custody and R2 compatibility; no actionable blockers reported.
- PASS candidate VSIX SHA-256 `46043513196f1929e01308933980948b0447e4c47c256a4e39c0daf83acb9525`, 519691 bytes, packaged before commit from the identical canonical module bytes.

Scratch fleet processes and runtime data are removed by the qualification
script. Task-local source clones, binaries, package output and test logs are
removed after the draft PR is created. Tool-managed Go/npm/uv caches may persist.

## Amazon S3 follow-up on the rebased branch

Starting PR head `8cfaf0c408da3b42632fc0c0191580feafcdb80f` contains main
`2dd0bdcf1bc16906818e7eef6513dad141fc1962` and the integrated R2 repairs.
The follow-up uses an isolated checkout at that exact head, not old local
history. A normal fast-forward-only push is conditional on the remote still
pointing to that expected starting head.

The extension admits S3 authority options and the real provider in domain
hashes. First Save can initialize DO custody only for an opted-in format-1 S3
enrollment. Existing format-2 S3 and unmarked MinIO stores do not migrate.
No actor transition, S3 SDK/Restic addressing code, R2 OAuth/transport code or
VS Code success/progress behavior changes. No migration command gains S3 support.

- PASS baseline scoped suite at the rebased head, 76 tests.
- FAIL new S3 regressions before implementation, rejected authority config options.
- PASS scoped portable/material/S3/backend/schema/R2 client suite, 97 tests.
- PASS local synthetic S3 flow with actual age encryption, the exact shared
  actor imported from `worker/src/room-store.js`, cold readback in fresh Node
  processes, binding, stale-generation rejection, wrong bucket rejection and
  no plaintext password/private identity in actor state. This fixture uses a
  file-backed storage double; it is not a new celld/workerd qualification.
- PASS botocore Stubber verification of conditional S3 control publication,
  ETag-bound head/get readback and inherited backend behavior; no AWS requests.
- PASS Worker/VS Code tests, 382 passed and 2 skipped. PASS OMP tests, 49 passed.
- PASS Ruff, JSON/schema parsing, changelog validation, source/controller/template
  parity and `git diff --check`.
- Secret scan reports one unchanged synthetic credential-rejection fixture at
  `tests/test_s3_schema.py:24`; no newly added secret findings.

Live AWS, new celld fleet qualification, service/Worker deployment, production
migration, bucket/credential administration and installed-VSIX acceptance are
NOT RUN for this follow-up. The previously installed test VSIX and its user's
live container/services were not touched. Earlier cancelled large-project
capture is not promoted to acceptance evidence. No subagents or PR watchers
were started.

Final complete follow-up verification: PASS `pytest -q --tb=short`,
2158 passed and 10 skipped in the isolated rebased checkout. Those skips are
not counted as live-provider or installed-platform passes.
