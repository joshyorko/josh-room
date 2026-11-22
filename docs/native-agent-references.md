# Room-bound native history references

This experimental operation retains encrypted Codex/Claude native history beside
one exact logical recovery point. It does not restore original T3 app identity,
credentials, provider homes or a proven native new turn. Save/Enter and all agent
Save selectors remain unchanged and unavailable for automatic inclusion.

## Explicit authority and selection

The host-private admission policy must use
`josh-room.native-agent-admission.v3`, retain every existing v2 grant field, and
set `allow_room_reference: true`. Only personal, OSS and homelab reference
operations qualify; remote work scope remains blocked. V1/v2 grants and false
flags stay local-only. Current Git identity, canonical private roots, both
recipients and the live enrolled Dimension keyset remain mandatory. An unset
cached config domain is accepted only when the live keyset matches the exact
grant domain, physical bucket, generation and recipients.

The native recovery-point row offers **Agent Recovery Status…**, **Capture Agent
Recovery…**, and **Restore Agent History…**. These registered commands retain
the exact Dimension/Room/logical ID, never `latest`. Private roots stay inside
the controller; the UI passes only opaque selectors and native UUIDs.

```sh
josh-room agents recovery-status --dimension demo --room demo-room --snapshot EXACT_ID --json
josh-room agents room-status --dimension demo --room demo-room --workspace /workspaces/demo --json
josh-room agents recovery-capture --dimension demo --room demo-room --snapshot EXACT_ID \
  --workspace /workspaces/demo --profile-ref OPAQUE_PROFILE_REF --tool codex --json
josh-room agents recovery-verify --dimension demo --room demo-room --snapshot EXACT_ID \
  --workspace /workspaces/demo --profile-ref OPAQUE_PROFILE_REF --tool codex --json
josh-room agents recovery-restore --dimension demo --room demo-room --snapshot EXACT_ID \
  --workspace /workspaces/demo --profile-ref OPAQUE_PROFILE_REF --tool codex --session-id NATIVE_UUID --json
```

Selectors come from `room-status`, not authority. Every operation re-resolves
the grant. Remote commands require native Secret Service custody and refuse
plaintext provider/runtime/encryption handoffs. Linux enrolled MinIO/S3 are
supported; R2 and Windows remain unqualified.

## Separate encrypted storage

Catalog v3 and logical JAT v1 retain their closed schemas. No agent state enters
RCC, Homebrew, Hauler or PCC slots. The separately encrypted
`josh-room.agent-references.v1` control object is stored at
`control/agent-references/v1/<binding-sha256>.age`; native archives use immutable
content-addressed ciphertext objects.

Binding includes the physical bucket, domain/generation, Room/logical ID,
descriptor ciphertext digest/size, Restic repository/snapshot/tree, capture
policy and source workspace signature. The key excludes generation so rotation
fails explicitly instead of making old references appear absent. Entries retain
profile/tool, native format, ciphertext integrity and original provenance. One
profile/tool capture per point is immutable; successive captures against an
unchanged workspace point are not implemented. Sidecars use bounded, conditional
revision updates with read-back verification.

Capture requires the exact path-bound v3 marker and a fresh Save scan matching
the original catalog signature/policy. Restore authenticates that exact history,
current host grant/keyset/Git identity and original native cwd, then requires a
clean local marker baseline. Cold hydration can establish a different stat
baseline. Restore compares against that hydrated baseline; it does not prove
current contents equal the original snapshot. Markers never supply authority.

Duplicate native UUIDs are rejected before upload. Restoration reuses anonymous,
no-overwrite, byte-verifying single-session materialization into a fresh authorized
profile. Native bytes/UUIDs remain unchanged. It is not multi-provider/home
restoration. Native plaintext remains in bounded memory until intentional native
file materialization; control/bundle encryption uses age stdin, not plaintext
staging. Current native version/record allowlists still apply.

## Lifecycle behavior

Normal logical Copy preflights separate source/destination grants and independently
re-encrypts references even within one Dimension. New Room/JAT bindings preserve
original native provenance/cwd; profile and scope remain unchanged. Admission
must bind the actual opened bucket/generation, not just an equal Dimension name.
No source identity or credential is copied.
Unsupported authority fails explicitly rather than silently dropping references.

Normal portable export writes the original capsule plus a separately versioned
encrypted `<output>.agents.age` companion containing its native ciphertext objects.
No agent state enters Hauler. Verify/restore accept `--export COMPANION` against
the exact selected authorized history; offline import/rebinding after that point
is removed is not implemented. The two files are not one atomic transaction;
partial output is disclosed. Existing companions are never overwritten. The
companion limit is 32 MiB.

Removal CAS-closes existing captures before catalog removal. If catalog deletion
fails, references stay readable but capture-closed. After fresh reachability
proof, authenticated closure receipts permit deletion only of known unreferenced
native ciphertext. Objects still referenced anywhere in the live catalog are
protected even if a closure entry is misbound. Uncertain cleanup defers Restic forgetting. Encrypted closure
metadata remains. Absent sidecars create no new remote objects. A racing first
capture/copy must also conditionally republish the unchanged catalog before
discarding its pending-object receipt. Fresh age ciphertext changes the catalog
ETag without changing its logical contents or revision. This serializes against
a deletion that read an absent sidecar: either that stale deletion is rejected,
or capture/copy retains its known candidates and reports an uncertain outcome.
A fresh deletion retry sees and closes the published reference. Lost fence
responses and cancellation retain pending receipts; they do not imply rollback.

Pending object receipts are fsynced before upload. Reconcile reports reachable
objects and known pending candidates, without global enumeration or automatic
pruning/garbage collection. Authority is checked after encryption immediately
before publication and after effects. Cancellation/cleanup preserve uncertainty.
Known control-CAS rejection retains `sidecar_published: false` and `orphaned: true`
when ciphertext was uploaded; unknown publication/reachability stays unknown.

## Capability and qualification

The optional offline `capabilities.room_reference` contract is version 1,
`mode: history-only`, `storage: dimension-encrypted-sidecar`, and
`opt_in: host-grant-v3`, for Codex/Claude. It retains `selectable: false`,
`save_integration: false`, and `native_resume_verified: false`. Unknown versions
or modes remain unavailable. Issue #190 owns later Save integration.

See [real admission](native-agent-live-admission.md),
[lifecycle acceptance](native-agent-reference-acceptance.md) and the
[verification ledger](native-agent-verification.md) for exact source/runtime
overrides and evidence. Native continuation, original T3 recovery, broader formats,
repeated unchanged-point capture, offline import/rebinding, Windows and immutable
controller qualification remain separate gates. No release or model/paid call is
authorized by this contract.
