# Draft PR: make bounded onboarding discovery useful and explicit (#189)

## Summary and root cause

Base: `c4db26925364b2c3dc4642b8ec10374e8ddc7712`, verified against live
`main` through GitHub's commit API before editing. The isolated branch is
`fix/bounded-onboarding-discovery-189`. The commit containing this receipt is
the implementation commit; no GitHub branch or PR has been published.

Warnings were appended inside traversal/link loops, causing repeated messages.
Separately, FIFO admission and stopping at 32 components could spend all work
on incidental directories and miss repositories and nested instruction scopes.
Queue emptiness also failed to reveal earlier truncation. This fix separates
bounded directory discovery from component selection, prioritizes recognized
markers, deduplicates links/cycles and reports structured coverage. It does not
claim complete inventory of unexplored subtrees.

The CLI returns additive `onboarding.discovery`; the first-run confirmation
shows coverage and main reasons before preview, including forceCreate and a
selected source different from the active workspace. The guide groups reasons
instead of printing every component warning. Existing files remain create-only.

## Documentation impact

Updated `docs/workspace-capture-policy.md` with limits, admission order, coverage
fields/codes, remaining sampling limitations and the explicit manual backup,
removal, initialization and review procedure for replacing an existing guide.
Updated the extension README and its template copy with the first-run notice.

## Synthetic comparison

The same disposable fixture was inspected with the base module and the changed
module: 36 `repo-00` through `repo-35` components, each with `.git`, a synthetic
manifest, scoped AGENTS.md, README and 12 local Markdown targets with cycles;
150 additional incidental directories. No private workspace was used. The
base module was loaded from `git show BASE:src/josh_room/workspace_onboarding.py`;
neither implementation executed fixture scripts, tasks or hooks.

| Observation | Base | Changed |
| --- | ---: | ---: |
| Admitted components | 0 | 32 |
| Nested instruction scopes read | 0 | 36 |
| Directories visited | 128 | 128 |
| Markdown documents read | 0 | 256 |
| Observed component candidates | unknown | 36 |
| Extra eligible document link targets observed | unknown | 160 |
| Compatibility warnings | 2 | 35 distinct reason/scope summaries |
| Main reasons in guide | 2 | 4 |
| Coverage | unstructured incomplete warning | structured partial |

Changed counters also report 187 directory probes and 762 enumerated entries.
Observed omission events/targets: project cap 4, pending-directory cap 58,
visited-directory cap 1 and document cap 160. These are not totals for omitted
subtrees. The 35 compatibility warnings comprise 32 component document notices
and three global reasons, with no identical duplicates.

### Base limits excerpt

```text
- Directory budget reached
- Discovery budget reached; discovery incomplete
```

### Changed limits excerpt

```text
Coverage: partial. Visited 128 directories; admitted 32 of 36 observed components, 36 nested instruction scopes and 256 documents.
Undiscovered instruction scopes remain unknown; observed omissions are not totals for unexplored subtrees.
- document-cap: 160 observed omission(s); further coverage unknown.
- pending-directory-cap: 58 observed omission(s); further coverage unknown.
- project-cap: 4 observed omission(s); further coverage unknown.
- visited-directory-cap: 1 observed omission(s); further coverage unknown.
```

## Bounds and safety

The existing caps remain 32 components, 128 visited directories, 128 pending
directories, 256 sampled entries plus one sentinel per visited directory, depth
3, eight Markdown documents per admitted component and 64 KiB per file. Child
marker probes are bounded by `1 + 128 * 256`; no recursive inventory is added.
At most three sanitized relative examples of at most 160 characters are kept
per reason/scope. Unexplored descendants remain unknown.

Tests cover 36+ components and 128+ directories, early/late and nested scopes,
README starvation, 256+ entries, depth 4, permission failures, oversize/missing
files, an empty queue after project-cap exhaustion, exact document budget,
duplicate/cyclic links, reversed creation/enumeration, bounded entry consumption,
qualified architecture/commands and workdirs, and source/workspace separation. A failing descriptor-lifetime regression also
proved the DT_UNKNOWN entry-type path must retain its directory handle; entry
type checks now finish before that handle closes.
Existing tests retain URL/escape/symlink protection, JSONC and byte/mtime
preservation, exclusive creation, editor-none, and exclusions-only behavior.
An independently marked repository beyond the walk depth cannot lend its
linked command to an outer component. A previously editable-installed module
was detected during CLI verification; the CLI test now explicitly points its
subprocess at this checkout's `src`.

Discovery does not execute scripts, hooks, installers or tasks; read credential
stores or external symlink payloads; contact providers; install extensions; or
write discovered source files. Only selected missing onboarding artifacts are
created. Save validators, capture policy, encryption, JAT and RCC are unchanged.

Ordering is deterministic within admitted entry samples, not across arbitrary
overfull filesystem enumeration. Important evidence behind unvisited unmarked
parents can still be missed. Extra link counts describe eligible paths observed,
not verified contents of unread files. No claim is made about private Rooms.

## Verification evidence

Platform: Linux x86_64 in the restricted workstation execution environment;
Python 3.12.14 / pytest 9.2.0 from the existing project runtime, Node v26.13.0,
age v1.3.2 installed. No host packages or new environments were installed. Node 22,
Windows, installed VSIX and live providers are not qualified by these results.

Commands below use `PY` for that existing Python, `RUNTIME_BIN` for its bin
directory and `TASK_TMP` for the disposable per-task directory. Paths are
normalized to avoid publishing operator-specific paths.

