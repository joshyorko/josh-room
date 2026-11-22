# Native capture-history contract v2

Design checkpoint for the dependent follow-up to #210. The initial tested slice
adds Claude archive-policy v2 recapture; this immutable Room-history extension is
specified here before implementation. It is **not** Save integration. Selectors
remain false, and unknown contract versions remain unavailable.

## Stable address and versioned document

Retain the existing `control/agent-references/v1/<binding-sha256>.age` CAS address.
The locator hashes the unchanged exact historical Room binding. A second control
namespace would let old writers/removers miss new history, so the document format
changes at that same address. Older readers must reject it before mutation.

The v2 encrypted document has exactly the v1 fields `binding`, `revision`,
`captures_closed`, `entries`, plus `format: josh-room.agent-references.v2` and
`workspace_content_sha256`. Its entries retain the v1 fields `profile`, `tool`,
`scope`, `created_at`, `archive_format`, `object`, `archive_binding`, `origin`,
and add `capture_id` and `content_sha256`. Keep the existing 64-entry and bounded
control-size limits. No automatic pruning or garbage collection is introduced.

- `capture_id` is an opaque immutable 64-hex event ID, distinct from native session
  UUIDs and ciphertext object hashes. Copy/export preserve it.
- `content_sha256` hashes a domain-separated canonical sorted native-file manifest
  of relative paths, raw-byte lengths and raw-byte SHA256s, plus the tool. It
  excludes archive schema/encryption recipients/ciphertext identity.
- The latest matching profile/tool capture determines no-op behavior. Immediate
  identical content returns its existing ID without upload, new pending receipt,
  sidecar CAS or catalog fence. A → B → A records three immutable events.
- Native bytes are never rewritten to produce either identifier.

V1 reads remain supported without writes. Stable legacy IDs derive from existing
origin/archive-binding/profile/tool/scope/created-at metadata, not ciphertext.
Content digests are computed and checked when decoding/migrating. Promotion occurs
only during a successful changed capture with an authorized content baseline.

## Workspace authority after hydration

Establish `workspace_content_sha256` under the original captured stat baseline.
The domain-separated digest binds the selected capture policy/mode and included
relative paths, file bytes/modes, directory modes and literal safe link targets.
It excludes inode/ctime/mtime, so normal same-cwd hydration can reproduce it.
Live reads must be descriptor-anchored/no-follow and mutation-checked, not staging
helpers that can follow a transient swapped parent.

Later capture requires the exact point/policy/binding, a clean path-bound current
marker and the same authenticated semantic digest. Recheck the whole digest and
authority before publication. A hydrated local marker alone cannot initialize
v1's missing baseline. Without original-stat authority or a separately verified
historical content proof, that migration fails closed. No marker rewrite or
phantom workspace snapshot is permitted.

## Selection and lifecycle

V2 verify/restore use an explicit `--capture-id`; never silently select latest.
Status exposes only capture IDs, timestamps, tool/profile/scope and content digests,
not native text or private profile paths. V1's single-capture interpretation stays
available. The existing native-history restore UI can obtain filtered status,
choose an explicit capture, verify that ID, choose its native UUID and restore the
same ID. This is separate from #190's Save chooser.

Copy re-encrypts all historical objects and preserves IDs/digests/origin/native
binding/timestamps. Export includes all history and rejects excessive aggregate
size before collecting blobs or publishing a normal capsule. Closure/removal and
reachability retain every historical candidate, deduplicate deletion and protect
all unrelated live catalog objects. Existing pending/orphan receipts, publication
uncertainty and the catalog-CAS fence remain required. A no-op never clears older
uncertain receipts.

The separately reviewed Save capability will advertise version 2 only after this
contract is implemented/qualified, with `selectable: false` and
`save_integration: false`. No #190 wiring follows from this specification.
