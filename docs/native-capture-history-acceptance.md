# Immutable native capture-history acceptance

2026-11-22, Linux. Dependent draft #219 implements the
[v2 history contract](native-capture-history-contract.md) on top of unchanged
#210. Source checkpoint `dff2135a38898169052aeebb558fc0606dc8c37a` adds capture
history without changing the logical JAT schema, Save selectors or workspace
recovery-point count.

## Source and fixture evidence

| Gate | Observed result |
| --- | --- |
| Same-point native Claude cycle | PASS, `tests/test_native_claude_history_cycle.py`: 1 passed in 1.40s. Real age and pinned CLI `2.1.310`; original native UUID turn through an isolated deterministic loopback backend, all 14 rows recaptured under a second ID, second fresh-profile cold restore byte-identical. First history remains available. Synthetic MemoryStore/admission, not live custody. |
| Immutable history/lifecycle | PASS, `tests/test_native_capture_history.py`: 35 cases. Same-point append, A → B → A, zero-write identical recapture, explicit ID selection, v1 compatibility/promotion restrictions, hydrated baseline, content/mode mismatch, copy/export/removal/reachability, pending receipts, lost responses/cancellation and deterministic no-op closure/revocation races. Real age, fixture storage/authority. |
| Existing reference/CLI contracts | PASS, 79 cases across `tests/test_native_agent_reference.py`, `tests/test_native_agent_reference_cli.py`, `tests/test_native_agents_cli.py`. Exact assertions retained with intended explicit-ID/version changes. |
| Native restore UI | PASS, full Node suite: 396 passed, 2 existing skips. Historical picker/receipt identity, cancellation/stale selections, unsupported contracts and numeric zero-byte no-op. This is Node execution, not installed workbench evidence. |
| Independent history review | SCOPED PASS; no actionable finding. Independently ran 96 workspace/reference/CLI cases and the then-current 30 history cases. Reviewed authority/CAS/removal, legacy promotion and descriptor-bound workspace hashing. No live provider/native/workbench rerun. |
| Canonical/shipped source | PASS, five changed/new modules synchronized into both controller mirrors; extension/template parity, Ruff and whitespace checks. |
| Full Python with the pinned runtime | PASS, verified Restic `0.19.1` prefixed to PATH: 2923 passed, 58 skipped, 47 subtests passed in 168.37s. All 11 earlier failures cleared; optional skips remain skips. |
| Hosted CI at `dff2135` | PASS, [test/private-runtime/managed-consumer workflow](https://github.com/joshyorko/josh-room/actions/runs/45055323650) and [Linux/Windows runtime qualification](https://github.com/joshyorko/josh-room/actions/runs/45055323613). Both platform qualification and consumer jobs succeeded; release-candidate/promotion were skipped. This is distinct from native Windows agent recovery, which remains unqualified. |

Reviewed `native_agent_reference.py` SHA256 is
`921e652d62f6eeb1035b99174c11865760b73aa1f9c91b0d7762987dcc5e58b3`;
`native_workspace_content.py` is
`43e60a543771171e3e7537c41d1cc78a7203022908f93bbdb3ec5767cff55cfe`.
Fingerprinting uses descriptor-bound no-follow reads with mutation/attachment
checks and repeated pre-publication content checks; it is not an atomic filesystem
snapshot. Native history capture retains its existing kernel lease/fence boundary.

A final native-cycle rerun after correcting capability reason strings passed in
1.37s. The core/helper hashes above stayed unchanged; `native_agents.py` SHA256
was `d419fbb76e610e281c146e7f2778af66fba1b070703d7edda32f21318a93489e`.
The capability distinguishes unqualified hosted continuation/general restore
from the observed local synthetic turns; all availability flags remain false.

## Fresh real MinIO and native custody

PASS, `tests/test_native_capture_history_live.py`: one real-provider workflow in
130.55s, with native Secret Service custody and the actual controller CLI.
Codex/Claude each retained three immutable captures at one unchanged workspace
point. Every historical ID verified/restored exact bytes. These appended turns
were synthetic native files, not model calls; the actual Claude turn is qualified
separately above.

Real botocore observation found zero mutating calls for identical recapture;
sidecar/catalog ETags and object inventory stayed unchanged. Normal Git Room
hydration produced new inode/ctime and a clean changed-stat marker, preserving
bytes/modes and the authenticated content digest. Hydrated no-ops and a third
capture succeeded without restamping a marker or creating phantom points.

All six companion payloads decoded exactly. Normal Copy preserved all six IDs
with independent ciphertext objects. Source removal deleted its six objects
while copied histories still verified; copy removal deleted the other six.
All twelve were independently absent by S3 HEAD/404. No existing user/provider
authentication or model/paid call occurred. The first run failed a test-only dictionary/list inspection assumption;
the corrected test passed with unchanged product code. Fresh service setup also
required correcting a scratch-only D-Bus activation/readiness race through normal
unlock/start; native custody/security requirements were not relaxed.

## Registered installed VSIX

PASS, actual signed-out task-private Code `1.148.0-insider`, private Xvfb/D-Bus,
fresh real MinIO/native custody, and registered commands for both providers.
VSIX SHA256:
`41b792966c2c698924ec3d7a99e3f424326629dcd821e1fae4aa09220bb8e400`.
All 103 recursive controller files, extension/runtime source and runtime manifest
matched source/package/install; the installed public manifest matched except
VS Code's added `__metadata`.

The existing installed-controller `local-build-fallback` source override was used.
A task Python observer retained real CLI stdout/arguments without replacing
provider, catalog or prompt behavior. This is not immutable RCC acceptance.

Both providers completed registered A capture, numeric-zero/same-ID no-op,
B capture at the same Room point and status with both entries. Complete remote
key/ETag/size inventories were equal before/after each no-op. After normal
same-cwd cold Room hydration and source-profile deletion, actual native pickers
selected A and B separately, verified full capture IDs and restored four fresh
profiles with exact original UUID/cwd/bytes. Claude B retained all 14 approved
synthetic fixture rows; no model turn occurred in this installed gate. The actual
native Claude turn is qualified separately above.

Twelve registered callbacks succeeded. The four no-model installed receipt
assertions passed, with the two separate native-turn cases explicitly skipped;
the parent independently reran those assertions: 4 passed, 2 skipped in 0.12s.
Six default opt-out cases skip without a private receipt. Ruff/whitespace passed.
One setup locator was corrected in the scratch driver; no product guard changed.
All owned Code/Xvfb processes were stopped and verified absent.

## Environment and remaining gates

The first full Python invocation found Homebrew Restic `0.20.0` on PATH while this
branch pins `0.19.1`: **FAIL, 11 failed, 2912 passed, 54 skipped, 47 subtests passed**.
The retained failures are real Restic version mismatches or repository-initialize
failures using that binary. The pinned-runtime rerun above cleared those failures.
Real-provider, registered installed and native-turn checks above remain distinct;
none is inferred from package parity or from a different gate's fixture run.

Production capability remains `native_resume_verified: false`,
`selectable: false`, `save_integration: false`. Hosted/account compatibility,
unknown/delta Claude traces, original-T3 identity recovery, offline rebind after
point removal, non-MinIO live backends, Windows and immutable RCC remain open.
No existing user/provider authentication, hosted/paid calls, feature-branch
integration, main merge or release action occurred. The owner release hold remains.
