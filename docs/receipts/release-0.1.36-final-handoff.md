Repair update: [qualified PR #152 and feature review stage](r2-minio-repair-handoff.md). The repair is unmerged pending specific merge approval; the source4274a59/main-stage evidence below remains historical and unresolved.

# Room 0.1.36 candidate handoff — publication on hold

**Do not publish this candidate while the R2→MinIO failure is unresolved.** Josh reported a real 0.1.35 failure after R2 OAuth then MinIO: connection creation completed, immediate bucket lookup reported `connection [id] is missing`, and later buckets appeared without rooms. Current 0.1.36 source retains the stale configuration-snapshot path. A disposable configuration-only reproduction confirms that a newly persisted connection is absent from runtime-snapshot lookup. Full provider/UI causality and the later missing rooms are **NOT_REPRODUCED**; no fix is claimed. Josh owns the repair. No user credentials, configuration or buckets were accessed. [Assessment and reproducible evidence](r2-minio-release-risk.md).

This evidence branch is for review only; do not merge it or use it as publication source. The candidate source is main `4274a59fd4a15e97bf51dd2f74aca7ef41bac86e`, tree `5cf97e3e52faed022eeec30d558ca88eee7acfd0`, identical to qualified PR #151 head `35341da6378a0af38cf869710f5c6a10913f06c3`. Room 0.1.36 and template 0.1.6 remain unpublished. Existing Room 0.1.35/template 0.1.5 artifacts are untouched. RCC 18.19.7 and controller pins are unchanged by this campaign.

## Delivered requirements and integration

| Requirement | Delivery and evidence |
| --- | --- |
| Room #144: initialize and explain exclusions | Non-overwriting, no-follow initialization in the explicitly selected folder; editing precedes fresh preview. Existing/custom/concurrent contents preserved. |
| Room #145: aggregate scan and native selection | Bounded structured findings and incomplete coverage; native review starts with none selected, cancellation makes no change, exact confirmation precedes edits, fresh scan retains unresolved Git administrative blockers. |
| Room #146: source-only capture | Explicit mode, full-repository compatibility defaults, filesystem safety, signatures/no-op invalidation, descriptors, export and relocated restore; omitted Git history/staging/worktree contents explained. |
| JAT #19: independent filtered capture | Literal exclusions and mode across standalone CLI/model/service/archive/thin RCC adapters; selection precedes archive creation and retained unsafe links still fail. No Room imports or manual source duplication. |
| Room→JAT integration | Room translates its effective policy to versioned JAT capture inputs; actual published Linux artifact restored selected payload with original source unavailable. |

Room #144 and #146 supplied the contracts required by #145's complete UX; reporting proceeded independently. Native Room Save remains independent of JAT #19. Filtered installed-runtime integration required JAT #19 plus qualification of the exact consumed artifact. Full relocatable Git worktree preservation is outside this delivery.

| PR / branch | Qualified head → merged main |
| --- | --- |
| [Room #147](https://github.com/joshyorko/josh-room/pull/147), `feature/capture-campaign` | `62fd601f50f5bd67823a8b3a474c0036f2f60b29` → `a5fa91a00ef24cd09025d3b09aa8deaa8232adfd` |
| [JAT #21](https://github.com/joshyorko/josh-all-the-things/pull/21), `feature/filtered-capture-19` | `2f9c671985cfcc055d5ab54e6e2e5bc5127c78f6` → `fe43065a654d1649ba9ebe5a6f202eac612e4385` |
| [Room #150](https://github.com/joshyorko/josh-room/pull/150), `release/capture-0.1.36-jat14` | `bdf08abccb186c3dd51877d15816f62869c95387` → `15286581b86abc1aa7f1b5ad3a537466bf959936` |
| [Room #151](https://github.com/joshyorko/josh-room/pull/151), `fix/restic-probe-chunk-fixture` | `35341da6378a0af38cf869710f5c6a10913f06c3` → `4274a59fd4a15e97bf51dd2f74aca7ef41bac86e` |

These merges followed later explicit user approval of qualified feature merges. The separate RCC maintenance branch and unrelated original checkout changes were preserved. Shared Python/CLI ownership was divided before implementation; orchestration integrated generated mirrors, packaging and policy handoff.

## Supporting JAT runtime: published with approval

Immutable [JAT v0.1.14-jat-runtime](https://github.com/joshyorko/josh-all-the-things/releases/tag/v0.1.14-jat-runtime), release ID `415200660`, is published from exact `fe43065a654d1649ba9ebe5a6f202eac612e4385` after explicit user approval. Native Linux/Windows source-embedded artifacts passed filtered restore and schema v3 promotion in [run 38934211098](https://github.com/joshyorko/josh-all-the-things/actions/runs/38934211098). Native publication and readback passed in runs [38938335140](https://github.com/joshyorko/josh-all-the-things/actions/runs/38938335140) and [38938737552](https://github.com/joshyorko/josh-all-the-things/actions/runs/38938737552), preserving the qualified archive bytes. All eight asset names, hashes, sizes and source/tag identities were independently verified. [Publication receipt](jat-v0.1.14-publication-receipt.json).

| Platform | Archive SHA256 | Bytes |
| --- | --- | ---: |
| Linux x64 | `de39c0f9374dfae541ee8688b643ed53e950331ee4efe9ca498c6b28b89cdf09` | 170877798 |
| Windows x64 | `8bbf7aa8925b5a517164e7e6ea00b735394cc1eac026ba34404e729d89e441b1` | 104094478 |

Actual live release metadata, peeled tags and both runtime pairs passed verification before adoption. Non-JAT manifest/lock entries and RCC/controller pins remained unchanged.

## Exact staged Room candidate

[Automatic stage 38946219816](https://github.com/joshyorko/josh-room/actions/runs/38946219816) passed with publication **SKIPPED**. Actions artifact `release-stage-files`, ID `12064968004`, contains the six assets below plus `release-candidate.json`. Download ZIP: 2,782,249 bytes, SHA256 `7ebe64b0c53c0ab910a8a6f606a3ab25a1aafcac97baf548dd76b0bef66f4916`. All six hashes and sizes match the source-bound receipt. The orchestrator independently ran the canonical staging verifier against a fresh extraction and verified the official artifact identity.

| Asset | SHA256 | Bytes |
| --- | --- | ---: |
| `SHA256SUMS` | `76b44b616010bf8c52a7d29c7929be76945d8755935ac0c2a112cc4f5baaff37` | 286 |
| `ci-readiness.json` | `228694ac07915294545062d33d01abb51cea896ca995da9ecb4079f5de6944ad` | 2125 |
| `josh-room-0.1.36.vsix` | `7fbf0fd59f2279f7a99f84b855b386721d568f0c2e0dcdf034ba3a2cccfb34ed` | 509157 |
| `josh_room-0.1.0.dev0-py3-none-any.whl` | `c9184ac07d8af47b79223695fd26850d61d88feff31ab99da9734c0ad50a56dd` | 418536 |
| `josh_room-0.1.0.dev0.tar.gz` | `8a63368db0e381e5069151e9cc5260e640484b7eb7a892530df70e3da64328e9` | 1869351 |
| `vsix-candidate.json` | `c39f8cd00a5b267dde9ab498b2ff9d14ce59a3447d9a0f2e877ccdce7e958b36` | 347 |

[Original stage receipt](main-4274a59-stage-candidate.json), [all file hashes and comparisons](main-4274a59-stage-payload-parity.json), [independent canonical verification](main-4274a59-stage-root-verification.json). The VSIX is 509,157 bytes, SHA256 `7fbf0fd59f2279f7a99f84b855b386721d568f0c2e0dcdf034ba3a2cccfb34ed`. Its embedded manifest contains exact JAT 0.1.14 source and the published Linux/Windows archive pins. These staged bytes are candidates, not a released or repaired product.

## Verification and practical scope

[Exact-main CI 38944493749](https://github.com/joshyorko/josh-room/actions/runs/38944493749) is **PASS** for source `4274a59`:

| Command / job | Environment | Observed result |
| --- | --- | --- |
| `uv run ruff check src tests` | Declared GitHub Actions Python/dev environment | PASS |
| `uv run pytest -q` | Same declared environment | 1,915 passed, 28 skipped |
| `node --test vscode-extension/*.test.js` | GitHub Actions | 304 passed, 2 skipped, 0 failed |
| `git diff --check` | Exact CI checkout | PASS |
| `node scripts/managed_runtime_acceptance.js` | Native Ubuntu and Windows hosted runners; actual packaged managed RCC consumers | Both PASS, including native JAT capture/restore and restic probes |
| `python scripts/verify_private_runtime.py` | Native Windows | PASS |
| `release-candidate` | Exact-main trusted CI | PASS |
| `python3 -m scripts.release_staging verify …` | Existing task CPython 3.12.14; standard library, fresh stage extraction, no dependency installation | PASS; exact command in independent receipt |

Skipped tests remain skipped; platform/backend checks and live-provider checks are reported separately below.

Canonical controller/template synchronization and Python/extension capture-policy behavior are covered by the focused checks and full suites. Structured scanner tests cover independent blockers, cancellation, truncation and unreadable/incomplete coverage; initialization tests cover no-overwrite/concurrent creation and the selected source differing from the editor workspace. Full-repository protections and old-snapshot defaults remain covered.

The actual `62fd601` Linux VSIX (SHA256 `7472a9624da838e761554d3b06c610b7aad116f5924ccefb38d9f5d151cb54a1`) completed create → selected-folder initialization → edit → aggregate scan → review with none selected → cancel unchanged → exact confirmation → rescan retaining administrative blockers → explicit source-only confirmation → fresh complete scan/preview → Save → relocated restore with the original unavailable. All 47 original file/Git records stayed unchanged. [Historical feature qualification](release-0.1.36-readiness.md).

The actual `5fba3bb` runtime-adoption VSIX (SHA256 `d1eefbf5716e0603ca022e0d45ec625945af4fc1ad33608e162bfabda9bc8930`) completed native Room Save → original unavailable → relocated Room restore → Export Portable JAT → native JAT restore using published JAT 0.1.14. Project, database, selected upload, retained worktree draft and ignore bytes matched; all 50 original file/link entries stayed unchanged. Git metadata, generated artifacts including an excluded unsafe link, and the omitted checkout were absent. Capsule SHA256 `59f10d02fc74024d84796521b13f1dfe0540b11861d5d67bf8d8b78a6f48ffbe`, 2,256 bytes. The orchestrator independently recalculated original/restored hashes, link targets and capsule identity. [Native flow receipt](5fba3bb-native-jat14-receipt.json).

All 105 decompressed files in the `bdf08ab` VSIX (SHA256 `cd1c6aeca35e8270e0264168755fe3b3d8bc20a43a2d2e347f0ea598825ef7e5`) equal that UI-tested `5fba3bb` package. It was installed into a disposable profile and a fresh native scan returned “Workspace valid · 8 entries.” [Package parity](bdf08ab-vsix-parity.json), [installed scan](bdf08ab-native-install-scan.json), [independent verification](bdf08ab-root-verification.json).

Every one of the final stage's 105 uncompressed VSIX files is byte-identical to **both** those actual `5fba3bb` and `bdf08ab` packages. Root independently recalculated their package hashes and compared the decompressed bytes. Later commits changed acceptance tooling and documentation outside the product payload. Exact executable-payload equality is separate from UI execution; the earlier full flow is not presented as a new full stage UI run.

[Windows UI run 38939436211](https://github.com/joshyorko/josh-room/actions/runs/38939436211) passed installed-package verification, source-only scan and fresh full-mode Git rejection. Its VSIX SHA256 is `b8cb833c210f4fb8fb3b3eea83185ddfb2251ce7cbf5c3be09cf0698f852f351`. Full Windows GUI Save/restore is **NOT RUN**. Native managed backend consumers are separate evidence.

Linux UI used disposable Debian 13, Xvfb, VS Code 1.142.0, native SecretService and Moto 5.1.15. No real repositories or storage were used. Live MinIO/R2 and the R2 OAuth→MinIO transition are **NOT RUN**. MinIO-only Moto evidence does not establish this transition works. Direct raw-folder JAT Pack was not qualified as a Room-policy operation; filtered Room export uses Export Portable JAT. Standalone JAT explicit exclusions were qualified independently. The optional mixed-Docker capsule smoke was **SKIPPED** because its `busybox:1.36` prerequisite was unavailable. Raw UI logs/screenshots remain local.

Retained failures and corrections are recorded without turning retries into acceptance: historical Windows PATH failures; local registry/upload credential rejection before writes, followed by authorized native Actions publication; initial packaging ENOSPC followed by removal of positively idle campaign materializations; an invalid synthetic consumer environment coupled to the JAT producer freeze, corrected in #150 without removing RCC/restore assertions; and main `1528658`'s random 8 MiB restic probe variance. #151 changed only that disposable fixture to 64 MiB, retaining its 50% threshold and every assertion. Fifteen focused tests and actual native Linux/Windows full probes passed. [Failed probe provenance](main152-windows-probe-failure-provenance.json), [corrected Windows probe](pr151-windows-restic64-provenance.json).

## Required next work and publication boundary

The action **publish Room 0.1.36/template 0.1.6** is **pending**. Its readiness recommendation is blocked by the unresolved R2→MinIO transition, not by an approval-tool denial. No Room release/tag/template publication, deployment or real extension replacement was performed.

Josh's repair needs isolated acceptance of R2 OAuth → MinIO connection creation → immediate bucket listing → Dimension/room loading, followed by full exact-source CI and a fresh stage. A new stage must bind the repaired source. Current green capture checks and staged artifacts are useful scoped evidence and do not establish a fix.

For read-only review of this historical candidate, use a new disposable output directory from an isolated checkout of exact `4274a59`:

```bash
gh run view 38944493749 --repo joshyorko/josh-room
gh run view 38946219816 --repo joshyorko/josh-room
gh run download 38946219816 --repo joshyorko/josh-room --name release-stage-files --dir ./review-0.1.36-4274a59/assets
gh api repos/joshyorko/josh-room/actions/runs/38946219816 > ./review-0.1.36-4274a59/stage-run.json
python3 -m scripts.release_staging verify \
  --directory ./review-0.1.36-4274a59/assets \
  --source-sha 4274a59fd4a15e97bf51dd2f74aca7ef41bac86e \
  --release-tag v0.1.36-standalone-vsix --version 0.1.36 \
  --checks ./review-0.1.36-4274a59/assets/ci-readiness.json \
  --runtime vscode-extension/runtime/manifest.json --release-lock release-lock.json \
  --stage-run-json ./review-0.1.36-4274a59/stage-run.json \
  --stage-run-id 38946219816 --stage-workflow-id 341135067
```

These commands review existing candidate bytes; they do not publish. After the repair, replace this source/run/artifact set with newly qualified values. No approval can substitute for the unresolved technical release gate.

After that repair and qualification, final publication remains Josh's action through the repository's guarded release workflow. Do not publish the current known-unresolved stage. Existing 0.1.35 assets must remain untouched.
