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

## Follow-up candidate qualification

Final executable source is `60b73937628d3909a34b08772849cbc1a56ccf90`;
`ae4ab4334c25809772aff66780bc6af4762d8f64` only formats test imports.
Visible UI testing exposed a missing native `joshRoom.checkWorkspace`
registration, fixed with a regression test. Native Windows testing exposed
CRT newline translation in ignore-file reads; `O_BINARY` now preserves the raw
policy bytes used by JavaScript fingerprints. Full-repository protections remain.

[Full hosted test job](https://github.com/joshyorko/josh-room/actions/runs/38893875252/job/116950078297)
passed: **1902 Python tests, 28 skipped; 288 Node tests, 2 skipped**; Ruff and diff checks passed.
Synthetic PR merge `33ff1ce1244622a5c3df9a536050ed126603886d` has tree
`f35dd326ee9e9d9ac6cd5cc739cf555e27cf5eb0`, exactly equal to `ae4ab43`.
This replaces the earlier broad-suite uncertainty. [Managed-runtime Linux consumer](https://github.com/joshyorko/josh-room/actions/runs/38893875252/job/116950077813)
also passed; its [evidence artifact](https://github.com/joshyorko/josh-room/actions/runs/38893875252/artifacts/12043032732) is retained. [Windows managed-runtime consumer](https://github.com/joshyorko/josh-room/actions/runs/38893875252/job/116950078054)
also passed, with [Windows evidence](https://github.com/joshyorko/josh-room/actions/runs/38893875252/artifacts/12042814550).
All applicable jobs in this CI run passed; release-candidate was skipped.

[Native Linux and Windows candidate workflow](https://github.com/joshyorko/josh-room/actions/runs/38893868899)
passed **77 tests on each platform**, with no skips, including actual restic
source-only Save and original-unavailable restore. The
[Linux VSIX and receipt](https://github.com/joshyorko/josh-room/actions/runs/38893868899/artifacts/12043341063)
and [Windows test receipt](https://github.com/joshyorko/josh-room/actions/runs/38893868899/artifacts/12043635209)
are durable workflow artifacts. Downloaded Linux package hash was verified:
`2120b0a8fbc7c3df1b1a90aae7273bc0418c844da3fc9977148a31cab5485f41`.
See `hosted-vsix-receipt.json` for exact source and test-file hashes.

JAT now embeds its source and thin task bridge in the candidate artifact.
[Linux and native Windows qualification](https://github.com/joshyorko/josh-all-the-things/actions/runs/38893454020)
passed at `e1332dd30e5aefd6820df36889adc86a4b7915dc`: embedded file hashes,
no checkout overlay, five byte-exact restored files, original source unavailable,
and retained unsafe-link rejection (neither platform skipped that check).
[Linux artifact](https://github.com/joshyorko/josh-all-the-things/actions/runs/38893454020/artifacts/12042762601)
SHA256 `d0ccc70eca1b6733f12fd48d1d135a85aec94a91a7c03141c624f767e71e99e7`;
[Windows artifact](https://github.com/joshyorko/josh-all-the-things/actions/runs/38893454020/artifacts/12042573212)
SHA256 `3a7cb0367f3a8271f0ae6345891338c2e6fe8d97cd793f8387af68a22f51d9c6`.
These JAT artifacts expire 2026-11-15; retain them before expiry if review continues.

Room `ae4ab43` consumed the exact downloaded hosted Linux JAT artifact through
RCC18.19.6. Its temporary adapter paths symlinked only to the acquired artifact's
embedded package and task bridge; no JAT checkout source overlay was used.
Actual build and restore passed, original fixture was renamed unavailable,
selected app/database/worktree/manifest bytes matched, excluded entries were
absent, and source hashes remained unchanged. See
`cross-hosted-capture-receipt.json`. Capsule SHA256:
`ef76418c50759eec6827513c372ce5096e331a78d4a2817e031c4559e9db24a2`.
This supersedes the earlier source-overlay proof for exact candidate integration.
Release pins deliberately remain unchanged and do not yet select this candidate.

## Remaining acceptance and release gates

Visible packaged VS Code acceptance is **partial; native Save failed and restore
is blocked**, not passed. Exact local VSIX `60b7393` SHA256
`92aeafdcb9cc03f95644e53285918c32e1537f3b8f2eaf1d222a29332ec99246`
ran in VS Code1.142.0 / Debian13 Docker+Xvfb with a disposable profile,
SecretService and Moto5.1.15 S3 emulator. Installed feature files matched the VSIX.
The actual UI passed selected-folder initialization (different editor workspace),
Monaco exclusion editing, grouped Git findings, zero-preselected multiselect,
exact-rule confirmation, fresh scan still rejecting main-repository registrations,
explicit source-only confirmation, complete valid rescan, recovery key creation,
and Save preview/confirmation. All50 original fixture file/link hashes stayed
unchanged. Only approved ignore-file content was added to the selected source.
Native cancellation also passed using a separate real linked-worktree fixture:
select one folder, inspect exact-rule modal, then Cancel; existing55-byte ignore
content remained byte-identical and no exclude/snapshot-create command ran.
See `native-cancel-result.json` and `native-cancel.log`.

At2026-10-16T11:35:56 native Save for `UI Capture Fixture` failed before snapshot
creation: pinned RCC18.19.6 rejected the JAT artifact with
`os.requiredLibraries requires ld-linux-x86-64.so.2, worker has` an empty set;
the UI reported `managed RCC did not produce an execution receipt`.
Actual app PATH includes /sbin and /usr/sbin; ldconfig exists and lists that loader.
No pin/source changes or native Save bypass were attempted. Native restore is
unrun because no snapshot exists. This is an execution failure, not an approval
denial or uncertain success.

Initial controller import had the same library rejection; native Build Locally
fallback succeeded. Native Add Storage credential submission then failed with
JSON decoding because fallback stdin was not forwarded. Disposable connection
setup used the packaged CLI and real SecretService; the empty-keyring probe
also required a synthetic reachability item. Fully native storage setup is
therefore not passed. See `native-ui-receipt.json`, `native-gate.log`, and
`source-integrity.json`. No live-provider acceptance was performed.

Automatic approval review rejected screenshot upload to the public repository
because an uninspected image could disclose workspace details. Screenshots remain
local; these sanitized textual receipts are the public evidence alternative.

Native source-only saves do not depend on JAT #19. Complete #145 UX depends
on #144 and #146, integrated here. Filtered portable export additionally depends
on JAT PR #21 and the qualified exact artifact above.

Before release: review both drafts, coordinate runtime adoption with the separate
RCC maintenance owner, resolve visible UI/runtime gates, rebuild the VSIX with
approved release pins, and rerun acceptance. Merge, tag, publish, deploy and
installed extension replacement require separate authorization.

Focused Python command (inside the test container):

```sh
python -m pytest tests/test_workspace_policy.py tests/test_workspace_check.py tests/test_workspace_init.py tests/test_workspace_report.py tests/test_source_only_capture.py tests/test_git_workspace.py tests/test_logical_jat.py tests/test_room_store_operations.py tests/test_room_store_export.py tests/test_jat_capture_handoff.py tests/test_repository_boundary.py --deselect tests/test_repository_boundary.py::test_real_vsix_contains_the_owned_runtime_contract -q
```

Post-maintenance review commands (run in repo-native managed/CI tooling, not an
arbitrary host environment):

```sh
python scripts/sync_controller_sources.py
git diff --check
uv run ruff check src tests
uv run pytest -q
node --test vscode-extension/*.test.js
npm --prefix vscode-extension run package
```

Then install the resulting candidate only into a disposable VS Code profile and
repeat the native creation/exclusion/Save/restore flow against isolated storage.
The release owner must select the qualified JAT artifact and approved RCC pins;
this campaign does not authorize those changes or any merge/publication.
