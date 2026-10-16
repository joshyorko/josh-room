# Subsequent capture feature candidate: readiness checkpoint

The actual packaged Linux native workflow passed on feature source
`995c3b6953ba5880e2a21db3ceb3bceec5d90fb3`. This source reconciles maintenance
main `e773963106829668c9bca8bf94c045c99f8267f1` without changing its release
workflow, version, release lock, or runtime pins. Both feature PRs remain drafts.
No merge, release, tag, deployment or user-extension replacement is authorized.

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
- Hosted full test job on `505998a`:1,910 Python passed, 28 skipped;
  299 Node passed, 2 skipped. Its later maintenance merge changes release workflow
  tests and documentation; final-source CI is independently queued.
- The candidate Windows native UI workflow is queued at final source. It will
  attempt actual renderer/command-palette source-only scan and full-mode Git
  rejection; it does not test Windows native storage/Save/restore.
- JAT's current-main integration and read-only promotion qualifier are pushed.
  Its final-source Linux/Windows candidate/promotion jobs are queued. Earlier
  exact qualified artifacts and Room integration receipts remain preserved.

## Merge and publication boundary

Room is ready for review with passing Linux native evidence. Merge readiness
remains conditional on final hosted gates and the JAT artifact/promotion results.
Native saves are independent of JAT, while filtered portable export requires its
explicitly qualified source/runtime pair. Refer to
`docs/capture-feature-integration.md` for the safe subsequent-release sequence.

No owner product decision is needed to keep these checks running. Once the named
heads and gates are qualified, the minimal owner decision is authorization to
merge the feature delivery and adopt its subsequent version/artifact pair.
Publishing remains separate from that approval. Windows full Save/restore and
live MinIO/R2 provider acceptance are unrun; Moto does not establish those gates.
No screenshot publication was attempted or retried.
