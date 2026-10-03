# Room Store secret prerequisite

The encryption keyset parser remains backward-compatible with format 1. A
format-1 keyset changes only through the explicit
`EncryptionKeyset.upgrade_for_room_store()` API. The upgrade creates a fresh
32-byte secret with the operating system cryptographic random source, keeps the
age `key_generation` unchanged, and adds format-2 `room_store` metadata. The
secret uses canonical unpadded base64url encoding. Parsing can verify that
encoding and its 32-byte length; it cannot prove that an externally supplied
value was generated with sufficient entropy.

The metadata fixes the repository prefix to `room-store/v1`, pins the Restic
repository format to version 2, and binds the secret to the normalized
provider/endpoint/bucket identity. Aliases with the same normalized physical
binding therefore use the same secret. The optional repository ID starts
empty. After repository initialization, `bind_repository()` sets it once and
advances the Room Store metadata generation. A repeated bind to the same ID is
idempotent; a stale generation or a different ID fails. Neither operation
rotates the age key generation.

## Persistence and trust boundary

The current MinIO keyset control object is serialized with `to_json()` and is
not age-encrypted by this code. Format-2 keyset serialization therefore makes
the Room Store secret readable to anyone who can read that control object.
The selected bucket credential is an enrollment-capable trust decision; this
change does not add a second encryption layer or claim confidentiality from a
bucket reader. Recovery private identities are never added to the keyset.
The keyring helpers cache the Room Store secret separately from the age
identity in the native OS secret store, scoped to the encryption-domain ID and
Room Store metadata generation. They do not write it to config, workspace
markers, logs, argv, or receipts. Binding the repository advances the metadata
generation, so the integration must cache the same winning secret under the
new generation after the bind succeeds. The prior generation cache entry is
retained because the password did not rotate and an in-flight operation may
still hold the old generation; this metadata update is not secret rotation or
cache garbage collection.

## Required remote-writer handshake

`auth.ensure_room_store_keyset(dimension, backend)` is the explicit MinIO
upgrade API. It uses the existing S3 backend
`replace_control(key, body, expected_etag)` seam, reloads the keyset after
every write outcome, and caches only the durable winner. If another first
writer wins, it discards its candidate and uses the winner's secret. It fails
closed when the Dimension keyset is absent or the backend lacks conditional
replacement. It does not enroll encryption identities or invoke normal Save.

`auth.bind_room_store_repository(dimension, backend, repository_id,
expected_generation=...)` is the explicit post-initialization bind API. It
conditionally publishes the one-time repository ID, then reloads and validates
the durable value before caching under the current metadata generation. Same-ID
retries are idempotent. A competing password, repository ID, domain, or
generation fails closed. These APIs support MinIO only; R2's keyset authority
remains owned by the Cloudflare auth service.

The owner integrating remote writes must:

1. Call `ensure_room_store_keyset()` before Restic initialization. If the
   keyring is unavailable, fail before Restic starts.
2. Initialize only `room-store/v1` with repository format 2 and the returned
   winner's secret. Verify the resulting repository ID and format.
3. Call `bind_room_store_repository()` with the repository ID and the metadata
   generation returned by step 1. If a conditional write loses, accept only a
   winner with the same repository ID, secret, physical binding, and exact
   next metadata generation. A different value is a hard conflict requiring
   explicit maintenance.

The backend's conditional replacement and readback semantics must be proven
for each provider before this handshake is enabled. The keyset JSON currently
contains the secret in the fixed control object, so this prerequisite alone
does not satisfy a design that requires secrecy from bucket readers.

Changing or removing the Restic password does not revoke a leaked password
from existing repository data. True revocation can require repository
re-encryption.
