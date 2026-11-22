# Composable Rooms: explicit namespace capture and pinned entry

Ordinary full-Room Save remains the default. The explicit catalog mode now has
separate CLI capture and entry operations backed by age-encrypted namespace
state and an isolated Restic repository. Full-Room Save refuses namespace
metadata; guarded children use the dedicated namespace Save path. Namespace
content cannot fall through to the legacy writer. No unload or migration of
existing Rooms is supported. The discovery previews below remain read-only.

## Writable capture: Linux, one initial approved capture per namespace

Use an already initialized MinIO/S3/R2 Dimension with a configured encryption
domain and root provider endpoint (no endpoint path aliases). Generate a fresh
32-character lowercase UUID hex namespace locally. It is a public locator, not
an authorization token. Provider credentials and age identities still come from
the existing selected-Dimension enrollment/keyring flow. No login or bucket
administration is added.

```sh
josh-room composition capture-plan --dimension synthetic-dimension \
  --namespace 11111111111111111111111111111111 --parent-room synthetic-parent \
  --workspace /synthetic/source --assign .=local-only \
  --assign projects/service-a=child --assign notes.txt=parent --json
```

If the configured Dimension has no cached encryption domain, add
`--expected-domain <domain-UUID-from-enrollment-receipt>` to both planning and
Save. This is non-secret; the runtime corroborates it against authenticated
provider material after approval.

Review the returned request and `approval_sha256`. Repeat those exact arguments
with `composition save`, adding `--approve <approval_sha256>` and
`--ack-local-only`. Every eligible path needs an explicit subtree assignment:
`child`, `parent`, or `local-only`. There is no implicit capture of unchecked
content. Overlapping non-root assignments are currently rejected, including a
parent override beneath a local-only subtree. The explicit root assignment can
set the reviewed default for all remaining paths. Existing exclusions remain
exclusions; child allowlists also apply inherited parent exclusions. Invalid or
stale approval fails closed. Approval binds namespace, source/policy/child
inventory metadata, provider, connection, physical bucket and encryption domain.

This first vertical is **source-only for both parent and children**: Git history
is intentionally excluded. It captures one to 32 independent whole children in
the unchanged sealed v1 manifest, not thousands of publications. Linked sources,
unsupported layouts, existing bound Room conversion and optional RCC/Homebrew/
Hauler components are not admitted. Source files and original ignore rules are
never edited or removed. Private allowlisted capture copies are made outside the
source; this trades additional scratch space and reads for explicit isolation.
No constant-space or large-workspace performance claim is made.

Children are independently saved Logical JATs. The parent captures only its
canonical pinned manifest, generated source-only policy, empty ancestor
directories for nested child paths, and selected parent-owned content. Before
parent commit, the actual Restic snapshot is restored privately to verify exact
metadata bytes and zero child entries. No exclusion is installed in the source.

The returned receipt contains the namespace and exact parent recovery point.
Restore that parent into a new absent destination, then enter one whole child:

```sh
josh-room composition restore-parent --dimension synthetic-dimension \
  --namespace 11111111111111111111111111111111 --parent-room synthetic-parent \
  --parent-snapshot <exact-parent-logical-jat> --destination /synthetic/new-parent --json
josh-room composition enter-child --dimension synthetic-dimension \
  --namespace 11111111111111111111111111111111 --parent-room synthetic-parent \
  --parent-snapshot <exact-parent-logical-jat> --workspace /synthetic/new-parent \
  --child-path projects/service-a --json
```

Entry authenticates the namespace, committed parent pin and descriptor, restores
the immutable parent manifest privately, compares its digest and local bytes,
requires a clean path-bound parent marker and parent exclusion, then restores
the exact pinned child through existing private staging/no-replace promotion.
It verifies the final child's marker and policy/signature. A matching clean
materialization can be reopened; dirty/differently bound content or links are
refused. `latest` is never a fallback. A versioned v4 path-bound marker retains namespace membership even if a guard
is removed; clean reopen additionally checks the exact local guard. A split guard prevents ordinary Save
from writing an entered child into the legacy store. Save in a guarded child
uses its dedicated namespace writer. Updating that child never implicitly
updates a parent's composition pin. The additive
[child Save and explicit parent pin refresh](composition-updates.md) extend a
saved namespace while preserving all historical pins. No automatic unload,
activation/terminal hydration, or selected-file restore is added.

