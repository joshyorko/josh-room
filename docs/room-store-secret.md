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
new generation after the bind succeeds.

## Required remote-writer handshake

This change adds pure keyset APIs and OS-keyring cache helpers; it does not
change the auth writer. The S3 backend already exposes
`replace_control(key, body, expected_etag)` with conditional replacement and
readback, but the current auth writer does not use it. A caller must not
persist an upgrade or start Restic initialization until the auth writer uses
that seam and verifies its results.

The owner integrating remote writes must:

1. Read and validate the current keyset and its backend version/ETag.
2. For format 1, generate one format-2 candidate and call
   `replace_control(KEYSET_CONTROL_KEY, candidate.to_json(), observed_etag)`.
   If the condition loses, reload and use the remote winner; never store or
   use the losing candidate secret.
3. Cache only the winning keyset secret in the native keyring. If the keyring
   is unavailable, fail before Restic starts.
4. Initialize only `room-store/v1` with repository format 2 and the winning
   secret. Verify the resulting repository ID and format.
5. Bind the repository ID by conditionally replacing the exact current
   keyset generation. On a lost condition, reload and accept only the same
   repository ID and secret. A different ID, secret, physical binding, or
   unexpected generation is a hard conflict requiring explicit maintenance.

The backend's conditional replacement and readback semantics must be proven
for each provider before this handshake is enabled. The keyset JSON currently
contains the secret in the fixed control object, so this prerequisite alone
does not satisfy a design that requires secrecy from bucket readers.

Changing or removing the Restic password does not revoke a leaked password
from existing repository data. True revocation can require repository
re-encryption.
