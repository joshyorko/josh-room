# Shared capture-integrity review corrections

Draft #216 includes the shared corrections requested during combined review of
#218 at `dbc1be230fd240f426fc4c61680a350d78f81667`. The shared product backport is
`10f37708111b64593ad4d9ad60cf67ba39a99435`; nested-pattern tests are included at
`36eea29a7a51673d555371483847dd26e441d16f`. Later receipt-only commits do not alter
these product bytes. Release remains on owner HOLD.

Child policies preserve rebased parent glob semantics, including future-created
excluded paths. Payload hashes frame path, type, mode, byte length and file bytes.
Tagged capture recovery requires authenticated persisted Restic added-byte counts
and exact actual ancestry before descriptor publication. Missing/invalid counts
and wrong parents refuse publication rather than inventing zeroes or lineage.
Existing descriptors and snapshot history are not rewritten. Older saved child
policies retain their original bytes; review any missing rules before further Save.

PASS in this #216 checkout: `python -m pytest tests/test_composition_staging.py
 tests/test_restic_store.py -q`: **118 passed**. Includes future-rule and framing
reproducers, nested glob-language equivalence, wrong-parent refusal, and missing/
invalid/nonzero persisted-accounting adapter checks.

PASS with pinned Restic **0.19.1**, age **1.3.2**, Python **3.12.14**:
`python -m pytest tests/test_composition_live_tools.py -q -k process_loss_retry`:
**4 passed, 6 deselected in 87.53s**. Bootstrap, child/parent snapshot and parent
commit interruptions preserve exact slots, snapshot counts and nonzero added-byte
accounting. SQLite supplies only synthetic S3 object transport; Restic and age
are real. This is not live-provider or installed-UI acceptance.

#218 separately qualifies child update/no-op/refresh regressions and installed
controller execution. See its `docs/receipts/issue-198-updates/review-fixes.md`.
Its installed VSIX does not qualify a separate #216 package. The earlier #216
installed/provider receipt remains evidence only for its older named source.

Ruff, whitespace and both controller mirrors are checked together. NEW independent
Astra review is required. Corrected-source native UI, managed immutable RCC,
Windows composition, live-provider rerun, large workspaces and unload remain
NOT RUN/UNPROVEN. No merge, rebase, tag or release staging/publication occurred.
