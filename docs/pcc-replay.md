# PCC raw-evidence replay/export

`josh_room.pcc_replay` is the public #14 consumer seam. It does not import
`codex-memoryd`, perform semantic extraction, write memory, or alter capture and
upload health.

## Contract

`ReplayReader` requires an explicit `profile_id` and `destination`, and accepts
an existing #11 backend plus an approved local age identity. `export()` reads a
bounded, deterministic page of encrypted index events, verifies index and
object SHA-256/size, decrypts both envelopes, validates Session Evidence v1,
checks the profile/workspace and policy binding, verifies the segment chain and
asset references, and returns inert normalized records plus quarantine receipts.
`iter_jsonl()` emits the versioned `josh-room.pcc-replay` JSONL contract described
by `schemas/pcc-replay-v1.schema.json`.

The cursor is opaque, bounded, and consumer-owned. It contains only the
explicit profile, destination, and last index key. A consumer may discard it and
replay from the beginning. Idempotency keys are SHA-256 over stable
profile/workspace/session/checkpoint/segment/plain-record identity; they never
use listing order or ingestion time.

`inspect()` is metadata-only: it lists bounded index keys and ciphertext
identities without decrypting or printing evidence. Export is the explicit
consumer path. Decrypted strings, including transcript/tool text, remain inert
JSON data and are never interpreted as commands, paths, URLs, templates, SQL,
or secret references.

Unknown major or malformed schema, policy/profile mismatch, untrusted producer,
broken predecessor chain, missing asset, corrupt ciphertext, and any digest or
size mismatch produce a stable quarantine receipt. Age decryption establishes
confidentiality/recipient integrity only; the producer must provide an explicit
authenticated claim for export. The current synthetic path labels absent or
unverified producer authentication as `untrusted-producer`.

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

The future MemoryD PCC importer is a separate bounded issue. It should consume
only this neutral JSONL stream, apply its own profile policy and content
screening, and report imported/skipped/quarantined counts. It must not parse R2
objects or import Josh Room private modules. No importer issue is opened by
this change without parent authorization; the required issue should reference
Josh Room #14, this schema, profile/workspace default-deny, idempotent cursor
resume, and the quarantine receipt contract.

## Triggering and live evidence

Optional R2 Event Notifications may wake an operator or Action Server, but they
are at-least-once hints only. Explicit replay from the encrypted index remains
authoritative, and duplicate/out-of-order notifications are harmless when the
consumer owns its cursor and idempotency store.

Synthetic fixtures and the neutral consumer tests do not claim a live private
R2 acceptance. Live R2 evidence is **SKIPPED — private R2 credentials are not
available**.
