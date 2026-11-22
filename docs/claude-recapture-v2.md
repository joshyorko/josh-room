# Claude tool-free recapture policy v2

The dependent follow-up to #210 closes the observed Claude `2.1.310` tool-free
post-turn capture failure without dropping or rewriting native rows. New captures
use `josh-room.native-claude-history.v2`; existing v1 archives still decode under
the unchanged legacy policy. Relabeling enriched native rows as v1 is refused.

## Qualified native subset

V2 admits the legacy text history or a completed legacy prefix followed by closed
completed SDK turn groups. Each group validates a drained enqueue/dequeue pair,
the matching text user message, an empty session-context attachment, the exact
canned date attachment, text-only API shape with no tools, structural API-request
metadata, a completed text assistant, matching last-prompt/leaf, normal mode and
closed finite usage/cost state. Native UUID ancestry includes the inert attachments.

Unknown rows/fields, nonempty context/tools, pending queue work, config/header/file
carriers, tool calls, sidechains, branches, incomplete assistants and unobserved
delta API traces remain refused. Opaque request/tail digests are structurally
checked diagnostic data, not authority or reconstructed full-request proofs.
No credential files/settings/hook stores/whole homes are captured.

The pinned official SDK `0.3.310` loader inspection supports ancestry through
attachments and excludes diagnostic rows from conversation selection. Its source
is research evidence, not vendored product code. The independent review checked
the stricter closed policy rather than adopting the SDK's broader permissiveness.

## Actual cycle and compatibility evidence

With real age and the pinned installed CLI in a fresh loopback-only namespace:

1. Capture synthetic native history, cold-materialize it and delete the source profile.
2. Resume the original provider UUID/cwd for one deterministic local-backend turn.
3. Capture the actual complete 14-row output under archive/policy v2.
4. Delete that native profile and cold-restore the recaptured file byte-identically.

`tests/test_native_claude_recapture.py`: **2 passed in 1.44s**, including legacy v1
archive compatibility, original-ID continuation, v2 recapture/second cold restore
and rejection of enriched data relabeled as v1. Host admission in this local native
fixture is synthetic/mocked; it is not new live Room/Secret Service acceptance.
No hosted/paid call or existing provider/user authentication was used.

The first affected parser/native/CLI/reference regression run had **324 passed,
2 explicit optional skips in 4.57s**. Policy mutation tests had **204 passed**;
independent read-only security review returned SCOPED PASS and independently ran
those 204 parser cases. An AST audit confirmed the legacy validator and original
helper bodies unchanged. Ruff/whitespace passed. The review did not rerun native
providers or perform a syscall/filesystem audit.

## Same-point immutable-history cycle

The subsequent [immutable-history contract](native-capture-history-contract.md)
retains both captures at the same logical Room point. The opt-in
`tests/test_native_claude_history_cycle.py` passed **1 case in 1.40s** against
source-bound controller modules and the pinned native CLI. It captures the initial
two rows, deletes the source profile, restores the explicit first capture into a
new authorized profile, runs the original-ID loopback turn, recaptures all 14 rows
under a second immutable ID, and restores that ID into another fresh profile.
The first capture still verifies/decrypts to its original bytes/ciphertext.
Identical recapture returns the second ID with zero remote writes; decrypting the
catalog fence confirms no phantom workspace point. Pending receipts are empty.

This cycle uses real age/native CLI but synthetic storage/admission. It is distinct
from live MinIO/native Secret Service and registered installed-command evidence.
Independent history review found no actionable issue; the 35 history regressions
cover no-op races, legacy migration, limits, copy/export/removal and uncertainty.
Save selectors remain false; #210 stays on its reviewed branch. No feature-branch
integration, main merge or release action is performed.
