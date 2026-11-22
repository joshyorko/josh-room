# Native history in the shared Save chooser

The reviewed bounded adapter is enabled for an explicit **Linux/MinIO source
pilot** on the integration branch. Only independently admitted existing Codex
and Claude profiles can appear; nothing is selected by default. S3, R2, Windows,
macOS, composition and work scopes remain unavailable in the shared chooser.
This enables the forthcoming installed trial; it does not claim its success,
immutable runtime acceptance, general native resume or release readiness.

`native_agent_save.py` orchestrates ordinary Save. The separate
`native_agent_save_producer.py` owns native capture, admission, encrypted durable
attempts, and one combined sidecar publication. Neither changes the closed
workspace/RCC/Homebrew/Hauler descriptor slots. Existing reference copy, export,
removal and reachability retain every immutable history capture.

## Explicit request and review

The shared native QuickPick used by New Room, Save Current and Save Folder As
has Codex HISTORY and Claude HISTORY rows. They require a fresh, versioned
`snapshot agent-save-status ROOM --source PATH --dimension DIMENSION` admission.
Unknown capability versions and unavailable profiles cannot authorize capture.
Selecting a row requires an explicit opaque granted-profile choice. Nothing
selects history by default or persists history opt-in as a component preference.

The optional `--agent-selection` JSON is a separate closed object:

```json
{"format":"josh-room.save-agent-selection.v1","entries":[{"tool":"codex","profile_ref":"<64 lowercase hex>"}]}
```

There may be one profile per tool and at most two tools. The request means
capture current history; it does not select an implicit latest historical
capture, copy authentication, or authorize a native turn.

Preview returns a bounded `josh-room.save-agent-plan.v2` public envelope with
the attempt ID, a reserved 32-hex workspace candidate ID,
target/workspace/policy/authority/content digests, explicit
reviewed point, reserved new-point capture IDs, and exact existing-point
created/reused alternatives. Create receives that **whole envelope** through
`--agent-plan`, the original selection and the component approval token.
The token includes a domain-separated hash of the full envelope only when
agents are selected. No-agent token bytes and component keys remain unchanged.
Normal JSON diagnostic redaction is unchanged.
The unreleased Save plan v1 is rejected rather than migrated. Native history
and sidecar v1 readers remain supported. Ordinary Save still chooses its usual
UUID; only an explicit agent Save supplies the approved reserved workspace ID.
The publisher rejects a reserved ID already present in the current Room
catalog, preventing replacement of an existing immutable point.

## Save and exact retry

Before workspace writes, the producer prepares both selected tools as one
batch, rechecks native inventory and current authority, and retains encrypted
payloads in its granted private reference root. The prepared scope remains
entered through workspace Save and history publication. It does not hold an
imaginary lock across the user's preview approval.

The default-None `before_catalog_cas` publication callback receives the actual
validated descriptor, its verified ciphertext object reference and workspace
signature. The producer must durably record the candidate before the workspace
catalog CAS can execute. Callback failure aborts that CAS; encrypted unreferenced
objects may remain for normal reconciliation. No workspace/history rollback or
destructive cleanup occurs.

History-only differences never force a workspace point. Explicit selection
bypasses both local Save no-op shortcuts; the normal workspace pipeline may
still return the exact existing point. The producer permits only the reviewed
existing alternative or actual journaled new candidate, verifies the committed
descriptor, marker, physical keyset and semantic content, then publishes both
tools through one sidecar CAS and catalog fence. All-noop history reuses exact
reviewed capture IDs and uploads no history data.

An `agent-save-partial` result has `ok:false` and a nonzero CLI outcome. It
contains the verified `workspace_result` when available, `agent_result` entries,
attempt/candidate IDs, cancellation and reconciliation state. Unknown upload
counts are not reported as zero. A tool cannot report created before confirmed
batch publication. A committed workspace remains a valid recovery point even
if history publication fails.

