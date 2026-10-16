Current handoff: [0.1.36 candidate — publication on hold](release-0.1.36-final-handoff.md). The R2→MinIO transition remains unresolved; the historical evidence below does not establish a fix.

Historical campaign checkpoint; see [subsequent0.1.36 report](release-0.1.36-readiness.md) and [published JAT0.1.14](jat-v0.1.14-publication-receipt.json).

# Subsequent capture feature candidate: qualification handoff

The actual packaged Linux native workflow passed on feature source
`995c3b6953ba5880e2a21db3ceb3bceec5d90fb3`. The current feature head is
`3d28f0ec74a74971e45d4ecbd14eab4597bfb1c4`; later changes are confined to the
candidate Windows UI driver/workflow. Every uncompressed file in its hosted
VSIX matches the actual Linux UI-tested package. This source reconciles maintenance
main `e773963106829668c9bca8bf94c045c99f8267f1` without changing its release
workflow, version, release lock, or runtime pins. Both feature PRs remain drafts.
No merge, release, tag, deployment or user-extension replacement is authorized.

## Issue requirements and dependency graph

| Issue | Implemented behavior | Qualification |
| --- | --- | --- |
| Room #144 | Explicit new-Room initialization targets the selected source. Exclusive/no-follow creation preserves existing bytes and concurrent edits; native editing resumes through fresh policy/preview state. | `tests/test_workspace_init.py`, policy tests and extension creation tests; earlier disposable visible creation/edit flow retains its original attribution. |
| Room #145 | One bounded deterministic diagnostic report collects independent findings and collision pairs. Cancellation, unreadable/skipped coverage and caps fail closed. Native zero-preselected worktree review requires exact-rule confirmation, applies only approved edits and rescans. | `tests/test_workspace_report.py`, `tests/test_workspace_check.py` and extension tests; earlier actual mixed-worktree selection/cancel/rescan flow preserved. |
| Room #146 | Explicit source-only mode omits complete Git administrative entries while retaining filesystem safety, signatures, dirty/no-op invalidation and descriptor/export/restore semantics. Full mode and old-snapshot defaults remain protected. | `tests/test_source_only_capture.py`, Python/JavaScript parity and native restic tests, plus actual packaged Linux Save and relocated restore below. |
| JAT #19 | Standalone literal exclusions and capture mode reach CLI/model/service/archive and thin RCC tasks before archive creation. Retained unsafe links still fail; no Room imports or shell interpretation of ignore rules. | JAT capture tests and exact embedded Linux/Windows candidate/promotion receipts below. |

The issue bodies retain the dependency graph: #144 and #146 are independent;
#145's full exclusion-and-continue flow requires both. JAT #19 is independent of
native Room saves, while filtered JAT export requires its exact qualified artifact.
Neither excluding a checkout alone nor a clean source-only result promises full
relocatable Git worktree recovery. Git history, staging state and excluded
worktree files are outside source-only backup coverage.

## Reviewable fixes and native evidence

- Runtime archives use SHA256-prefixed cache names. Old cache files remain
  untouched; corrupt active entries and symlinks still fail closed.
- Linux RCC launches append genuine system-tool directories, including initial
  acquisition. Native runtime import now succeeds without the old test symlink.
- Empty Secret Service lookup is recognized without a fake probe item; locked,
  disconnected and other errors remain unavailable. Local-build fallback forwards
  JSON through stdin using RCC's interactive mode, keeping credentials off argv.
- Existing connection choices show endpoint and ID. Cancel exits before credential
  entry. An identical Dimension is reused without a config rewrite; conflicting
  definitions still fail.
- The encryption-ready notification no longer blocks Save. A unit test leaves
  that notice unresolved; the actual renderer also showed the next native picker
  while the notice was still visible.

Fresh native Add Storage, secure credential reuse, exact same-bucket repetition,
connection cancellation and Save/relocated restore passed on `505998a`. No CLI
credential seed was used for the new connection. Config bytes were unchanged on
repeat/cancel. See `readiness-native-ui-receipt.json`, its log, and repeat/restore
JSON. The later `995c3b6` candidate independently completed new-bucket encryption,
Save and relocated restore without dismissing the readiness notice.

Final actual UI-tested VSIX SHA256:
`b64c17189ca8c577b9f1fa3720fadb25b602dca8c35337dc3b2630822c3638d2`.
Current hosted Linux VSIX SHA256:
`a60aa06758630832befb0845b10ba4cdc929c357a25274a32190d88ba0309b7f`.
Archive digests differ because packaging metadata differs; payload equality is
recorded in `3d28f0e-hosted-vsix-receipt.json`, not treated as a new UI execution.
Version metadata remains inherited 0.1.35 for this unpublished review package;
it must not be uploaded to the staged 0.1.35 maintenance draft. Subsequent release
versioning and exact final-source rebuild remain explicit release-preparation gates.

Final snapshot `931d1bb09e1a4e858649814bbd808164` restored at another path with
the original synthetic source unavailable. Selected project/ignore bytes matched,
excluded files and Git metadata were absent, and all three original file hashes
were unchanged. The successful run used no fake probe item, system-tool symlink,
cache deletion or local runtime fallback. Legacy archive files remained present.
See `final-native-ui-receipt.json`, `final-restore-result.json`,
`final-notice-flow-result.json` and `final-native-gate.log`.

Environment: disposable Debian 13 Docker/Xvfb, VS Code 1.142.0, RCC 18.19.7,
real native Secret Service and Moto 5.1.15 S3 emulator. Earlier worktree fixture
and review/select/confirm/cancel evidence remains attributed to its earlier
candidates; no result is silently relabeled as a later-source run.