Native commands are `Josh: Save Catalog Room`, `Josh: Enter Child Room`,
`Josh: Save Child Room`, and `Josh: Refresh Catalog Child Pins`.
New Room offers Save one Room first, or explicit Catalog configuration. Catalog
configuration selects a source, child folders and optional parent-owned
files/folders with native dialogs, then shows the source-only/local-only review.
Cancel before confirmation never invokes provider Save. Entry asks for the
namespace receipt and exact relative child path; it opens the folder only after
verified entry. Installed VSIX qualification remains separate from Node fixtures.

## Retention, namespace isolation and interruption limits

All authority state and encrypted descriptors live below
`composition/v1/<namespace>/`; payloads use a distinct
`composition/v1/<namespace>/payload-restic-v1` repository and fresh secret.
The encrypted reservation is create-only and read back before Restic init;
verified repository identity is bound before capture. No legacy catalog,
descriptor, keyset or Restic repository is migrated or rewritten. There is no
namespace delete, forget, Optimize, unpin, copy/move or garbage-collection API.
Historical records/pins are retained. Existing supported legacy cleanup only
addresses root object keys and the root `room-store/v1` repository, so an
in-flight legacy Delete or Optimize cannot address the new namespace at the
unchanged physical endpoint/bucket. Raw administrative deletion, bucket lifecycle
rules and differently configured endpoint/proxy aliases are outside this
guarantee. This is namespace isolation, not an exclusive-principal claim.

Resume Save with the same namespace and unchanged approval. Slot IDs are durable;
child record plus pin, and parent record plus committed phase, use single CAS
updates with exact encrypted-state readback. A slot is claimed by CAS before
backup to prevent two owners starting it. An interrupted single tagged snapshot
is privately restored and content-compared with the approved capture view before
reuse. Multiple matches, changed source/policy, conflicting CAS, or a claim with
no completed snapshot stop without deleting any data. A claim interrupted before
backup currently requires operator reconciliation or a new namespace; automatic
lease takeover is not implemented. An already committed exact operation is
idempotent. Never remove a source guard or delete snapshots to force a retry.

Real Restic/age tests with synthetic SQLite S3 transport establish encrypted
capture and restore logic, not a real cloud-provider or installed-extension
qualification. Report loopback MinIO, Windows, installed VSIX and remote R2/S3
evidence separately. v1.0.0 remains on OWNER HOLD. See the
[synthetic Linux acceptance receipt](receipts/issue-198/README.md)
for installed VSIX/source-runtime evidence and the separate unproven gates.

## Read-only safety evidence

`josh-room composition reconcile --evidence-file <synthetic-evidence.json> --json`
validates an explicitly supplied local evidence bundle. It does not contact a
provider, read keys, migrate a catalog, repair state or resume publication. The
bundle contains `state`, `descriptors`, `repository`, `inventory`, `expected_scope`.
Results explicitly say `offline-untrusted-input`, `can_apply: false`, and
`can_resume: false`. A consistent bundle is not a durable recovery guarantee.
Missing referenced Restic data returns `ok: false`, including component data.
Output contains only operation IDs, phase and summary counts, not child paths.
Contradictory claims about one ciphertext object (payload kind, size, or logical
descriptor) are refused. Shared legacy objects and shared Restic snapshots remain
valid when their evidence agrees. This stricter offline check does not alter the
ordinary deletion planner's conservative handling of shared objects. A committed
parent's explicit logical/Restic lineage must agree with its reserved baseline;
absent lineage remains supported when Save receives no Restic parent. Present
origin fields and timezone offsets must satisfy their closed-schema contracts.
Retained committed operations may coexist with one active continuation touching
the same Rooms. An active operation's baseline must match the current parent
head; historical committed parents need not remain latest. Two active operations
cannot overlap, and historical pins cannot be dropped. Missing baselines and
legacy records whose embedded snapshot IDs differ from their catalog slots are
refused with bounded validation errors.

