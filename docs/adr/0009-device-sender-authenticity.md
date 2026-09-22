# ADR 0009: Device authority and sender authenticity

## Status

Accepted for issue #12.

## Decision

Josh Room uses age confidentiality/integrity, a private R2 endpoint, and an
opaque device identifier as an **untrusted claim**. Device provenance is
metadata supplied by the producer; it is not an authentication proof. No
signing registry or owner-root signing chain is introduced in this release.

Durable credentials and enrollment receipts are held only by an allowlisted
native OS secret authority: Linux Secret Service (including KWallet-compatible
providers), macOS Keychain, or Windows Credential Manager/Credential Locker.
Null, plaintext, file, environment, container-layer, fail-open, and unknown
backends are rejected. A copied JSON configuration does not copy the private
receipt and therefore cannot make a device ready for prepare/upload.

A device profile binds an R2 credential profile to a versioned recipient set.
Rotation changes recipients for new writes while retaining historical recipient
metadata so old ciphertext remains readable to holders of old identities.
Previously published ciphertext cannot be revoked by this local operation.
Recovery identity/profile is independent of the device identity and is not
rotated or removed as a side effect of operational recipient rotation.

## Options considered

1. **Untrusted provenance claim (selected).** Age protects content and the
   private R2 transport/storage boundary protects object custody. Device ID,
   profile, and recipient-set version remain useful routing/audit metadata but
   are explicitly untrusted. This is implementable without a key-distribution
   service and does not overclaim sender authentication.
2. **Signing registry.** Each producer signs claims and recipients verify keys
   through a registry. This adds key enrollment, rotation, revocation,
   registry availability, and recovery authority that issue #12 does not
   provide. It is deferred rather than silently approximated.
3. **Owner-root chain.** A root owner key authorizes device keys and a
   transitive chain establishes provenance. This provides stronger identity,
   but requires owner-root custody and recovery ceremonies, online/offline
   revocation semantics, and a migration for existing captures. It is outside
   the approved scope.

## Consequences

- Prepare/upload is blocked when the native authority is missing, locked, or
  diagnostically unavailable; hook enqueue remains available for later retry.
- `device doctor` reports backend identity, availability, lock/session and
  WSL/container/headless diagnostics without exposing secret values.
- Fresh-device transfer is out-of-band: transfer the approved age identity and
  current recipient set through an operator-controlled secure channel, then
  enroll on the fresh device via stdin. Never place them in argv, environment,
  repository files, or container layers.
- Historical revocation is limited: rotating new writes does not make prior
  ciphertext undecryptable to an identity that already has the old recipient.
