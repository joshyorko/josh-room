# History-only Save producer protocol

The optional producer is `josh_room.native_agent_save_producer`; Save orchestration
belongs to `native_agent_save`. It captures only the existing qualified native
history formats. No native CLI/server/model is started, no grant is created, and
the standalone recovery CLI's native-keyring/handoff gate is unchanged. Global
Save availability remains false pending separate adapter qualification.

## Scope and review

`TargetScope(context, workspace, capture_policy_sha256, workspace_signature,
reviewed_context=None)` accepts the real opened ordinary Linux MinIO/S3 backend,
current encryption material and exact resolved Room. Its context may have no
selected record/descriptor for New Room. The optional reviewed context must be an
actual immutable existing point selected by the Save owner. Composition, R2,
Windows, missing keysets, work scope and absent/stale grants fail closed.

Selection is the closed object `josh-room.save-agent-selection.v1` with `entries`
containing one or two `{tool, profile_ref}` objects, unique by tool. Only Codex and
Claude capture-current semantics are admitted. Profile references are opaque
64-hex values resolved against existing v3 host policy and independently admitted
against the physical Dimension/current keyset.

`preview_batch(scope, selection)` returns a bounded public
`josh-room.save-agent-plan.v1` envelope with exactly:

- `attempt_id`, `target_scope_sha256`, `workspace_content_sha256`,
  `capture_policy_sha256`, `workspace_signature`, `digest`;
- `reviewed_point`: null or `{logical_jat_id, binding_sha256, sidecar_sha256}`;
- `entries`: `{tool, profile_ref, content_sha256, authority_sha256,
  new_point_capture_id, existing_point}` for each selected tool;
- `existing_point`: null or `{capture_id, outcome}`, where outcome is `created`
  with a reserved ID or `reused` with the exact reviewed immutable no-op ID.

The envelope contains no paths, raw grants, native text, raw ETags, recipients,
identities or credentials. Its digest hashes canonical JSON without `digest`;
the Save owner's approval token separately binds the full envelope. An absent
sidecar uses SHA256 of canonical `[null,null]`, not a missing digest. New-point
IDs are distinct from the existing-point alternatives, including component-only
new descriptors with unchanged workspace content. No implicit latest selection
or arbitrary revision rebase is permitted. Changed content, sidecar, physical
scope, keyset or admission requires a fresh review.

## Preparation and durable workspace callback

Keep `with prepare_batch(scope, selection, plan) as attempt:` entered through
workspace Save and agent publication. It acquires every unique profile lock and
one combined Linux kernel read barrier, validates all selected native files and
checks current workspace policy/stat/content. `attempt.check()` repeats admission,
inventory, barrier and workspace checks. A second-tool failure occurs before
workspace Save; there is no independent per-tool publication.

Only encrypted `codex.age`, `claude.age` and an atomically replaced/fsynced
`attempt.age` journal are retained under
`<existing-granted-reference-root>/save-attempt-<opaque-id>/`, outside the
workspace/profile/runtime temporary root. Native plaintext remains in bounded
memory. The encrypted journal retains the approved plan, prepared payload object
identities, phases, reserved IDs, actual workspace candidate, pending receipt and
publication proposal; runtime cleanup cannot erase the retry identity.

Before the workspace catalog CAS, the Save owner must call
`attempt.before_workspace_publication(descriptor, object_ref,
workspace_signature)` with the actual `LogicalJat` and uploaded ciphertext
`ObjectRef`. It fsyncs the candidate logical ID, ciphertext identity/size and
descriptor digest before returning. If it fails, the workspace CAS must not run.
It does not fabricate a selected snapshot or publish agent data.

Record `attempt.workspace_outcome(phase, logical_jat_id=...)` with phase
`committed`, `uncertain`, `rejected` or `cancelled`. A committed no-op must name
the exact reviewed point; a new committed result must match the journaled
candidate. Unknown/cancelled attempts retain their phase/candidate. A journaled
absent-but-uncertain candidate is never permission to make a newer workspace point.

## Publication and exact retry

`publish_batch(exact_context, attempt)` independently refreshes the exact result
record/descriptor, physical keyset scope, current native admission and clean
path-bound marker. Semantic workspace content must match the approved plan and
authenticated history baseline. Legacy history without a content baseline needs
original-stat authority; a hydrated marker cannot invent that proof.

All new native objects are tracked before upload. Document capacity is checked
before any upload. The entire selected batch uses one sidecar CAS and catalog
fence; unselected history is preserved. All-noop repeats use the exact reviewed
IDs and make zero remote writes. A → B → A records distinct immutable events;
a new workspace point gets its own events even when native bytes are identical.

The public aggregate uses `entries` with `tool`, `profile_ref`, `capture_id`,
`content_sha256`, `state`, `data_uploaded` and `upload_uncertain`, plus
`attempt_id`, exact `logical_jat_id`, `workspace_committed`, `history_only`,
`native_resume_verified:false` and `reconciliation_required`. No tool is reported
created before successful batch CAS/fence. Workspace commitment is not rolled
back when agents remain prepared/unpublished or uncertain.

Before any retry that might invoke workspace Save, call
`reconcile_attempt(scope, selection, plan)`. It first locates/decrypts the durable
journal through the still-resolvable granted private root and corroborates only
its exact candidate/descriptor. This precedes current native-content rejection:
stale native data or admission must not hide an independently verified committed
workspace or trigger a duplicate Save. A blocked receipt can retain
`workspace_committed:true` and that exact ID without authorizing agent publication.
An unresolvable/revoked selector returns blocked, never absent permission.

Verified matching reserved IDs/payloads reconcile publication uncertainty without
new events. Otherwise retries retain the original proposal/ETag/IDs and reject
changed sidecars. Fresh admission, native content and clean exact marker remain
mandatory before publishing. The Save owner may repair only a corroborated exact
result marker through its existing authority; it must never substitute latest.

Existing v1/v2 reads, Copy, encrypted export, closure/removal and reachability
continue to traverse the same sidecar/object schema. Pending-object receipts and
encrypted attempt candidates survive interruption; no garbage collection or
automatic pruning is introduced.

## Verification scope

Producer tests use real age and kernel read barriers with synthetic native files,
storage and admission. They do not establish live provider, installed VSIX,
immutable RCC, native Windows or model/account acceptance. Such evidence must be
recorded separately. Release HOLD and all-false Save selectors remain unchanged.
