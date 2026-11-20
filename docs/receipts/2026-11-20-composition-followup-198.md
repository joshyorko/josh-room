# Composition preview and offline evidence follow-up

Issue: https://github.com/joshyorko/josh-room/issues/198

Base: `b1a65fcd8fea233e7f1d3c666432c042ab75cdb4`, including merged #199 and #200.
Code checkpoints: `0ba6dd46c06c81fc4e8712e5b43384cec8026368`, then
`3b093ff3ad882cd852e16cda4e062f41f5c03246`.

## Scope and review

Sol reproduced and fixed reserved composition metadata offered as selectable
children, lexical parent-path normalization, Win32 trailing-dot/space aliases,
null logical origins, contradictory ciphertext evidence, explicit parent-lineage
conflicts, invalid timezone offsets, and native selection correction/empty-state
feedback. Reserved child boundaries now stop discovery before their descendants.
No performance speedup is claimed.

GPT-6.2 Astra, high reasoning, reviewed the merged source and exact follow-up
checkpoints read-only. Its initial request-changes findings were reproduced.
Its patch review found timestamp validation missing from logical records and two
notification behaviors hidden by the mocks. Those were corrected with failing
regressions before the fixes. Astra approved code checkpoint `3b093ff`, independently
passing 93 Python tests, 41 subtests, and seven Node composition tests and checking
source/mirror parity. This is source and harness review, not installed UI proof.

## Verification

Commands run from the task-owned Linux checkout with Python 3.12.11, installed
project/dev dependencies, Node 26.13.1, checksum-verified Restic 0.19.1 and age 1.2.1.
The pinned binaries, environment, configuration, state and tool caches live under
`/tmp`; no host package or workstation configuration was changed.

- PASS `/tmp/josh-room-198-venv/bin/python -m pytest -q tests/test_composition.py tests/test_composition_safety.py`:
  93 passed, 41 subtests passed at the approved code checkpoint.
- PASS `node --test vscode-extension/*.test.js scripts/verify_node_version.test.js`:
  376 passed, four skipped under existing environment gates. Task Python and
  pinned Restic were on PATH. Managed extension runtime acceptance remains unrun.
- PASS `node --experimental-strip-types --test omp-extension/tests/*.test.mjs`:
  49 passed.
- PASS `/tmp/josh-room-198-venv/bin/ruff check src tests`.
- PASS `python scripts/sync_controller_sources.py`: final check changes zero
  files. Canonical extension JS also matches its template copy.
- PASS `python scripts/request_release.py validate --root . --branch fix/198-composition-validation`
  and `git diff --check`.
- PASS focused public-storage credential scanning and controller-artifact checks
  in `tests/test_public_storage_safety.py` and `tests/test_controller_artifact.py`.

Initial failures are retained: the baseline encountered host Restic 0.20.0 rather
than the pinned version; a full Node attempt used system Python without botocore;
sandboxed full suites needed loopback/npm access. A full Python attempt reported
22 failures, 2,201 passes and 76 skips with missing crypto tools, subprocess import
paths and inherited configuration. After isolating config and supplying pinned
crypto tools, a second run reported 14 failures, 2,276 passes and 10 skips because
the project itself was not installed in the task environment. These are failed
runs, not passing qualification. The environment was corrected with repo-native
`uv sync --group dev --no-managed-python` before further verification.
All 14 failed bridge cases then passed with
`python -m pytest -q tests/test_room_store_bridge.py --lf` under the corrected
environment; 31 other cases were deselected in that focused rerun.

## Actual CLI demonstration

Synthetic sibling Git boundaries A/B/C, including an 8 GiB sparse child fixture:
the actual `python -m josh_room composition plan --workspace <synthetic-root>
--child=a --child=c --json` selects only A/C, reports zero payload bytes read,
preserves the original ignore file, creates no manifest, and keeps B under its
existing policy. No child payload was uploaded.

The actual `composition reconcile --evidence-file <synthetic-evidence> --json`
accepts internally consistent evidence only as `offline-untrusted-input` with
apply/resume disabled. Removing the pinned child's inventory entry returns a
nonzero exit and one missing snapshot. It does not silently select latest.

This is preview and offline evidence, not a parent capture or selective child
hydration demonstration. Publication, retention fencing, crash-consistent split,
child Enter and unload remain unimplemented. Issue #198 remains open. Native
RCC/JAT/Hauler capture, installed VSIX, live R2/MinIO/S3 and Windows/macOS
qualification are SKIPPED or NOT RUN; no private workspace or provider was used.
