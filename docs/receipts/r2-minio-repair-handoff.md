Current integration update: specific merge approval was received and PR #152 merged at `19723c71d64f81bd664cf34491d13014c213773d`. [Full-tree identity](main-19723c7-tree-receipt.json) matches the qualified/native-tested head. [Exact-main CI38954825822](https://github.com/joshyorko/josh-room/actions/runs/38954825822) and fresh guarded staging are qualifying. Publication remains prohibited. The blocked-merge checkpoint below is historical; its denial is resolved.

# R2 to MinIO repair: qualified feature review stage

The stale registry repair is implemented and qualified in [Room PR #152](https://github.com/joshyorko/josh-room/pull/152), branch `fix/r2-minio-session-registry`, exact head `b69eaea08b365d48fe8a04b2656d3e3b032efcdb`, tree `68423d859e09bca1ddd9e45d4ef42a9a70a5bac4`. A feature review stage is built and verified. The guarded exact-main release stage is **pending specific merge approval**. PR #152 remains open and unmerged; main remains `4274a59fd4a15e97bf51dd2f74aca7ef41bac86e`. No release or template publication occurred.

Automatic approval review denied merging PR #152 because it interpreted prior merge approval as scoped to PRs #21 and #147. The merge was verified unexecuted and was not retried or delegated. Explicit approval for this merge has been requested. This technical implementation is complete; integrating it to obtain the guarded main stage is blocked on that authorization.

## Repair and evidence

An active R2 OAuth session retained an entire configuration snapshot. MinIO connection and Dimension changes went to persisted configuration, so later commands could not see them. `private_config()` now composes the current persisted MinIO registry with session-issued R2/encryption settings. Updates, disconnects, removals, legacy/inline records and current selection settings take effect. Provider changes under one ID and malformed JSON roots fail clearly. An absent persisted file retains runtime-only compatibility.

The read does not rewrite session credentials, identity, configuration or expiry. Canonical `config.py` and both packaged copies match. `docs/MINIO-SETUP.md` documents the behavior. Room 0.1.36/template 0.1.6 versions, published JAT 0.1.14, RCC 18.19.7, controller pins and publication guards are unchanged.

- PASS: [full CI 38952556154](https://github.com/joshyorko/josh-room/actions/runs/38952556154), 1,933 Python tests /28 skipped; 304 Node tests /2 skipped; native Linux/Windows managed consumers and Windows private runtime.
- PASS: [RCC qualification 38952556199](https://github.com/joshyorko/josh-room/actions/runs/38952556199), both platforms and JAT candidate; promotion skipped. Template validation 38952556155 passed.
- PASS: 132 focused auth/config/provider tests, Ruff, whitespace and mirror verification. Seventeen regression cases failed before the corresponding fix; runtime-only compatibility already passed. Independent review's malformed-null finding was fixed and checked with six cases.
- PASS: actual installed Linux VSIX `3e9a616874e43093e1c3fcb77b6c1006da008c61c37574970323a9b735b96f57`, 509,620 bytes, completed the native transition, Room Save/loading, relocated restore and logical JAT inspection.
- PASS: orchestrator independently checked installed files, original/restored hashes, unchanged R2 session files/expiry, all staged assets and hosted-candidate payload equality.

[Native receipt](b69eaea-r2-minio-native-receipt.json), [independent native verification](b69eaea-r2-minio-root-native-verification.json), [commands and retained local failures](r2-minio-local-verification.json), [product comparison](r2-minio-product-diff.json).

The installed package's real `auth._write_runtime` constructed an active R2 session from synthetic authority material. MinIO credentials were entered through native masked controls. New connection and Dimension records remained absent from that old session snapshot throughout the flow, while native controls could list their bucket, save the Room and reload its Room/snapshot entries. Snapshot `3b0aa9ab4fa5423794f9f3c356521ed3` restored at another path with the original unavailable. Selected project/upload/ignore hashes matched; excluded generated content was absent; all four original files and all four R2 session files stayed unchanged. Native logical JAT inspection passed.

A fixture proxy failure is retained: its loopback alias lacked NO_PROXY, causing default curl 403/direct curl 200. Only the disposable launcher was corrected; the existing connection was reused. No product workaround, source overlay, fake probe, CLI credential seeding, runtime fallback or cache clearing was used. Initial broad local tests also exposed missing test-environment tools/imports and later exhausted the shared disk. The local receipt retains all 34 initial failed test names, interrupted run and packaging failures; hosted exact-source CI completed the full suites successfully. Final local Node testing with declared Python dependencies passed 304, with 2 skipped.

## Review artifacts

Local review ZIP `r2-minio-b69eaea-feature-review-stage.zip`: 2,786,112 bytes, SHA256 `4ae7500d9da005da3622ac34d2864599bfecefedc990bd7fb7fa31e5f286824b`. It contains the six assets below plus `review-stage.json`. This is explicitly a **feature review stage, ineligible for publication**, not a trusted main release stage. The metadata cannot substitute for the guarded main workflow.

| Asset | SHA256 | Bytes |
| --- | --- | ---: |
| `josh-room-0.1.36.vsix` | `3e9a616874e43093e1c3fcb77b6c1006da008c61c37574970323a9b735b96f57` | 509620 |
| `josh_room-0.1.0.dev0-py3-none-any.whl` | `8be094c79ee5235382f181f6aac106b12beee356bc601bba1d6e1a60487a7ff8` | 419000 |
| `josh_room-0.1.0.dev0.tar.gz` | `dacefffb8292f99b314a1430262bb005d33debfdf03ae74d96c8dd2e68aa3538` | 1872877 |
| `qualification.json` | `acfec08bfbe8a7f270b999550d014c4d7aed8d1fd5552d52c901704e6efca444` | 1863 |
| `vsix-candidate.json` | `06c4ccd1e5bc48659f9237e3eda4e89ccccd700efcc565d9dd0c82d51a02697f` | 347 |
| `SHA256SUMS` | `6174ec6af14061d0e3ea6e796a03f068780fa2d8b2afd144f77c75725436ab20` | 457 |

[Stage receipt](r2-minio-feature-review-stage.json), [qualification](r2-minio-feature-qualification.json), [ZIP identity](r2-minio-review-stage-identity.json), [independent stage verification](r2-minio-feature-stage-root-verification.json). Python distributions were built with declared build tooling in a disposable container using isolated hatchling 1.32.4 environments; their `config.py` bytes match canonical source. No host project dependencies were installed.

Hosted review artifact `12068569853`, `managed-runtime-evidence-ubuntu-latest`, belongs to CI 38952556154. Its ZIP SHA256 is `76975a3132f1ed7728c249639e7e18bad04d0701b6c0e8ae66e30bf4d42e5627`; contained VSIX SHA256 is `7c10b580c121d7d256418680eb9ea8d05d1e09a016672f9f1841d4ab1716a66c`. All 105 uncompressed files equal the actual UI-tested package. Packaging metadata changes ZIP bytes; executable payload equality was verified directly. Compared with previous source4274a59's candidate, only packaged `config.py` changed; the other 104 files are identical.

For read-only hosted review:

```bash
gh pr view 152 --repo joshyorko/josh-room
gh run view 38952556154 --repo joshyorko/josh-room
gh run download 38952556154 --repo joshyorko/josh-room \
  --name managed-runtime-evidence-ubuntu-latest --dir ./review-pr152
```

## Remaining authorization and scope

Exact blocked action: merge `joshyorko/josh-room#152` at `b69eaea08b365d48fe8a04b2656d3e3b032efcdb`. Status: **DENIED by automatic approval review; specific approval pending**. Reason: earlier authority was scoped to other PRs. Completed: repair, regression/review, full hosted CI/native runtime qualification, actual packaged disposable Linux flow, verified feature-stage artifacts. After explicit merge approval, recheck head/state, retry the known-unexecuted merge once, then run exact-main CI and verify its fresh automatic stage. Publication remains prohibited.

Live Cloudflare OAuth and real MinIO infrastructure are NOT RUN. Full Windows GUI Save/restore is NOT RUN. This establishes the stale-registry repair and tested native fixture, not end-to-end reproduction of the real user's account or every cause of later missing Rooms. Raw UI/private fixture material remains local. Real user source, configuration, credentials, storage and installed extensions were untouched.

The old main 4274a59/stage 38946219816 still predates this fix and must not be mistaken for the repair. Existing Room 0.1.35 artifacts remain untouched. Supporting JAT 0.1.14 was already published under earlier approval and was not republished here. No release/tag/template publication or deployment occurred.
