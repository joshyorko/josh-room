# Session Evidence Envelope v1

This document defines the transport-neutral contract from issue #2. It is a
data contract only: it does not discover Codex files, install hooks, choose a
policy, classify material, encrypt, upload, or call a downstream memory
service.

## Schema family and document kinds

Every document has `schema_name: "codex-session-evidence"`, a
`schema_version` object with non-negative integer `major` and `minor`, a
`kind`, and an externally supplied `event_id`. The four kinds are:

| Kind | Meaning | Content identity |
| --- | --- | --- |
| `session-segment` | One immutable incremental range of normalized records. | Canonical JSON of `records`; `content_sha256` and `content_size` describe those bytes. |
| `session-asset` | Metadata for one extracted binary or large value referenced by a segment. | `sha256` and `size` describe the asset bytes; the contract contains no asset URL. |
| `session-final` | Final or recovered session state and its last resumable checkpoint. | `content_sha256` and `content_size` describe the final-state payload supplied by its producer. |
| `index-event` | Append-only discovery metadata for one evidence object. | `content_sha256` identifies plaintext content; `ciphertext_sha256` identifies the encrypted object. |

The JSON Schemas are stable files under `schemas/`:

- `session-evidence-session-segment.schema.json`
- `session-evidence-session-asset.schema.json`
- `session-evidence-session-final.schema.json`
- `session-evidence-index-event.schema.json`

## Shared vocabulary

The encrypted evidence documents can carry the following facts when supplied
safely. Missing Codex facts are omitted; the producer must not invent a
session, thread, turn, subagent, version, repository, or timestamp. Explicit
unknown values are used only where the vocabulary provides one, such as the
`unknown` source surface, checkpoint representation, sensitivity, and
repository fields.

- `event_id` is the identity of this immutable document event.
- `session_id`, `thread_id`, `turn_id`, and `subagent.id`/`subagent.type` are
  optional Codex identifiers. They are not required when Codex did not supply
  them. The optional top-level identifier fields are declared consistently
  across all four document kinds.
- `source.surface` is one of `cli`, `desktop`, `vscode`, `app-server`,
  `import`, or `unknown`. `source.adapter` and `source.adapter_version` name
  the producer; `source.codex_version` is optional.
- `profile_id`, `workspace_id`, and `device_id` are opaque identifiers. The
  device identifier is pseudonymous; a display name is not part of this
  contract.
- `repository`, when present, contains only a sanitized remote identity,
  commit, branch, and `dirty` state. `dirty` is `true`, `false`, or `unknown`.
- `checkpoint` is a source name, representation, non-negative record/byte
  range, and prior-prefix digest. It contains no absolute path and never uses
  wall-clock time as ordering authority.
- `capture.status` is `partial`, `complete`, `recovered`, or `quarantined`.
  `policy_decision`, `sensitivity`, and non-negative redaction/block counters
  are observations supplied by their owning policy/classification stages.
- Timestamps are observations only. They do not establish event order.

## Identifiers and limits

Externally supplied identifiers use the ASCII grammar
`^[A-Za-z0-9][A-Za-z0-9._~-]*$` and have a maximum length of 128 characters.
This applies to event, session, thread, turn, subagent, profile, workspace,
device, asset, source-event, source-adapter, and checkpoint-source IDs. A
slash, backslash, colon, leading dot, `..`, URL, or absolute path is not an
identifier. Adapter and Codex version strings use the same grammar with a
maximum length of 64 characters.

Repository remotes are sanitized host/path identities such as
`github.com/synthetic/example`; schemes, credentials, query strings, fragments,
and empty path components are rejected. Commit values are lowercase hexadecimal
identifiers of 7–64 characters. Branch values are bounded to 128 characters,
may contain single `/` separators, and may not contain `..` or `//` path
components. SHA-256 values are exactly 64 lowercase hexadecimal characters.