`composition_safety.CompositionSafetyState` validates the closed candidate-v4
envelope in `schemas/composition-safety-v1.schema.json`. It embeds an unchanged
sealed v3 catalog. Exact pins bind reserved Room/logical ID slots to descriptor
ciphertext digests and are reconciled with corroborated descriptors, repository
identity and snapshot/tree inventory. Historical pins never resolve latest.
Caller maps are copied; phase-specific fields, overlap and nesting are checked.
The ordinary catalog decoder continues to reject v4; no production migration or
v4 writer exists. Existing in-flight maintenance cannot be fenced by a new ETag
alone. See [the corrected safety design](composition-safety-design.md) for the
source-backed admission blockers and required crash/concurrency qualification.

The native Save UI, headless create/preview entry points, direct portable/native Save,
Save preview and local no-op receipt path refuse a root `.josh-room-composition.json` or
`.josh-room-split.json` of any type without reading its contents. This is an
unsupported-composition admission check, not a lock against concurrent writers.
Do not delete a guard to work around it; preserve the workspace and investigate
the operation that created it. No code in this increment creates either file.
The shared scanner remains unchanged for restore and export: older snapshots
containing these filenames remain recoverable. Truncated sibling discovery
disables selection below that scope; unseen aliases cannot authorize a selection.

## Explicit preview

Run `Josh: Split Workspace into Rooms (Preview Only)` from the command palette.
Choose the source folder (it need not be the open folder). Discovery builds a
private local SQLite metadata index outside that source. A cancellable native
progress notification shows observed counts. Cancel pauses; Resume continues the
same live controller session. Closing discards the index. **Process loss requires
restarting discovery**; durable cross-process resume is not implemented.

The native picker shows at most 50 directory rows per page, with Next/First,
a visible workspace recommendation, separate suggested-boundary pages,
search across the whole index, and exact relative-path lookup. Select a row to
toggle an explicit choice. Picks survive navigation and filters, and no
recommendation starts selected. Review displays the actual explicit choices in bounded pages, allows removal,
validates every chosen path and ends with
an explicitly read-only message. There is no 32-choice ceiling in this discovery
picker. It does not serialize a composition manifest or authorize capture. The
v1 manifest remains limited to 32 children / 64 KiB; scalable versioned storage
and tri-state capture assignment belong to future PR E.

### Local JSON discovery session

```sh
josh-room composition inventory --workspace /synthetic/workspace --json
```

This foreground command accepts one JSON object per stdin line and flushes one
JSON result per request. Send `{"action":"advance"}` repeatedly until `status`
is `complete`, `partial`, or `stale`. Each advance consumes at most 256 work units
and a 100 ms deadline checked between blocking filesystem calls. Send
`{"action":"cancel"}` to pause; another advance resumes without reopening the
active directory stream. `{"action":"page","after":"","query":""}` returns
up to 50 sorted rows, bounded to 32,000 encoded candidate bytes. Its `next` key
is a bounded session-local row ID cursor for the next page. Browsed choices use
row IDs; long path labels are explicitly marked as truncated display text, never
used as identities. Manual paths are limited to 16,000 UTF-8 bytes per request;
longer eligible paths remain reachable by browse/search and row ID. Search covers the full index,
not just the rendered page. `{"action":"lookup","path":"src/deep/manual"}`
validates a manual choice; `{"action":"lookup","id":"42"}` validates a browsed
choice. Invalid choices return a recoverable request error without discarding
the session. `{"action":"page","suggested":true}` pages repository suggestions. `{"action":"close"}` or EOF cleans up the private
index. The final command receipt says `closed` and `can_apply: false`.

Results carry a random live-session generation, policy digest, observed counts,
omission codes, `payload_bytes_read: 0`, and an explicit observation freshness
label. An iterative, descriptor-relative traversal avoids call-stack recursion,
never follows symlinks or reparse entries, and checks Linux mount IDs as well as
devices (including same-device bind mounts). Unsupported platforms fail closed.
Administrative `.git` internals are not traversed. Git pointers, nested Room markers and
reserved composition metadata stop that branch. An ordinary Room marker at the
selected source root does not hide its directories. All sibling names, including
files and rejected spellings, are indexed before descendants are admitted.
Undecodable byte names are omitted explicitly. Sibling finalization is batched;
an incomplete sibling scan never admits staged descendants, so a
late case/Unicode alias cannot escape the alias check. Excluded directories
remain discoverable with a local-only policy label. Unsupported names and
inaccessible scopes make coverage partial, while completed safe scopes remain
browsable. Complete means the eligible directory metadata observation finished,
not a stable filesystem snapshot, complete file inventory or passing Save gate.

