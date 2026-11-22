# Append-only child Save and explicit parent pin refresh

This extension depends on the isolated namespace vertical in PR #216. It does
not admit legacy repository migration, unload, deletion, or release publication.

## Reviewed transaction boundaries

Child Save accepts only an entered, path-bound namespace child, its exact current
logical baseline, and a retained operation ID. Planning stages the source through
held no-follow descriptors, binds its policy, record metadata and content digest,
and compares it with the authenticated saved baseline. Unchanged content does not
publish a new descriptor, catalog record or Restic snapshot. Changed content
reserves its exact logical output in CAS before backup; Restic receives the exact
prior payload snapshot as parent. Completed tagged orphan snapshots are verified
and reused. A claim without a completed snapshot still refuses automatic takeover.

The state contract advances to `composition-safety-v2`, preserving all existing
operations, catalogs, pins and repository bindings. A separate bounded child-update
ledger reserves and atomically publishes child records. Old v1 namespace readers
reject this contract; their stale CAS cannot overwrite it. Legacy root repositories
remain physically isolated. There is no retention deletion API.

A committed child Save updates only the child's local guard and marker. Both
old/new marker combinations are accepted solely when resuming that same committed
operation. Content changed after remote commit is not rebound as saved. A parent
is never refreshed implicitly.
If local rebinding fails after a verified remote commit, the CLI returns a nonzero
`saved-local-binding-unverified` receipt containing the saved pin and operation ID.
The working folder and later local edits remain unverified. Preserve both the
receipt and local edits; do not clear the guard or start a blind replacement.
Committed-operation retries re-read the exact descriptor and verify restored
payload bytes before reporting success or rebinding metadata.

Parent refresh requires a current exact parent baseline and explicit
`child-path=exact-snapshot` selections. It proves availability of those saved child
pins, restores the immutable old parent privately, changes only its manifest, and
publishes a new parent with exact lineage. Existing child paths and Room IDs cannot
be remapped. Unselected pins stay unchanged. Historical parent operations and
manifests retain their old pins, regardless of current child latest. Working parent
folders remain bound to their old snapshot; restore the returned new parent pin
into an absent destination to use its updated references.

Review conclusion: these bounded append-only transitions need no unload or
filesystem deletion authority. Independent review and installed/native qualification
of this extension remain separate gates; the prior installed receipt covers only
the earlier PR #216 source revision. Release remains on owner HOLD.

## CLI

Use `composition plan-child-save` with `--dimension`, `--namespace`, `--room`,
`--child-snapshot`, `--workspace`, and a stable `--operation-id`. Review its request
and `changed` field, then repeat as `composition save-child --approve <digest>`.
Retain the exact baseline, operation ID and digest for retries.

Use `composition plan-parent-refresh` with `--dimension`, `--namespace`,
`--parent-room`, `--parent-snapshot`, a stable `--operation-id`, and one or more
`--set-child path=exact-logical-snapshot` selections. Repeat as
`composition refresh-parent-pins --approve <digest>`. Neither operation accepts
`latest`. Changing a reserved request requires reconciliation, not a new blind retry.

The native **Save Child Room** action is also used by ordinary Save when the
current folder is a guarded namespace child. **Refresh Catalog Child Pins**
publishes one explicitly selected path/pin per action. Pending operation ID,
baseline, output and approval are retained in VS Code workspace state before
publication and cleared only after a verified result. A changed pending approval
refuses a blind retry. Normal unbound/full-Room Save and its component picker
retain their existing routing. Namespace capture does not consume Homebrew,
RCC or Hauler defaults. Native action tests are not installed-VSIX acceptance.

The bounded ledger retains at most 128 child updates and 32 parent operations per
namespace, including initial capture. Reaching a bound refuses further writes;
history is never trimmed. No-op checks restore the authenticated baseline and read
payloads; they avoid new catalog entries, descriptors and snapshots, not all
provider reads or temporary repository locking.

New child-update approvals identify `sha256-framed-v2`: path, entry type, mode,
byte length and file bytes are framed before hashing. Existing immutable
descriptors have no such digest field; they remain readable and their restored
payloads are compared using the new algorithm. Old ledger entries without an
algorithm remain readable and immutable, but cannot be resumed with their old
approval digest. An uncommitted old entry requires reconciliation; a completed
entry may be followed by a newly approved operation at its exact saved baseline.
Neither retries nor history are silently upgraded. Tagged snapshot retries recover
real persisted byte counts and verify actual payload ancestry before publication.

Every child update also requires authenticated `source.composition_policy` equal
to `inherited-rules-v2` on its exact baseline before any staging or publication.
This is independent of operation ID and hash version. New initial captures stamp
it only after proving the rebased policy view; verified updates retain it. Legacy
descriptors remain readable/restorable but cannot authorize new child writes.
Editing an ignore-file comment cannot supply provenance. Legacy-policy
reconciliation is not implemented and never happens implicitly.

The live effective policy digest must exactly equal the authenticated baseline
before staging, for fresh operations and no-ops alike. It is checked again before
capture and descriptor/catalog publication; the private view and restored output
must preserve that same digest. All policy edits, including comments, require
unsupported explicit reconciliation and cannot silently acquire provenance. The
experimental `inherited-rules-v1` marker remains readable/restorable but no longer
authorizes updates: earlier builds could stamp a changed policy. Only new v2
generated captures or verified unchanged-policy v2 updates can supply the proof.