These limits are enforced by the executable validator as well as the schemas.
Validation errors expose only a stable code and JSON path; they never include
record values.

## Canonical serialization and hashing

`josh_room.session_evidence.canonical_json(value)` produces the identity bytes:

1. JSON objects are ordered recursively by Unicode key order.
2. Separators are `,` and `:` with no insignificant whitespace.
3. Text is UTF-8 and Unicode is not unnecessarily ASCII-escaped.
4. NaN and infinity are rejected.
5. The result is hashed with SHA-256 by `canonical_digest(value)`.

Object insertion order therefore cannot change a digest. Host filesystem paths
are adapter-local and are not fields in the envelope identity vocabulary; an
adapter must remove them before constructing a document. Top-level `path` and
`*_path` fields are rejected at the schema/runtime boundary, while known
identifier fields are checked against the safe identifier grammar. Known nested
contract objects are strict (`additionalProperties: false`); document-level
additive fields are preserved and ignored without applying validation for a
different document kind. Transcript text and tool output remain payload data
and are not interpreted or normalized as instructions. If their content
changes, their payload digest is expected to change.

Observation timestamps use RFC3339 date-time values with an explicit `Z`/`z` or
numeric offset. They remain observations and never establish ordering.

For a `session-segment`, `records` is the normalized record array and its
canonical JSON bytes are the plaintext payload described by `content_sha256`
and `content_size`. Asset bytes are not embedded in an asset reference.

## Compatibility

The current major is `1`, and the current minor is `0`.

- An unknown major is not guessed. The validator returns `quarantined` with
  `unknown_major`.
- A higher minor may add document-level fields. Unknown additive fields are
  preserved in the returned document and ignored by current field-aware
  validation; known nested objects remain strict so schema and executable
  validation cannot disagree about their shape. A segment-only field does not
  acquire segment semantics when added to a final, asset, or index document.
- Known fields, required checkpoints, identifier rules, digest rules, and
  closed enums remain enforced for every accepted minor.
- A malformed version, wrong type, missing required field, or invalid known
  value is `rejected` with stable machine-readable error codes.

The result type is `ValidationResult` with `disposition`, `errors`, and a
defensive copy of the accepted document. Error codes include
`missing_checkpoint`, `digest_mismatch`, `duplicate_asset_reference`,
`invalid_repository_provenance`, `invalid_identifier`,
`identifier_too_long`, `invalid_metadata`, `unknown_field`, `unknown_major`,
and `wrong_type`.

## Asset references and cleartext metadata

Segment `asset_refs` contain exactly `asset_id`, `sha256`, `size`, and
`media_category`. They contain no URL, URI, credential, token, bucket, or
storage key. Duplicate asset identifiers are rejected.

`minimal_unencrypted_metadata()` accepts only these declared envelope content
types: `application/vnd.josh.codex-session-segment+json`,
`application/vnd.josh.codex-session-asset`,
`application/vnd.josh.codex-session-final+json`, and
`application/vnd.josh.codex-index-event+json`. It emits only:

```json
{
  "schema_family": "codex-session-evidence",
  "schema_major": 1,
  "ciphertext_sha256": "…",
  "ciphertext_size": 0,
  "content_type": "…"
}
```

It does not expose repository, customer, employer, project, profile, device
display name, session title, or source path. Event identity and plaintext
content identity are inside the encrypted document; ciphertext identity is the
outer object digest; `index-event.discovery` is mutable discovery state and is
not a replacement for any of those identities.

## Threat assumptions and non-goals

Transcript text, tool output, filenames, and metadata are untrusted inert data.
Schema validation does not make their text authoritative and no field can
select a path, command, import, URL fetch, secret lookup, profile, or
destination. This issue intentionally does not implement capture, filesystem
discovery, policy decisions, material classification, hooks, encryption, R2,
memory extraction, or adapter behavior. Existing workspace snapshot/catalog
schemas and behavior remain separate and unchanged.
