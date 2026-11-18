# Composition safety prerequisite: dependent draft #200

Base: `da70c81d018703f6739624e781401fef40e58b9e` from draft #199.
Initial safety checkpoint: `76c73363f6c1a3667858dc355d8527d91e95ff4e`.
PR: https://github.com/joshyorko/josh-room/pull/200 (base #199's branch).

This is a read-only candidate-v4 decoder/reconciler and unsupported-composition
Save refusal. There is no v4 migration, remote publication, local policy edit,
resume writer, child hydration, unload or delete implementation.

## Independent review and corrections

Fresh read-only Codex GPT-6.2 Astra HIGH design review rejected writable
admission: old removers can continue descriptor deletion/Restic forget after
their catalog CAS, already-open Optimize lacks a catalog fence, and alternate
catalog keys can share one physical repository. These are source-backed blockers
in `remove_logical_catalog_records`, `complete_logical_catalog_removal`,
`optimize_room_store` and `_repository_locator`, not a claim that retention was
implemented. The corrected safety design records required fencing and atomic
slot association/readback before publication may be enabled.

Fresh read-only Astra patch review at `76c7336` requested five changes. Sol fixed:

1. Removed Save refusal from the shared scanner. Save and its preview enforce
   admission separately; restore/export continue to validate historical files.
2. Added common headless create refusal before receipts/provider setup, plus
   direct portable Save refusal before any build or instance mutation.
3. Closed legacy record fields/timestamps at the candidate boundary, preserving
   the existing v3 decoder and schema unchanged.
4. Disabled candidate selection below entry-truncated sibling scopes, where an
   unseen alias could exist. Partial coverage alone is not selection authority.
5. Validated offline inventory timestamp and parent snapshot fields instead of
   accepting arbitrary unused values.

The earlier eight fixes remain in ancestry. Astra confirmed them except for the
sample-cap alias issue, now covered above. Trusted controller cwd, path round
trips, fixed boundary stats, descriptor-bound policy, reserved device names,
output/exit bounds, `--child=` and credential cleanup remain intact.

Astra has not independently re-reviewed the fixes. Its actual verdict was
request-changes, not final approval. It never edited or implemented code.

## Verification and limits

- PASS `PYTHONPATH=src python tests/test_composition_safety.py`: final 22 cases,
  19 passed / 3 skipped. Uses system Python, stdlib and existing source only.
  Covers exact pins after newer child Save, missing workspace/component snapshots,
  scope/tree/digest conflicts, phase consistency, duplicate/unknown evidence,
  copied state, replay-stable read-only reports, legacy field closure, entry-cap
  aliases, ignored/dangling-link metadata and local no-op refusal.
- SKIPPED actual CLI reconciliation, common headless create and native scanner /
  bridge cases locally: system Python lacks `botocore`. These tests are present
  for normal CI dependencies; no fake provider module was substituted.
- PASS `node --test --test-name-pattern='composition' vscode-extension/extension.test.js`:
  four native harness cases, including refusal before any controller/provider
  work and untouched source metadata.
- PASS affected Python syntax, candidate schema JSON parsing, controller and
  extension mirror byte parity, source synchronization and `git diff --check`.
- PASS #199's exact failed command/template contract by direct stdlib invocation;
  repair checkpoint `da70c81` is pushed. Full results belong to hosted CI.
- NOT RUN local dependency installs, builds and full matrices under the supplied
  btrfs threshold. Host has substantial free space inside allocated chunks, but
  reported unallocated space remains below the configured gate. No sudo balance
  ran; password entry was declined. No user data or caches were deleted.
- Hosted full CI was explicitly dispatched for `76c7336` because pull-request CI
  only auto-triggers for main-target PRs. That dispatch is not final-fix evidence;
  the final checkpoint requires its own exact-SHA dispatch and terminal result.
- FAIL exact-SHA full CI run `44361328920` at `a35400e`: eight Ruff findings
  (exception type, import ordering, combined context managers and dict literals).
  Its full Python/Node regression steps were skipped after lint failed. The four
  automatic PR checks passing did not qualify this separately dispatched run.
  Sol corrected the lint findings and repeated the lightweight suite: 19 passed,
  three dependency skips. The next exact-SHA full run must verify lint and the
  previously skipped regression stages before any all-green claim.
- CI trigger repair: full CI now runs for dependent pull requests as well as
  main-target PRs. Main push scope and read-only workflow permissions are
  unchanged. This makes full lint/regression failures visible to the PR watcher
  instead of relying on a separate dispatch omitted from automatic check counts.
- NOT RUN installed VSIX, native RCC/JAT/age vertical, live providers or platform
  qualification. All fixtures are synthetic; no real work/PHI data was accessed.

The offline CLI labels evidence `offline-untrusted-input`. Even an internally
consistent fixture returns `can_apply=false` and `can_resume=false`. This does
not prove ciphertext was fetched/authenticated or actual payloads restored, and
must never be presented as a durable composed recovery point.

## Final-review follow-up

Astra's final read-only review at `89ffe12` requested four evidence-validation
corrections: pre-commit phase with an already published parent output, output
identity equal to prior baseline, non-array descriptor/inventory containers, and
present null legacy origin. Sol reproduced all four (nine failing subcases),
then added the boundary checks without changing ordinary v3 decoding.

`PYTHONPATH=src python tests/test_composition_safety.py` now reports 26 cases:
23 passed, three dependency skips. Valid pre-commit fixtures have no published
output record; empty evidence arrays remain valid for a genuinely empty reserved
state. Missing arrays and present null origins are refused. Source mirrors and
`git diff --check` pass. The revised checkpoint requires a new explicit Astra
approval and hosted CI; the green run at `89ffe12` remains historical evidence.

The follow-up review at `cbc7058` confirmed those four fixes and found one
additional inconsistent state: an allocated child record without its pin in a
reserved operation. Sol added the converse record/pin check. Its regression
failed before the fix and now passes, including valid partial reservations and
genuinely unfilled slots. The focused suite now has 27 cases: 24 passed, three
dependency skips. Exact-head final approval and CI remain required.
