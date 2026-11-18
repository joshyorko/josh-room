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

The native Save UI, headless create entry point, direct portable/native Save,
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
Choose the source folder (it need not be the open folder), then check desired
child directories in the native picker. Every checkbox starts unchecked.
Unchecked directories keep their existing parent capture policy. The final
dialog shows selected paths and proposed literal exclusions locally, then ends.
There is no apply button. Cancel does not change workspace files or contact a
storage provider. Workspace Trust is required before controller acquisition.
The managed controller may require normal runtime installation and temporary
local process receipts; the planner does not read provider credentials or keys.

Headless equivalent:

```sh
josh-room composition plan --workspace /synthetic/workspace --child service-a --child service-c --json
```

This returns `status: preview-only`, `can_apply: false`, coverage, candidates,
selected paths, proposed exclusions, the current capture-policy digest and
blocker codes. It never creates Room IDs or pretends a destination is authorized.
Paths are local interactive metadata: do not publish results from private trees.
The extension redacts composition arguments from Output logs.

Discovery enumerates at most 128 directories, samples at most 257 entries per
directory (256 admitted plus one overflow sentinel), and descends at most three
levels. It checks a two-second deadline between directories; blocking filesystem
calls are not interruptible. Payload contents, Git config/index/objects, scripts,
and marker contents are never read. Only the existing bounded capture-policy
file/defaults are read. Large regular files are counted by stat (at least 1 GiB),
without classification of their contents. Symlinks, reparse points, device
crossings, special entries, inaccessible paths and limits make coverage partial.
Excluded directories are pruned with the canonical capture policy. Candidates
inside Git/Room boundaries are deliberately not enumerated. `complete` describes
this boundary pass only, **not** an exhaustive inventory or passing workspace
validation. Over-limit sampling is filesystem-order-dependent.

A `.git` directory is an unverified repository candidate; a gitfile is an
unsupported pointer boundary. A local Room marker is unverified and unselectable.
Neither is read, executed or interpreted as authority. Discovery currently
requires descriptor-relative, no-follow directory operations; unsupported
platforms fail closed (Windows qualification remains blocked).

Fixed Git/Room boundary names are statted independently of the entry sample.
Policy reads and rechecks use the held root descriptor, even if its original
pathname is replaced. Unicode sibling aliases are blocked before descent,
including aliases whose spelling is otherwise unsupported. Candidate JSON is
bounded to 16 KiB with an explicit `output-cap` omission. Names that diagnostics
would normalize or redact (for example repeated spaces or key-shaped strings)
are unsupported and never offered as a different sibling. The native controller
launch uses its packaged controller directory, never the selected source as a
Python import directory, and removes the inherited provider credential pointer.

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
`hydration: explicit`. `latest` is forbidden. A connection ID is a lookup hint,
not a credential profile or authorization. No absolute paths, URLs, credentials,
contents or nested catalogs belong here. This file is ordinary encrypted parent
workspace content once publication is safely supported; **schema validation does
not authorize any remote operation**. `.josh-room.json` stays a local path-bound
marker, never a portable pointer. No child stub is written in this patch.

Exclusions use the existing grammar: `service-a/**` is root-anchored and excludes
that directory and descendants; it does not exclude `other/service-a`. The
planner does not rewrite `.josh-roomignore`, compute a future policy digest, or
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
