# Issue 198 PR A: contract/planner checkpoint

Base: `88f18e4b9d0eeab76c3a8c14fd82ac783521d184`.
Initial pushed checkpoint: `f7d23474c7f06ec589d6a773866ac20ff7e8b83d`.
Draft: https://github.com/joshyorko/josh-room/pull/199.
Linux synthetic fixtures only; no live credentials or private workspace access.

## Evidence

- PASS baseline: `uv run --group dev python -m pytest -q tests/test_workspace_policy.py tests/test_git_workspace.py tests/test_dimension_schemas.py`: 55 passed.
- PASS final affected regression batch: `uv run --group dev python -m pytest -q tests/test_composition.py tests/test_workspace_policy.py tests/test_git_workspace.py tests/test_dimension_schemas.py tests/test_cli_contract.py tests/test_onboarding_discovery.py tests/test_public_storage_safety.py tests/test_real_vertical.py tests/test_controller_artifact.py tests/test_source_only_capture.py`: 232 passed, 1 skipped. This includes repository public-artifact credential scanning and controller packaging checks.
- PASS final added name-safety cases: `uv run --group dev python -m pytest -q tests/test_composition.py`: 48 passed (overlaps the batch above).
- PASS `node --test vscode-extension/*.test.js`: 367 passed, 2 skipped. Includes native checkbox selection A/C, chosen noncurrent source, initially unchecked state, trust/cancel gates, trusted controller cwd, no SecretStorage reads, inherited credential-pointer removal and redacted arguments.
- PASS `uv run --group dev ruff check src/josh_room/composition.py src/josh_room/workspace_policy.py tests/test_composition.py`.
- PASS `python scripts/sync_controller_sources.py`, byte parity of canonical extension JS/package/README and both controller mirrors, JSON schema parsing, and `git diff --check`.
- PASS actual `PYTHONPATH=src .venv/bin/python -m josh_room composition plan --workspace <synthetic-root> --child=a --child=c --json`: A/C proposals only, `can_apply=false`, `writes_performed=false`, zero payload bytes read; no source files created. B remains under its existing parent policy. This is a planner demonstration, not parent capture or hydration evidence.
- PASS synthetic 8 GiB sparse fixture: stat-only large-file observation and zero payload reads. No upload/time benchmark or installed UI claim.

Initial failures retained: system Python lacked pytest; uv needed ordinary tool-cache access outside the filesystem sandbox. Initial native test had a missing mock status-item lifecycle dependency. During review fixes, an overbroad assertion replacement briefly broke unrelated tests; it was reverted and the final full suite passed. No production behavior was changed to accommodate these harness failures.

## Astra review outcome

Both reviews used read-only Codex GPT-6.2 Astra, high reasoning, separate T3
checkouts. Astra made no implementation changes.

Design review approved the contract/planner fallback and blocked publication on
pin retention and crash-consistent orchestration. Diff review requested changes
on `f7d2347`. Sol addressed all eight findings:

1. Launch from packaged controller directory, not selected source (mocked native launch evidence; real managed import qualification remains unrun).
2. Refuse names that cannot survive existing diagnostic serialization without changing identity; preserve ordinary single spaces.
3. Stat Git/Room boundary names independently of sampled entries, without reading contents.
4. Read/recheck policy relative to the held root descriptor, tested across root pathname replacement.
5. Block sibling Unicode aliases before descent and Windows reserved device variants.
6. Bound candidate output with explicit partial coverage; derive exit status from the bounded emitted result.
7. Pass `--child=<path>` for leading-dash names.
8. Use auth-free credential handoff and remove inherited provider credential pointers.

Added synthetic reproductions cover each failure. Astra has not independently
re-reviewed the fixes; its original diff verdict was request-changes, not final
approval. The PR remains draft.

## Unverified and blocked

SKIPPED real RCC/JAT/Hauler/age vertical: full required toolchain unavailable
(age alone is installed). Two Node managed-runtime integration cases are skipped
by their existing environment gates. Installed VSIX UI, managed RCC import
resolution, Windows/macOS runtime qualification and live R2/MinIO/S3 are NOT RUN.

Child publication, exclusions, parent capture, selective hydration and unload are
not implemented. A parent file cannot yet protect logical child pins from
deletion; sequential policy/manifest/Save updates have an unsafe crash window.
Git-history isolation remains unproven. PR B/C prerequisites and compatibility
decisions are recorded in `docs/room-composition.md`. No merge, tag or release.
