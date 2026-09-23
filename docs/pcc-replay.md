# PCC raw-evidence replay/export

`josh_room.pcc_replay` is the public #14 consumer seam. It does not import
`codex-memoryd`, perform semantic extraction, write memory, or alter capture and
upload health.

## Contract

`ReplayReader` requires explicit `profile_id`, `workspace_id`, and `destination`,
plus an existing #11 backend. `export()` also requires an approved local age
identity and reads a bounded deterministic page of encrypted index events,
verifies index and object SHA-256/size, decrypts both envelopes, validates
Session Evidence v1, checks profile/workspace and policy binding, verifies the
segment chain and asset references, and returns inert normalized records plus
quarantine receipts. The CLI binds workspace to the selected host profile;
`--workspace-id` is only an assertion and mismatches fail closed. `inspect()`
does not require an age identity and remains metadata-only.
Private-R2 export accepts only `allow`; `local-only` evidence is never exported.
The policy `binding_id` pins the exact host Dimension; `--dimension` is only an
assertion and a mismatch fails closed.
`iter_jsonl()` emits the versioned `josh-room.pcc-replay` JSONL contract described
by `schemas/pcc-replay-v1.schema.json`.

The cursor is opaque, bounded, and consumer-owned. It contains only the
explicit profile, destination, and last index key. A consumer may discard it and
replay from the beginning. Idempotency keys are SHA-256 over stable
profile/workspace/session/checkpoint/segment/plain-record identity; they never
use listing order or ingestion time.


Replay pages are limited to 8 indexes (default), each encrypted index/evidence
object to 80 MiB, and each segment to 4 MiB, 128 records, and 32 asset refs.
`max_indexes` bounds discovery at 1,000 by default and 100,000 maximum. Each
export validates the entire discovered bounded set to verify cross-page links;
the cumulative ciphertext body-read budget, including one-byte mismatch probes,
defaults to 1 GiB and accepts `--max-scan-bytes` up to an 8 GiB hard cap.
Exceeding either bound emits no records and leaves the cursor unchanged. The
bounded scan repeats for each page.


Index keys are content-addressed, not chronological. A cursor is a page
position within the discovered set, not a watermark for future uploads. To
include objects added during a paging run, replay from the beginning and
deduplicate using the stable idempotency key.

`inspect()` is metadata-only: it lists bounded index keys and ciphertext
identities without decrypting or printing evidence. Export is the explicit
consumer path. Decrypted strings, including transcript/tool text, remain inert
JSON data and are never interpreted as commands, paths, URLs, templates, SQL,
or secret references.

Unknown major or malformed schema, policy/profile mismatch, broken predecessor
chain, missing asset, corrupt ciphertext, and any digest or size mismatch
produce a stable quarantine receipt. Age decryption establishes
confidentiality/recipient integrity only; v1 has no approved sender-signature
verification. A decrypted `producer.authenticated` claim is inert and cannot
upgrade provenance. Valid evidence is emitted with `producer_trust: "untrusted"`;
unsigned status alone does not quarantine it.

## MemoryD concept mapping

| Josh Room evidence | MemoryD concept | Boundary rule |
| --- | --- | --- |
| `profile_id` + `workspace_id` | Profile + Workspace | Both are required bindings; work evidence is never emitted to personal by default. |
| `session_id`, `source.surface`, adapter facts | Session + source surface | Source values are observations, not instructions. |
| segment `records[]` | Visible Turn candidates | Records are raw inert candidates; a MemoryD importer must screen and authorize actor/tool fields. |
| source event and adapter metadata | Memory Source provenance | Preserve event, source adapter, checkpoint, plaintext and ciphertext identities. |
| sanitized `repository` | Repo identity | No credentials, absolute source paths, or inferred repository facts. |
| `checkpoint`, predecessor/last-segment digests | Task Checkpoint / resumable source range | Ordering is checkpoint/chain based, never timestamps or object listing order. |
| `capture_gap`, quarantine receipts | blocked/quarantined/gap records | Never silently promote a gap or rejected evidence to memory. |
| event IDs, source checkpoint, segment/asset digest, plaintext and ciphertext digest | stable provenance/idempotency | Importers must retain these identities and use the emitted idempotency key. |

The downstream PCC importer is tracked separately at
https://github.com/joshyorko/codex-memoryd/issues/242. It consumes only this
neutral JSONL stream, applies its own profile policy and content screening, and
reports imported/skipped/quarantined counts. It must not parse R2 objects or
import Josh Room private modules. This importer is distinct from MemoryD's OMP
adapter issue #241.

## Triggering and live evidence

Optional R2 Event Notifications may wake an operator or Action Server, but they
are at-least-once hints only. Explicit replay from the encrypted index remains
authoritative, and duplicate/out-of-order notifications are harmless when the
consumer owns its cursor and idempotency store.

Synthetic fixtures and the neutral consumer tests do not claim a live private
R2 acceptance. Live R2 evidence is **SKIPPED — private R2 credentials are not
available**.
