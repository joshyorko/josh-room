# Composition safety implementation sequence

Starting point: draft #199 at `9519ce5ff925d6ade82480428576f3e37a1a7ec7`.
Scope: same-Dimension, explicit source selection, synthetic qualification only.
This is a proposed design for independent review, not permission to use real data.

## Safety boundary

A sidecar by itself cannot protect pins: older catalog-v3 clients could delete
their child records without reading it. Introduce an explicitly admitted catalog
v4 with closed composition state, keeping v3 unchanged. Existing clients reject
v4 at decode. Do not silently migrate a Dimension on ordinary Save or activation.
Use one catalog CAS as the authority for both pin reservations and publication
phases. No global lock, new daemon, GC, or distributed cross-Dimension transaction.

The first v4 implementation conservatively refuses every Room/JAT deletion and
Optimize in a composition-enabled Dimension. Preserve complete logical records,
descriptor objects and Restic snapshots, not merely underlying shared bytes.
Reconcile remains read-only and verifies all recorded pins against exact catalog
records and authenticated descriptors. Missing pins fail; never resolve latest.
There is initially no release/unpin operation. This trades storage reclamation
for an enforceable retention boundary. Direct provider deletion, bucket lifecycle
rules and unsupported external Restic clients remain outside this guarantee.
Cross-Dimension composition, copy/move of composed recovery points and legacy
export remain refused until their reference semantics are separately qualified.

## Publication protocol

The encrypted v4 catalog stores a closed operation record keyed by an opaque
operation ID. A canonical plan digest binds source real-path hash, original
policy digest, explicit parent/child Room identities, same Dimension/domain,
approved capture mode, child relative paths and expected catalog state. IDs are
allocated once; a retry uses the same plan and operation ID. Unknown versions,
changed plan/source/policy, ambiguous remote outcomes and conflicting revisions
stop writes until reconciliation.

1. **Reserve:** explicit opt-in CAS to v4 and operation `reserved`, before any
   child Save or policy edit. This activates the destructive-operation freeze.
   Repeating the same reservation is a no-op; a different digest is rejected.
2. **Save children:** reuse existing workspace-check, Git validation, Restic Save,
   descriptor verification and authoritative receipts. Record each verified exact
   child logical JAT identity through catalog CAS. A receipt alone is insufficient:
   corroborate catalog and descriptor before advancing. Lost/uncertain responses
   require remote inventory reconciliation; do not blindly issue another Save.
   Never delete committed children as rollback.
3. **Prepare parent:** only after all child pins are reserved and verified,
   record intended manifest bytes/digest and exact future capture policy digest.
   Preserve original user policy bytes; append only literal selected exclusions.
   Require explicit source-only approval or prove absence of ancestor Git stores;
   full-repository Git history is not isolated by directory ignore rules.
4. **Guard local edits:** before changing either parent file, create and fsync a
   no-clobber operation guard with identity and expected digests. Normal Save,
   no-op receipts and preflight must reject an incomplete or mismatched split.
   The remote reservation is also checked at parent publication. The manifest
   and policy are individually atomically replaced with compare-before-write;
   recovery recognizes each old/new digest combination. Unknown bytes stop for
   explicit reconciliation. Do not overwrite concurrent owner edits or delete
   any child directories. Removing a local guard does not bypass remote admission.
5. **Publish parent:** reuse existing Save validation and Restic staging with
   exact proposed policy. Require the saved tree to contain the manifest and
   policy, and zero selected-child payload entries. Catalog CAS atomically adds
   the parent logical record, pins its exact children and advances the operation
   to `committed`. A separate later operation cannot rewrite this historical pin.
6. **Reconcile:** after crash/uncertain CAS, load authenticated fresh catalog,
   exact operation and source digests. Distinguish reserved, child committed,
   parent files prepared, parent committed and local cleanup pending. Verify
   remote commit before retry; finish only proven idempotent steps. A committed
   parent with missing local cleanup returns committed-with-cleanup-pending,
   never a fake failure inviting duplicate publication. Preserve receipts and
   committed children across cancellation. No blanket remote rollback.

## Reviewable increments

1. Repair #199's missing command-list expectation in CI; retain earlier evidence.
2. Add v4 schema/validation and explicit admission with destructive operations
   refused. Prove an unchanged old parser rejects the new format. Test exact pins,
   missing/corrupt descriptors, revision conflicts and no downgrade/stripping.
3. Add durable reservation/commit/reconcile transitions with synthetic provider
   CAS and injected crashes before/after each mutation. Test retries, ambiguous
   commits, changed policy/source and concurrent actors. Keep UI apply blocked.
4. Integrate Save admission and local guarded metadata changes; test all partial
   file states plus no-op fast paths. Keep child contents in place.
5. Only after those checks and independent review, connect one same-Dimension
   native split vertical to existing Save and receipts. Do not claim selective
   hydration/unload. Record unmet installed-runtime and platform gates separately.

Fresh read-only Astra design review precedes production implementation; a second
read-only patch review must cover the resulting diff and all earlier eight fixes.
Disk admission must be restored before new reviewer worktrees or heavy tests.
