# PCC session normalizer

`josh_room.session_normalizer.SessionNormalizer` is the #9 boundary between
the approved Codex source stream and the future encryption stage. It consumes
one already-open #6 `BoundedRecordStream`; it does not discover files, reopen
Codex roots, inspect the #8 queue, encrypt, upload, or create a plaintext
staging file.

## Input and output

The normalizer accepts an immutable `NormalizationContext`, the stream's
initial #6 `Checkpoint`, and optional linkage to the previously committed
segment. It yields `NormalizationEvent` values containing validated Session
Evidence Envelope v1 documents:

- `session-segment` documents contain one bounded, ordered source range.
- `session-asset` documents contain metadata for bytes handed to an injected,
  ephemeral `AssetWriter`.
- `session-final` is emitted only when the caller requests finalization.

Segment boundaries are determined only by configured record and canonical
normalized-byte limits. Event IDs and segment linkage use canonical content,
source ranges, and the prior segment digest; wall-clock time and file
timestamps are not involved. Replaying an already committed checkpoint opens
an empty range and emits no duplicate segment.

## Closed record and material policy

The normalizer accepts only the bounded normalized record kinds already
approved by the #6 adapter: visible messages, session metadata, tool metadata,
usage, approvals, sanitized repository identity, deferred asset metadata, and
the explicit chunked `asset_payload` seam. Unknown kinds, hidden or encrypted
reasoning, credentials, auth/keyring/config material, malformed records, and
unsafe repository remotes become bounded `capture_gap` records. A trusted
record boundary permits continuation; a cursor or source-stream failure
quarantines the range.

The #6 adapter's `record_kind=asset, decision=deferred` record contains only
bounded size/digest metadata. It is never interpreted as bytes. Actual asset
bytes enter this boundary only through the explicit bounded `asset_payload`
sequence, whose MIME type is closed and whose Base64 chunks are decoded,
hashed, and handed off incrementally. Repeated content is deduplicated only
within the normalizer's bounded in-memory session scope.

Transcript and tool text remains inert data. It is NFC-normalized, line-ending
normalized, and stripped of ANSI/control characters; no Markdown, URL, shell,
template, or instruction-like text is executed or interpreted.

## Resource and plaintext guarantees

`NormalizationLimits` bounds source bytes, record bytes, segment bytes and
records, decoded asset bytes, asset counts, total session bytes, working
bytes, and dedupe entries. The normalizer retains one segment and one active
asset state at a time. The writer owns the ephemeral asset handoff: it must
not turn the injected sink into a durable plaintext staging area. On
cancellation, malformed Base64, writer failure, iterator abandonment, or
source failure, the active sink is aborted and only public-safe reason codes,
counters, hashes, and checkpoints are exposed.

The next stage (#10) can consume the yielded evidence events as a stream. It
owns encryption and durable preparation; those concerns intentionally do not
appear in this module.
