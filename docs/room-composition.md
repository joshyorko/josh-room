# Composable Rooms: preview contract

Issue #198 PR A currently supplies a **read-only contract and planner**, not a
working split, composed recovery, child Enter, or unload. Existing Save/Enter
contracts remain in force for ordinary Rooms. Save now refuses reserved
composition metadata and split guards, even when user-excluded. Do not create
parent exclusions from a proposal.

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

## Hard gates before enabling split publication

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