Completed scopes receive a bounded revalidation pass before browsing. Detected
source/policy changes mark the session stale; selected paths additionally recheck
all ancestor identities, sibling-generation timestamps, guards and policy.
Changes after an observation are possible: this is not a filesystem lock or
cryptographic snapshot, and in-place file edits need not change directory stamps.
No payload size or data-isolation claim follows from the index. Regular files
are statted, never opened. Only canonical capture-policy inputs and Linux mount
metadata are read. No Git commands, hooks, provider or authentication reads occur.

Ranking is pure deterministic metadata labeling: one workspace-level cohesive
recommendation for a root Git directory, one candidate per independently observed
nested Git directory, internal ordinary folders retained with their cohesive
repository by default, excluded/generated branches local-only, and ordinary
unknown folders manually selectable. Evidence code, reason, numeric rank,
confidence (`unknown` for unverified ownership), and proposed consequence are
included. Git-directory evidence remains **unverified**; validating Git storage
requires the separate Save gate. Dataset-specific size heuristics are not yet
implemented. Recommendations never select, exclude or save anything.

The old `composition plan --workspace ... --child ... --json` contract remains
available for compatibility with prior previews. Its bounded boundary discovery
and 32-child preview limit are unchanged; it is **not** the exhaustive inventory
API and is no longer used by the native picker. Ordinary New Room and Save
semantics are unchanged. No Room ID, exclusion, manifest or provider data is
created by either preview path. Never publish private discovery responses.

The native integration uses the existing managed controller receipt lifecycle
and packaged controller cwd; installed VS Code and RCC stdin forwarding remain
separate qualification gates. Fixture UI tests do not establish installed UI
behavior. A process killed before cleanup can leave its temporary index behind;
this first vertical has no durable resume or crash cleanup service.

## Closed portable manifest

`schemas/room-composition-v1.schema.json` describes the proposed parent file
`.josh-room-composition.json`. `composition.parse_manifest` additionally rejects
duplicate JSON keys, oversized input, unsafe relative paths, overlapping paths,
case/Unicode aliases including shared ancestors, Windows reserved names, glob
syntax, control paths and duplicate Room identities. Call both structural and
semantic validation when consuming a manifest. No existing catalog, descriptor
or marker schema is extended.

Each entry requires `path`, `provider` (R2/MinIO/S3), opaque `connection_id`,
`dimension_id`, `room_id`, immutable logical `snapshot_id`, and
`hydration: explicit`. Dotted identifiers are supported, matching catalog and
logical-JAT identities. `latest` is forbidden as a snapshot selector. A connection ID is a lookup hint,
not a credential profile or authorization. No absolute paths, URLs, credentials,
contents or nested catalogs belong here. This file is ordinary encrypted parent
workspace content once publication is safely supported; **schema validation does
not authorize any remote operation**. `.josh-room.json` stays a local path-bound
marker, never a portable pointer. No child stub is written in this patch.

Exclusions use the existing grammar: `service-a/**` is root-anchored and excludes
that directory and descendants; it does not exclude `other/service-a`. The
planner validates that each portable path can compile into its literal exclusion,
including the capture grammar's segment limit. It does not rewrite `.josh-roomignore`, compute a future policy digest, or
claim every retained file was enumerated. Existing policy may already exclude
unselected files. No file is removed or migrated.

## Bounded formal discovery check

The [discovery verification](../verification/discovery/README.md) checks sibling
admission with TLA+/TLC and bounded generated schedules against the real scanner.
Use the project-local `verify-discovery-admission` skill when changing admission,
alias handling, iterator failures or pagination. This checks a finite scope; it
does not prove arbitrary filesystem safety or any Save/publication behavior.

## Shared legacy repository migration remains blocked

### Synthetic transaction qualification

`composition_transaction` now implements candidate-v4 metadata transitions:
reserve operation and logical slots, record a child and its pin atomically,
prepare canonical manifest/policy digests, then record the parent and committed
phase atomically. An operation ID cannot change its approved plan, recorded
outputs are immutable, and retries do not allocate new slots. The manifest uses
the exact reserved child JAT, never the current latest JAT.

