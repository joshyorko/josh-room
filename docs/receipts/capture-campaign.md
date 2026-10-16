# Capture campaign candidate — review only

Implementation references: Room #144, #145, #146; JAT draft
https://github.com/joshyorko/josh-all-the-things/pull/21 (#19).

Room branch `feature/capture-campaign` is based on
`78fa48e41ddbad0c159f8f8dfa29f6b120a4f1b0`. Candidate executable source is
`a78414c4162576d0184f052f78ee11f2e48eb14b`; subsequent receipt-only commits do
not alter the package. JAT source is
`1110b5490222ef396d7bf6bf8ef0868625286569` on `feature/filtered-capture-19`.
No maintenance branch, runtime version, release pin, tag, installed extension,
or release was changed. Do not include these features in the concurrent RCC
maintenance release. Generated controller copies and canonical `cli.py` are
potential shared-file integration points; coordinate their regeneration after
that release rather than replacing its changes.

## Delivered behavior

- Exclusive, non-overwriting initialization in the selected source folder;
  explicit editing and source-only confirmation before preview.
- Bounded aggregate diagnostics using Save's traversal and validators. Multiple
  failures retain relative paths and collision pairs. Incomplete, cancelled and
  capped scans never produce a clean result.
- Native worktree multiselect, exact-rule confirmation, concurrency detection,
  configuration-only appends, fresh scan and preview. No automatic Git cleanup.
- Source-only policy/descriptor, Python/JS parity, mode-aware no-op/deletion
  confirmations, native Save/export/restore, and full-repository compatibility.
- JAT literal exclusion handoff with capability and policy-fingerprint checks;
  standalone JAT filters before archive creation and retains archive safety.

## Candidate identities

See adjacent JSON receipts for complete hashes and scopes.

- VSIX `josh-room-capture-candidate.vsix`, version 0.1.34, 508331 bytes,
  SHA256 `43c2380fb6ef09b9f9e3fe8fca94917554ee76392b8e10d813431075631323c4`.
- Cross-repository capsule `room-jat-source-only.haul.tar.zst`, 2162 bytes,
  SHA256 `306a9b0a420c212b2d389cc3bd7c4f678765cd6270832f3e6cb1404f55930ac7`.
- JAT source archive `jat-filtered-candidate-1110b54.tar.gz`, 134803 bytes,
  SHA256 `44e9557a5233f2581a8174ad85213b2ed49dc44c30ff62edc229436e81095343`.

These files are retained in the cloud task workspace, not published release
assets. The VSIX version is intentionally unchanged and it was not installed.

## Verification

Linux x64 test container `room-campaign-tests` image
`sha256:a58df4297ca9708c6ae0f9c1c85bf01120ceceedabc011f128000c670c997d15`
uses Python 3.13 and the repository-declared runtime/dev dependencies. No host
project dependencies were installed. Commands below run from the Room checkout
with it mounted into that container, `PYTHONPATH=src:.`, a writable disposable
HOME, Node available, and the existing pinned restic 0.19.1 on PATH.

- PASS: 196 focused Python tests (1 packaging test deselected and passed separately): policy, scanner, initialization, source-only, Git,
  descriptor, native storage/export, JAT handoff and repository-boundary tests.
  Real restic Save/restore verifies selected bytes at another path after the
  disposable original becomes unavailable. Existing destinations remain refused.
- PASS: `node --test vscode-extension/*.test.js`: 287 passed, 2 skipped.
  This is mocked extension/API evidence, not visible UI acceptance.
- PASS: `npm exec --yes --package=@vscode/vsce@4.0.0 -- vsce package --no-dependencies --out CANDIDATE.vsix`.
  All 64 packaged Python modules plus changed JS match canonical/template bytes.
  The real VSIX repository-boundary packaging test also passed separately.
- PASS: extracted candidate controller actual `python -m josh_room workspace
  init`, `workspace-check`, `workspace exclude --source-only --rule generated`,
  and rescan. First scan returned nonzero with Git and symlink findings; approved
  configuration changes made the fresh scan complete. Original fixture Git
  pointer and unsafe generated symlink remained unchanged.
