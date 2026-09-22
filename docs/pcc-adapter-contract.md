# Logical source adapter contract

Issue #5 adds a closed internal source boundary for future Codex adapters.
The boundary is intentionally not a plugin API and does not route workspace
snapshots.

## Operations

Each built-in adapter exposes `probe`, `plan`, `open`, `checkpoint`, `resolve`,
and `inspect` in `josh_room.synthetic_adapter` (the contract types live in
`josh_room.adapter_contract`). Plans and checkpoints use logical root labels;
they never export host paths, credentials, raw records, or wall-clock/mtime
authority.

`open` is a pull iterator. The caller controls backpressure by requesting one
record at a time, supplies a `CancellationToken`, and receives a bounded
`StreamResult` for checkpoint publication. A caller must provide both a policy
gate and a material gate to `plan`. The adapter reports their frozen decisions;
it cannot choose a profile, recipients, destination, retention, or upload
authority. `plan` reads only the source's cached count, byte-size, and prefix
metadata; record content is read only by `open`.

## Stable logical names and registry

The current reviewed built-ins are:

- `codex.transcript`
- `codex.session-metadata`

`builtin_registry()` is a literal mapping. There are no entry points, dynamic
imports, repo-controlled adapter modules, arbitrary commands, or network
access. Adding another logical source requires reviewed vocabulary, an explicit
declaration, contract tests, and a change to the built-in registry.

The synthetic declaration explicitly approves only the directional transitions
`active-jsonl` → `archived-jsonl` → `compressed-jsonl-zst`. A representation
transition may publish or resume a checkpoint only when the stable identity and
all consumed-boundary metadata still match. `open` accepts only plans bound by
the adapter that created them; it requires plan identity in the adapter's
module-private issuance boundary and revalidates an immutable structural and
source snapshot. Immediately before and during streaming, live record bytes,
lengths, prefix digests, and observed size are checked against that snapshot;
changed synthetic source data is never emitted.
There is no externally callable sealing helper or key. Changing status, limits,
or gate decisions in a plan is rejected before a stream is created. Exceptions
raised by caller gates become opaque typed errors; their paths, URLs,
credentials, and raw messages are never exposed.

`BoundedRecordStream` is an empty-slotted facade with no record, callback, or
limit slots. Its weak-identity private state store holds the generator,
counters, cancellation token, immutable copied scalar bounds, and result.
Record, asset, record-count, and byte-count limits are enforced before an item
is returned. The built-in registry accepts only the literal approved logical
source set and exposes an immutable adapter map; it is not a plugin registry.

## Resume semantics

A checkpoint identifies the logical source, session, stable source identity,
representation, next record/byte boundary, observed size, and digest of the
consumed prefix. Publication and resume validate every cursor field against the
current synthetic source: the source identity and representation, record count,
byte boundary, observed size, and prefix digest. Resuming the same stable prefix
is idempotent. Prefix changes, truncation, replacement, and inconsistent cursors
return stable typed conflicts. A move between the approved
active/archive/compressed representations is resumable only when the stable
source identity and consumed prefix still match; unknown representations
quarantine. Public errors are code-only and public JSON contains no paths, URLs,
credentials, or records.

The synthetic adapter is test-only deterministic data. It is not a Codex
parser, filesystem scanner, watcher, network transport, or credential reader.
