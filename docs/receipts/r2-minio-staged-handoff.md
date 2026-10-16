# Room 0.1.36 R2 to MinIO repair candidate

The stale configuration registry blocker is fixed in [PR #152](https://github.com/joshyorko/josh-room/pull/152). The actual installed Linux candidate completed a synthetic active R2 session → native MinIO connection and bucket selection → Dimension creation → Room Save and loading → relocated restore → logical JAT inspection. Publication was not performed, as requested.

This qualification uses synthetic authority material and disposable Moto storage. Live Cloudflare OAuth, real MinIO infrastructure and the real user's later missing-room symptom were not reproduced end to end. The result proves the stale-registry repair and the tested native flow; it does not assert credential corruption, recovery of real missing Rooms, or success against a real account.

## Source and change

Branch `fix/r2-minio-session-registry`, qualified head `b69eaea08b365d48fe8a04b2656d3e3b032efcdb`, merged main `19723c71d64f81bd664cf34491d13014c213773d`, tree `68423d859e09bca1ddd9e45d4ef42a9a70a5bac4`. Main and the qualified head have identical trees.

`private_config()` now composes current persisted MinIO connections and Dimensions with the active session's R2/encryption settings. It replaces or removes stale MinIO records, supports legacy/inline/connection-backed shapes, and respects current selection settings. A record changing provider under the same identifier fails clearly. Existing persisted/runtime configuration files must contain JSON objects; an absent persisted file retains runtime-only compatibility.

No session file is rewritten by this composition. Credentials, identity, expiry and R2 authority remain unchanged. Canonical `config.py` and both packaged/controller copies match. Documentation changed in `docs/MINIO-SETUP.md`. Room 0.1.36/template 0.1.6, published JAT 0.1.14, RCC 18.19.7, controller pins and publication guards are unchanged.

## Verification

| Check | Exact source and result |
| --- | --- |
| Regression tests before fix | Seventeen new cases failed for the corresponding stale-registry/malformed-root behavior; the runtime-only compatibility case already passed. |
| Focused auth/config/provider tests | 132 PASS in declared `room-campaign-tests` image. |
| Ruff, whitespace, generated copies | PASS; both mirrors and installed payload independently verified. |
| [PR CI 38952556154](https://github.com/joshyorko/josh-room/actions/runs/38952556154) | Qualified head b69eaea: 1,933 Python PASS /28 skipped; 304 Node PASS /2 skipped; native Ubuntu/Windows managed consumers and Windows private runtime PASS. |
| [RCC qualification 38952556199](https://github.com/joshyorko/josh-room/actions/runs/38952556199) | PASS on native Linux and Windows, plus JAT candidate; promotion SKIPPED. Qualified source b69eaea. |
| [Template validation 38952556155](https://github.com/joshyorko/josh-room/actions/runs/38952556155) | PASS. |
| Final-main CI | [38954825822](https://github.com/joshyorko/josh-room/actions/runs/38954825822) PASS at exact main 19723c7: 1,933 Python PASS /28 skipped; 304 Node PASS /2 skipped; Ubuntu and Windows managed consumers, Windows private runtime and release-candidate PASS. [Template 38954825117](https://github.com/joshyorko/josh-room/actions/runs/38954825117) PASS. |
| Actual installed Linux native flow | PASS on VSIX `3e9a616874e43093e1c3fcb77b6c1006da008c61c37574970323a9b735b96f57`, 509,620 bytes. |
| Independent installed/restore/session checks | PASS: 102 installed files and normalized package.json match; original and restored hashes verified; all four R2 session files/expiry unchanged. |
| Final staged candidate | [38956687056](https://github.com/joshyorko/josh-room/actions/runs/38956687056) PASS at exact main 19723c7; `publish` SKIPPED. Artifact `12071465063`; all six assets and canonical guarded-stage verifier PASS. |

[Commands, red/green cases and retained local failures](r2-minio-local-verification.json), [native receipt](b69eaea-r2-minio-native-receipt.json), [independent verification](b69eaea-r2-minio-root-native-verification.json), [previous-stage product comparison](r2-minio-product-diff.json).

The initial local broad Python run failed because its minimal container lacked subprocess imports, Node/npm, linked-worktree Git visibility and the normal test-user environment. All 34 failed test names are retained in the local receipt. Host Node testing also failed its actual Python-contract test because host Python lacked botocore; project dependencies were not installed on the host. The corrected broad container run exhausted the shared disk and was stopped with exit 137. These attempts are failures, not qualification. Exact-source hosted CI completed the full suites successfully. Final local Node verification in the declared dependency container also passed 304 tests with 2 skipped.

Packaging initially failed with the default npm cache outside writable task paths, then with ENOSPC during the overlapping broad run. It succeeded using the existing task-owned packaging cache after only the owned test container was stopped. Active UI/RCC caches were retained. No assertions or publication protections were weakened.

## Actual native transition and restore

The installed package's real `auth._write_runtime` constructed the active R2 session from synthetic authority material. Its original snapshot contained zero MinIO connections. MinIO credentials were entered through actual native masked controls; no CLI credential seed, fake SecretService probe, source overlay, runtime fallback or cache clearing was used.

The new connection was immediately visible while the old R2 snapshot remained unchanged. Native controls selected fresh bucket `room-r2-minio-native`, created its Dimension, initialized encryption and saved the Room. Reloaded Room and snapshot pickers listed the new entries. Snapshot `3b0aa9ab4fa5423794f9f3c356521ed3` restored to another path with the original unavailable. Project, selected upload and ignore bytes matched; excluded generated content was absent; all four original files remained unchanged. Native logical JAT inspection succeeded.

One fixture network failure is retained: the loopback alias was missing from container NO_PROXY. Default curl returned403 while direct curl returned200. Adding that alias only to the disposable launcher and restarting VS Code resolved it; the created connection was reused. All four R2 session files and expiry stayed unchanged across that restart and the complete flow. The session remained active, and its snapshot still contained zero MinIO connections.

Only packaged `config.py` differs from the previous known-unresolved stage; the other 104 files are identical. Final-stage parity is recorded below and is separate from actual UI execution.

## Exact stage artifacts

[Guarded stage 38956687056](https://github.com/joshyorko/josh-room/actions/runs/38956687056), artifact [`12071465063`](https://github.com/joshyorko/josh-room/actions/runs/38956687056/artifacts/12071465063), `release-stage-files`, contains six assets plus `release-candidate.json`. The original downloaded ZIP is 2,786,611 bytes, SHA256 `84b19613e6fd7e8d153bb59f9789c2d363b212af4e454f83483c960ec89c278f`. It is an Actions review artifact, not a release. GitHub reports expiry 2027-01-14T19:40:49Z.

| Asset | SHA256 | Bytes |
| --- | --- | ---: |
| `SHA256SUMS` | `7c4eb004179642202d4f39f3ffb4b7d6ef6c63e6be653a648d91f3125fe1bfc9` | 286 |
| `ci-readiness.json` | `86303ea62fdfc6e7334f19d5a02cb6d0c5799a5b855b9bedb79c5496bf7e4af8` | 2125 |
| `josh-room-0.1.36.vsix` | `0d7a4ea8577aacd7067321f5b364bb811234a3210977e653d9c97078cda46eaa` | 509620 |
| `josh_room-0.1.0.dev0-py3-none-any.whl` | `8be094c79ee5235382f181f6aac106b12beee356bc601bba1d6e1a60487a7ff8` | 419000 |
| `josh_room-0.1.0.dev0.tar.gz` | `dacefffb8292f99b314a1430262bb005d33debfdf03ae74d96c8dd2e68aa3538` | 1872877 |
| `vsix-candidate.json` | `cef7adf5cd21e739717e47dff3b7234419e4de98ff7b2e66b5b188587bce3ab7` | 347 |

Independent agent and root verification checked the original artifact ZIP against GitHub's digest/size/source/run metadata, all six asset hashes and sizes, exact main/CI/stage receipts, JAT 0.1.14 source and both platform archive pins. Root also ran the canonical `scripts.release_staging verify` from the exact main checkout. Wheel and source-distribution `config.py` bytes match canonical source.

All 105 uncompressed VSIX files are byte-identical to actual UI-tested VSIX `3e9a616874e43093e1c3fcb77b6c1006da008c61c37574970323a9b735b96f57`. Archive SHA256 differs because ZIP metadata differs. This is tested-payload equivalence and stage integrity, not a claim of another GUI run on the staged archive.

[Official stage receipt](main-19723c7-release-candidate.json), [artifact identity](main-19723c7-stage-artifact.json), [all-file parity](main-19723c7-stage-parity.json), [independent root verification](main-19723c7-stage-root-verification.json), [main test-log readback](main-19723c7-tests-readback.json), [publication boundary](main-19723c7-publication-boundary.json).

The previous stage 38946219816/source 4274a59 remains historical evidence of the unresolved candidate. Use the new exact source and artifact set for review.

## Campaign requirements and dependencies

[Room PR #147](https://github.com/joshyorko/josh-room/pull/147) integrated #144 non-overwriting selected-folder initialization, #145 aggregate/incomplete diagnostics and native explicit selection/confirmation/rescan controls, and #146 explicit source-only policy across native capture/preview/receipts/export/restore. Full-repository protections and old-snapshot defaults remain. Packaged Linux worktree/source-only acceptance and native Windows scan/runtime evidence are recorded in the [campaign readiness handoff](release-0.1.36-readiness.md).

[JAT PR #21](https://github.com/joshyorko/josh-all-the-things/pull/21) implemented #19 explicit filtered capture independently of Room, with archive safety validation and restored-content evidence. Published supporting JAT 0.1.14 source `fe43065a654d1649ba9ebe5a6f202eac612e4385` was adopted in [Room PR #150](https://github.com/joshyorko/josh-room/pull/150). [PR #151](https://github.com/joshyorko/josh-room/pull/151) corrected the disposable restic verification fixture; this repair did not change its threshold or runtime pins.

The graph remains #144 + #146 → #145 complete UX; scanner/reporting can run independently. JAT #19 is independent of native Room Save. Filtered export depends on #19 and exact published runtime adoption, both verified. Room issue states remain open for owner disposition; JAT #19 remains closed. This repair removes the reproduced stale-registry blocker; the unrun provider/GUI checks below remain acceptance scope for any future release decision.

## Remaining scope and publication boundary

Live Cloudflare OAuth and real MinIO/R2 providers are NOT RUN. Full Windows GUI Save/restore is NOT RUN; native Windows backend consumers and private-path checks are separate passing evidence. Raw screenshots, renderer text and private fixture material remain local. All source, storage, credentials and configuration used for this acceptance were disposable; original user state and the separate RCC maintenance branch were untouched.

Room 0.1.36 and template 0.1.6 remain unpublished. Existing 0.1.35 artifacts are untouched. Supporting JAT 0.1.14 was already published under the earlier separate approval and was not republished during this repair. No release, tag, deployment or real user extension replacement occurred.

For read-only review, use an isolated checkout of exact source `19723c71d64f81bd664cf34491d13014c213773d` and a fresh output directory:

```bash
gh run view 38954825822 --repo joshyorko/josh-room
gh run view 38956687056 --repo joshyorko/josh-room
gh run download 38956687056 --repo joshyorko/josh-room \
  --name release-stage-files --dir ./review-stage-19723c7
gh api repos/joshyorko/josh-room/actions/runs/38956687056 > stage-run-19723c7.json
python -m scripts.release_staging verify \
  --directory ./review-stage-19723c7 \
  --source-sha 19723c71d64f81bd664cf34491d13014c213773d \
  --release-tag v0.1.36-standalone-vsix --version 0.1.36 \
  --checks ./review-stage-19723c7/ci-readiness.json \
  --runtime vscode-extension/runtime/manifest.json --release-lock release-lock.json \
  --stage-run-json stage-run-19723c7.json --stage-run-id 38956687056 \
  --stage-workflow-id 341135067
sha256sum ./review-stage-19723c7/josh-room-0.1.36.vsix
```

The VSIX digest must be `0d7a4ea8577aacd7067321f5b364bb811234a3210977e653d9c97078cda46eaa`. This task stops at staging. Before any future publication, the owner must decide whether the unrun live-provider/Windows GUI checks are required, reverify the exact source/artifact set and give separate publication authorization. No publication command was executed or queued.

The issue status sections point to this superseding evidence and preserve the dependency graph: #144 and #146 supply #145's complete UX; native Room Save remains independent of JAT #19, while filtered JAT export requires the exact published filtered runtime. Publication remains prohibited for this task and requires a separate owner action. The earlier automatic merge denial was resolved by explicit PR #152 approval followed by one successful retry; it is no longer a blocker.