`persist_candidate` qualifies those transitions against an injected synthetic CAS
store. It checks the paired body/ETag baseline and reads back the entire desired
state after a write or uncertain reply. Process loss propagates; reopening and
repeating first checks for the exact desired state. A conflicting or unconfirmed
outcome stops rather than blindly repeating a write. Tests use durable SQLite
commits and process-loss injection before/after every phase, not a live provider.

This metadata module itself has **no production adapter or CLI write command**. Its `committed`
phase describes candidate metadata only, not a verified saved parent. It does
not capture payloads, inspect an immutable parent tree, prove Restic reachability,
establish an exclusive authority, migrate catalogs or modify owner files. The
ordinary Save guard remains in place. The explicit namespace lifecycle above
supplies separate capture/entry commands; installed UX remains independently
qualified.

Independent Astra design review reconfirmed the shared-repository migration blocker on 2026-11-22:
a new Dimension/connection/catalog key does not establish physical repository
isolation. An independently provisioned physical bucket with exclusive writer
access from creation is a possible prerequisite, but current adapters cannot
verify that condition. An arbitrary local `retention-locked` flag is insufficient.
Existing v3 writers already past CAS can still delete shared descriptor objects or
Restic data. A later read-only Astra review approved the fresh, physically
prefixed namespace design above for unchanged endpoints/buckets. No provider
write is authorized merely by synthetic transaction test results.

1. **Historical pin retention.** `room_store_references.build_reference_index`
   indexes catalog records and logical descriptor references, not manifests in
   workspace snapshots. `remove_logical_catalog_records`, reconcile and Optimize
   cannot preserve a logical child pin referenced only by a parent file. Even
   retained Restic bytes do not preserve its catalog identity. Define a versioned,
   authenticated reference mechanism at the owning Dimension authority with CAS,
   publication/deletion serialization, historical parent reachability and older
   client enforcement. Cross-Dimension pins remain blocked until their own
   authority enforces retention. Missing pins must visibly fail, never use latest.
   No new GC or relaxed sealed schema is included here.
2. **Crash-consistent split publication.** Bind approval to source identity,
   policy bytes/digest, target Dimension, child identities, parent revision and
   verified committed child pins. Persist a resumable operation outside captured
   sources. Reconcile uncertain child commits before retry; never delete committed
   remote children as rollback. Only introduce an exclusion together with its
   corresponding protected pin/manifest, while making ordinary Save safe or
   blocked across every interruption. Recheck policy and source before commit.
   The existing sequential interactive `saveRoom` flow is not this transaction.
3. **Capture isolation.** Run existing complete `workspace-check`, Git validators,
   policy compiler/digest, Restic exclusion resolver and verified Save receipts
   at the capture boundary, not as an unbounded discovery pass. Verify zero child
   payload entries in the parent snapshot. Parent Git history/index may retain
   child bytes even after live exclusions. The preview always reports this as
   unproven; ordinary `.git` evidence is not proof of separation. Block unsafe
   layouts or offer separately approved source-only mode with honest history
   loss. Never rewrite Git or silently change capture mode.

## PR B/C prerequisites and overlap

PR B must corroborate exact pins against authenticated inventories and explicit
per-Dimension authority. Begin hydration with an absent destination and preserve
Restic's private staging and no-clobber contract. In-place stub replacement needs
exact identity/type/content checks, sibling staging, atomic no-replace promotion,
rollback and final-path marker binding. Loaded/changed/unavailable must be honest;
child changes must not dirty the excluded parent but must remain tracked by the
active child. Unload needs a fresh clean/saved/recoverable proof plus an exact
confirmed deletion inventory; it cannot claim secure erasure.

PR C waits for manual child Enter qualification. Any shell integration remains
opt-in suggestion only, without automatic restore, authentication or probing.

Issue #51 owns incremental Room Store; #107 owns native navigation; #145's
fail-closed workspace checks and #146's explicit source-only mode remain intact.
#189's onboarding pass reads documentation and is not a child inventory, so this
planner is separate. #190 selects Save **components**, not child directories;
this patch does not add a competing component picker or alter Save. #188 owns
agent session recovery and remains out of scope.