## Verification scope

- Local final source: Ruff passed; all Node tests reported 299 passed, 2 skipped.
  Focused Python tests reported 108 passed, 32 skipped (native authority/restic and
  optional release-test dependencies not available in that container invocation).
  Actual native Save/restore and separate pinned restic qualification are distinct.
- Actual RCC fallback stdin probe passed after using a disposable writable home.
  Initial missing-cache/read-only-home attempts remain failures, not passes.
  See `fallback-stdin-receipt.json`.
- [Full CI on `3d28f0e`](https://github.com/joshyorko/josh-room/actions/runs/38930204819)
  passed all applicable jobs: 1,915 Python passed, 28 skipped; 299 Node passed,
  2 skipped; both native managed-runtime consumers and Windows private-runtime
  checks passed. The main-only release-candidate job was skipped as intended.
- [Native capture qualification](https://github.com/joshyorko/josh-room/actions/runs/38930197092)
  passed 77 tests each on Linux and Windows. [Linux candidate artifact](https://github.com/joshyorko/josh-room/actions/runs/38930197092/artifacts/12057594865)
  includes the review VSIX, identity and test XML. [Windows receipt artifact](https://github.com/joshyorko/josh-room/actions/runs/38930197092/artifacts/12058795937)
  records its native tests. These backend checks do not establish visible UI acceptance.
- [Windows actual renderer run on `3d28f0e`](https://github.com/joshyorko/josh-room/actions/runs/38930197139)
  failed after 360 seconds waiting for the source-only result. Installation/payload
  identity, complete focused workbench and native palette interaction succeeded;
  no workspace-check success or full-mode Git rejection was established.
  `windows-ui-3d28f0e-failed.json` preserves the sanitized failure. Further diagnosis
  is pending; full Windows storage/Save/restore remains NOT RUN.

## Exact JAT artifact and Room integration

JAT head `d084ade2adf2945b6aa1f04ddb9ad32ed53b43c2` deliberately reconciles
maintenance main `3a3d897c31264308f1c1721d8fc3c9ebca4c8293`. Its [final candidate
qualification](https://github.com/joshyorko/josh-all-the-things/actions/runs/38928605678)
passed Linux and native Windows embedded-source filtered restore, retained unsafe
link rejection and the existing schema-v3 promotion gates. No external source
overlay was used. Local declared RCC tests passed 285 tests, including 14 new
promotion-qualification tests; focused Ruff passed. Five existing real-Hauler
checks passed on integrated source. Mixed Docker-image smoke remains blocked by
the missing local `busybox:1.36` image and is not claimed as passed.

| Platform | Hosted artifact | Archive SHA256 | Bytes |
| --- | --- | --- | --- |
| Linux | [12058112452](https://github.com/joshyorko/josh-all-the-things/actions/runs/38928605678/artifacts/12058112452) | `90b4ccb42580f93811c78b65b508d5877a01abb0ab27ee224297aded92963e46` | 170877751 |
| Windows | [12058367009](https://github.com/joshyorko/josh-all-the-things/actions/runs/38928605678/artifacts/12058367009) | `c9148b6404893f21373f600b152e28d74d5c1ebc391884682e859dd5fcbfdea8` | 104094462 |

Both downloaded archive hashes and unchanged candidate/promotion receipts were
verified. The four `jat-final-*-receipt.json` files preserve those identities.
RCC artifact digests are
`sha256:47d319a70e29d98bc24cfebef244195b0be11e205b70f4d9aa2878798dcfe0c0`
on Linux and
`sha256:dde47aaed6ed4a3c998e0c6b4f5e7061893a70ae47f1ce1f9d16b523e3064488`
on Windows. Download these 30-day artifacts before November 15, 2026.

Room `3d28f0e` consumed the exact downloaded Linux artifact through RCC18.19.7.
The adapter build/restore passed with the original unavailable, all selected
source/database/manifest/worktree-file bytes matched, and excluded Git metadata,
generated content and the omitted checkout were absent. Original hashes remained
unchanged. Capsule SHA256 is
`a2e58865d7aabe3bde6dd6d23cc0a3a33445cabc925d2b9e40a20fca0053d345`,
2,193 bytes. See `cross-final-filtered-capture-receipt.json`. This is a real
adapter plus exact embedded artifact check, separate from VSIX UI execution.

The qualified archive must be adopted deliberately. Current Room pins remain
maintenance pins; ordinary installed filtered export must not be advertised as
qualified until it consumes the approved filtered runtime. JAT's
[promotion guide](https://github.com/joshyorko/josh-all-the-things/blob/d084ade2adf2945b6aa1f04ddb9ad32ed53b43c2/docs/filtered-candidate-promotion.md)
and PR21 describe byte-preserving HTTP asset mapping and unchanged schema-v3
receipts. OCI publication alone does not provide Room's required HTTPS archive.

## Merge and publication boundary

Room is ready for review with passing Linux native evidence. Final full CI,
native capture and JAT artifact/promotion results pass at the named sources.
Visible Windows scan qualification remains unresolved and is not silently
substituted with backend tests.
Native saves are independent of JAT, while filtered portable export requires its
explicitly qualified source/runtime pair. Refer to
`docs/capture-feature-integration.md` for the safe subsequent-release sequence.

No owner product decision is needed to keep these checks running. Once the named
heads and gates are qualified, the minimal owner decision is authorization to
merge the feature delivery and adopt its subsequent version/artifact pair.
Publishing remains separate from that approval. Windows full Save/restore and
live MinIO/R2 provider acceptance are unrun; Moto does not establish those gates.
No screenshot publication was attempted or retried.