The extension handles this typed rejection before cancellation discards its
receipt. It applies only a workspace result corroborated by the current marker,
Dimension, Room, exact point, canonical workspace, signature and policy. The
warning offers **Retry Exact Attempt**, retaining the original reviewed create
arguments. Retry first reconciles the durable candidate and its reserved IDs;
it cannot choose latest, create a replacement point after an unknown CAS, or
duplicate an already retained history event. If no workspace is corroborated,
the UI remains unknown. Dismissing the warning does not automatically retry.

Reconciliation opens the Room scope without first loading the former reviewed
parent. A later removal of parent A cannot prevent independent corroboration
of the already journaled committed candidate B. Only an attempt that still
needs workspace Save resolves the reviewed parent before preparation. Retry
does not substitute the catalog's current latest point.

A lost workspace-CAS response may leave the local marker at A even after B is
committed. With fresh native authority still admitted, the entered batch
corroborates exact B, approved stat/policy and semantic content before finalizing
that marker. The adapter claims and verifies the expected A marker, then
promotes B with Linux atomic no-replace operations. A newer/foreign marker,
symlink, changed content or revoked authority cannot be overwritten to proceed.
Publication still requires the producer's strict exact-B marker check.

Marker custody is a private, attempt-bound sibling directory named
`.josh-room-marker-retry-<attempt_id>` outside the captured workspace. Its intent
is fsynced before the old marker is claimed. A retry can discover and finish a
crash before/after claim or after promotion. Normal completion removes only its
validated control files; conflicting/uncertain custody is retained for
reconciliation, never recursively deleted. Cross-filesystem or unavailable
no-replace support refuses finalization. Failure never rolls a custody file
back into the live marker namespace. Cleanup first moves each verified control
entry into private anchored cleanup custody, verifies its held inode and bytes,
and deletes only that private claim. Namespace replacements remain untouched;
conflicts remain discoverable under the attempt's custody directory.
Each control entry has one deterministic cleanup child, made durable before
the move. Retry validates and replays those three bounded claims, including an
intent retained inside its cleanup child. A process interruption between claim
and deletion therefore does not lose the intent or require a new Save.

When the encrypted attempt ledger is unavailable, a read-only blocked receipt
can verify that scoped candidate B exists, but cannot attribute its commitment
to this attempt: `workspace_committed:null`, unknown upload counts, and no
successful `workspace_result`. The UI may offer only the original reviewed
retry after checking its candidate, Room/Dimension/domain, current policy and
marker binding; it does not adopt a baseline or claim Saved. Revocation never
repairs the marker or publishes history. Restoring the same grant can recover
the original encrypted ledger and resume normal exact-attempt verification.

Partial-result UI admission reuses the full Save binding validator, including
the encryption domain, signature algorithm and freshly loaded capture policy.
The registered Save command and managed private-result receipt tests cover a
late cancellation request without discarding a typed partial result or its
original attempt ID. Missing/unverified outcomes remain unknown.

## Current limits and verification

Admission requires ordinary Linux MinIO/S3 custody, an already opened/bound
Room Store and an existing exact-Room v3 host grant. New Room does not create a
grant. Missing authority, R2, Windows and composition namespaces are unavailable.
The global native capabilities remain selectable false and Save integration
false. The adapter's separate narrow qualification flag must stay false until
review and actual integrated acceptance qualify it.

Focused checks live in `tests/test_native_agent_save.py`,
`tests/test_native_agent_save_integration.py`,
`vscode-extension/component-picker.test.js` and
`vscode-extension/extension.test.js`. The integration fixtures use real age and
synthetic storage/admission; the explicit pinned-Restic test exercises the
actual bridge hook ordering. They are not a real provider, installed extension,
managed-controller artifact, native continuation or Windows acceptance claim.

For the affected bridge/operations tests, set `JOSH_ROOM_TEST_RESTIC` to the
checked-in installer's verified 0.19.1 binary and prepend its directory to PATH.
Do not replace a host Restic installation to satisfy this gate. Preserve
unavailable-platform/provider tests as skipped. Qualification and release
promotion remain separate; this adapter performs no release action.