- PASS: initial regressions against the base produced 11 expected failures.
- PASS: `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest -q -rs -p no:cacheprovider tests/test_onboarding_discovery.py tests/test_workspace_onboarding.py tests/test_workspace_init.py tests/test_workspace_check.py tests/test_real_vertical.py tests/test_real_r2.py` — final tree, 54 passed, 2 skipped. Includes actual CLI JSON, preservation and bounded synthetic discovery.
- PASS: `PATH="$RUNTIME_BIN:$PATH" JOSH_ROOM_TEST_PYTHON="$PY" PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 TMPDIR="$TASK_TMP" node --test --test-skip-pattern='HTTPS download aborts a redirected active response' vscode-extension/*.test.js scripts/verify_node_version.test.js` — 369 passed, 2 managed-runtime skips; one loopback case explicitly excluded. Includes the actual CLI in forceCreate/Save and picker/Cancel first-run flows, Continue and selected-source checks. Final missing-link and descriptor-lifetime changes were followed by focused Python and four first-run extension checks.
- PASS: `"$PY" -m ruff check src tests`.
- PASS: `python3 scripts/sync_controller_sources.py` — synchronized both controller copies; final rerun changed zero files.
- PASS: byte comparisons of all 87 canonical Python/JSON/license files against each packaged controller copy, plus extension.js and README parity.
- PASS: `node --check vscode-extension/extension.js`, `node --check templates/room/vscode-extension/extension.js`, `bash -n .devcontainer/bootstrap.sh templates/room/.devcontainer/bootstrap.sh`, and `git diff --check`.
- PASS: repository `material_security.scan_content` over `git diff --unified=0` and complete new files returned ALLOW. A full-file scan of existing extension tests returned CONTENT_MATCH; the same match was confirmed in the unchanged base test file. This is a bounded repository-pattern scan, not an exhaustive external secret audit; gitleaks/trufflehog were unavailable.
- FAIL / incomplete: the initial broad Python run hit a blocked npm registry request during the existing VSIX packaging test. Earlier environment failures were also observed; no complete summary was produced.
- FAIL: a diagnostic Python run stopped with 260 passed and one failure because synthetic encryption state defaulted to a read-only host state directory. The subsequent run isolated XDG/config state under TASK_TMP.
- FAIL: `PATH="$RUNTIME_BIN:$PATH" XDG_STATE_HOME="$TASK_TMP/state" XDG_CONFIG_HOME="$TASK_TMP/config" JOSH_ROOM_CONFIG_DIR="$TASK_TMP/config/josh-room" PYTHONDONTWRITEBYTECODE=1 JOSH_ROOM_R2_LIVE=0 JOSH_ROOM_FIRST_RUN_LIVE=0 "$PY" -m pytest -q -rs -p no:cacheprovider -k 'not test_real_vsix_contains_the_owned_runtime_contract' --basetemp="$TASK_TMP/pytest-isolated"` — 2,150 passed, 7 failed, 10 skipped, 1 deselected. This broad run preceded the final missing-link, descriptor-lifetime and explicit CLI source-path assertions. All seven failures are unchanged `test_pcc_hooks.py` configuration trust assertions listed below. The sandbox reports `/` and `/tmp` owned by UID 65534 while the process is UID 1000. `_validate_config_security` rejects that ancestry; no trust gate was weakened.
- FAIL: initial Node run with corrected network scope had 368 passed, 1 failed, 2 skipped because a CLI-contract test found host Python without botocore. Setting PATH to the existing project runtime resolved that failure (369 passes above).

Seven broad-run hook failures:

- `test_remove_without_owned_hooks_is_idempotent`
- `test_install_receipt_failure_rolls_back_and_no_final_lf_is_exact`
- `test_install_preserves_unrelated_hooks_and_remove_rolls_back`
- `test_status_detects_stale_runtime_manifest`
- `test_stale_repair_and_command_windows_contract`
- `test_unowned_same_command_conflicts_with_install`
- `test_remove_refuses_unowned_duplicate_canonical_command`

### Skipped, blocked and unrun checks

- SKIPPED: real JAT/age vertical — RCC-first JAT/Hauler/tar/zstd toolchain unavailable. age alone does not establish that vertical.
- SKIPPED: live R2, MinIO and private first-run acceptance — secret-gated, no provider writes authorized.
- SKIPPED: native secure authority (session bus missing), two exact managed RCC/JAT bridge cases, secure Room acceptance (registry/tool-gated), opt-in large streaming acceptance, and native Windows ChangeTime.
- SKIPPED: two Node managed-runtime cases lacking opt-in/runtime.
- BLOCKED: unrestricted `node --test vscode-extension/*.test.js scripts/verify_node_version.test.js` required loopback access (`127.0.0.1`). The sandbox denied the network operation; the initial Node log was empty, so full command outcome is uncertain, not a pass. The narrower command above excluded only that network test and completed.
- BLOCKED: VSIX packaging attempted `npm run package` / npx against `registry.npmjs.org`; sandbox network policy denied the request. Python tests before that point executed, but package/suite completion was not established. The package operation was not retried.
- BLOCKED then recovered read-only: `ghx issue view` for #189/#170 could not reach `api.github.com` (domain not allowlisted). Both full issues were subsequently retrieved through the GitHub connector. No GitHub mutation was attempted.
- NOT RUN: hosted CI, installed extension UI, Node 22, Windows, release or deployment acceptance.

## Publication and retained files

This document is draft PR text and local evidence. Owner authorization is still
needed to push or open the draft PR. No merge, tag, release or deployment was
attempted. Task scratch, synthetic Rooms and test logs are removed after the
sanitized evidence is recorded; no clone, new worktree, environment or container
was created. The task worktree and implementation files are retained.

Canonical onboarding module SHA-256: `ccd8465f2d8cfe5894f00c1ac461c2d7916dc398c990f79c8c9345984a3723a4`.