- PASS: actual Room `run_build`/`run_restore` through RCC 18.19.5 with JAT
  candidate source overlay on environment artifact
  `sha256:ae6b5802103c780de49b42768885a8c66073747272caf759784e9eb23f44e4f1`.
  Selected source, database bytes, manifest and included worktree draft survived;
  excluded Git/generated/worktree entries were absent. Original fixture was
  unavailable during restore. This is not a newly published JAT runtime.
- PASS: JAT isolated RCC tests: 271 non-vertical tests, final 24 capture tests,
  and 5 selected real-Hauler gates. See JAT PR for commands and retained evidence.
- PASS: changed-file Ruff, `git diff --check`, targeted added-line secret/private
  path scan, and `python scripts/sync_controller_sources.py` (zero drift).

The hosted [CI test job](https://github.com/joshyorko/josh-room/actions/runs/38892717571/job/116945829752)
passed for campaign head `7c20aef844bce1d7f6950b6078727d88dbef90e8`:
`uv run pytest -q` reported **1901 passed, 28 skipped**; Node reported
**287 passed, 2 skipped, 0 failed**; Ruff and `git diff --check` passed.
The job checked out GitHub's synthetic PR merge
`4d4f9100171b0bcf48aacbd66a0a79d1233db678`. Its tree
`1ba2e7a59730cfa8ad897dd70873fd93fbd8bfef` equals the campaign head's tree,
so this evidence verifies the same source contents.

Earlier container failures remain distinct from that hosted result: missing
child-process PYTHONPATH, missing writable HOME, host Node selecting Python
without botocore, and packaging without npm. The recorded broad HOME-adjusted
run ended normally at its five-failure limit: **1388 passed, 37 skipped,
2 deselected, 5 failed**. Four failures were missing installed Room package
metadata (`runtime-version-unavailable`); one was a Secret Service expectation
that differs when `/.dockerenv` identifies a container. This was not an abrupt
exit at 60%. The existing detached-process cancellation test also fails on both
base and candidate in this container. These local failures do not establish
regressions or replace the hosted full-suite evidence.

A final container run with editable package metadata and explicit HOME/PYTHONPATH
completed: **1875 passed, 41 skipped, 13 failed**. Seven hook tests rejected the
root container reading a checkout owned by another UID; the public-storage test
also encountered Git's ownership check. Packaging lacked npm. The two existing
container authority/cancellation failures remained. Two real-restic bridge tests
use unchanged synthetic RCC `v18.19.5` metadata against the unchanged `v18.19.6`
constant and therefore report pending RCC capture. Those fixtures and production
bridge files are unchanged from the campaign base; runtime-version maintenance
belongs to the separate RCC task. No campaign regression was identified by this
investigation, and this local run is not reported as passing.

## Remaining acceptance and release gates

- NOT RUN: visible packaged VS Code create → edit exclusions → scan → resolve
  findings → Save → restore. No VS Code/display is available in this environment.
- NOT RUN: candidate source packaged into and consumed from a newly qualified
  JAT runtime artifact. Existing release pins deliberately remain unchanged;
  old JAT source fails closed when filtered capture is requested.
- NOT RUN: native Windows qualification and live-provider writes. Only
  disposable fixtures/storage were used. JAT mixed-image smoke lacked its local
  BusyBox image; JAT also records existing full-lint findings.
- Native source-only saves do not depend on JAT #19. Complete #145 UX depends
  on #144 and #146, integrated here. Filtered portable export additionally
  depends on JAT PR #21 and exact consumed-artifact qualification.

Before release: review both drafts; coordinate with the RCC maintenance owner;
qualify a JAT candidate artifact and update pins in a separately authorized
release effort; rebuild the VSIX; run the visible flow against isolated storage;
run platform/full-suite gates and reconcile existing failures. Merge, tag,
publish, deploy and extension replacement require separate authorization.

Focused Python command (inside the test container):

```sh
python -m pytest tests/test_workspace_policy.py tests/test_workspace_check.py tests/test_workspace_init.py tests/test_workspace_report.py tests/test_source_only_capture.py tests/test_git_workspace.py tests/test_logical_jat.py tests/test_room_store_operations.py tests/test_room_store_export.py tests/test_jat_capture_handoff.py tests/test_repository_boundary.py --deselect tests/test_repository_boundary.py::test_real_vsix_contains_the_owned_runtime_contract -q
```
