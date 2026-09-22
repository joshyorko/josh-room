# PCC Crypto #10 Design

## Goal

Stream each v1 normalized session evidence event into an independently decryptable, randomized age ciphertext and atomically publish only ciphertext plus safe delivery metadata to the existing #8 prepared outbox.

## Scope

This change owns evidence envelope construction, recipient resolution at the #3 policy seam, managed-age streaming, verification, durable temporary ciphertext cleanup, the additive file-backed prepared-outbox handoff, mirror/template copies, focused tests, and narrow documentation. It does not install hooks, change source discovery, change normalization, publish to R2, or implement device enrollment.

## Inputs and identity

The encryption boundary consumes the actual #9 `NormalizationEvent` shape:

- `session-segment`, whose typed payload is the canonical JSON document;
- `session-asset`, whose typed payload is the asset byte stream exposed by the event's asset receipt/sink boundary;
- `session-final`, whose typed payload is the canonical JSON document; and
- `index-event`, accepted as a validated v1 document for the later #11 producer.

Every input is validated by `session_evidence.validate_document`. Unknown major versions, malformed documents, wrong kinds, and reference mismatches fail closed. The encrypted object contains exactly one typed payload and a manifest that binds its kind, schema version, event identity, payload digest/size, profile/workspace, source provenance, policy result, recipient-set fingerprint, and relevant segment/asset/index references.

## Envelope and age pipeline

The plaintext presented to age is a streaming envelope with exactly two logical members: a versioned JSON manifest and one typed payload. JSON payloads use canonical v1 JSON bytes; asset payloads use raw bytes. The manifest is encrypted with the payload and is never written separately as durable plaintext.

The managed age executable is launched with public recipients only. A producer thread writes the envelope to age stdin while a bounded reader copies age stdout into a restrictive adjacent temporary ciphertext, updating SHA-256 and exact byte count. Stderr is consumed without exposing its contents. No complete plaintext or ciphertext is materialized in memory, and private identities are not passed through argv, environment, logs, receipts, or metadata.

The temporary ciphertext is fsynced, hashed, size-checked, and atomically handed to the outbox. Any age error, cancellation, broken pipe, short write, digest mismatch, disk-full error, publication failure, or process crash leaves no prepared record and removes the temporary file when possible.

## Recipient authority

The caller supplies a host-observed #3 `CaptureProfile` and a host-owned resolver keyed by that profile's `recipient_set_ref`. Event fields cannot select a profile, recipient set, destination, or broader policy. Recipient values are treated as opaque age recipient strings; structural malformed values, duplicates, missing daily-use/recovery roles, unsupported values, and unrelated sets are rejected without logging the values.

Private-R2/production-enabled profiles require distinct daily-use and independent recovery recipients. Recipient ordering is canonicalized for a deterministic versioned fingerprint; the fingerprint is encrypted and no private identity is recorded. Rotating the set affects only newly prepared objects. Historical ciphertext is not revoked or re-encrypted by this issue.

## Outbox handoff

`pcc_outbox.py` gains an additive file-backed preparation method and durable prepared-record representation. The existing byte-based `prepare_encrypted` API remains compatible for prior workspace and test callers. The new path stores the ciphertext in a restrictive binary prepared object and a separate JSON state record containing only the event ID, ciphertext digest/size, and validated public-safe metadata. Both publication steps use the existing outbox lock, atomic rename, file fsync, and directory fsync semantics; recovery can reconcile a durable ciphertext-only prepared object after a process crash.

## Verification

Focused synthetic tests cover all four kinds, normalization integration, policy recipient matrices, fingerprint stability, randomized ciphertext, independent decryptability, wrong identities, tampering/truncation/digest/manifest/kind/schema failures, failure injection, cleanup, no metadata leakage, and bounded stream behavior. Real-age tests are skipped only when managed `age`/`age-keygen` are unavailable. The full Python suite, Ruff, diff checks, mirror parity, and available real-age vertical are run before handoff.
