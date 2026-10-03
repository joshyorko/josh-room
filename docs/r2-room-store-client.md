# R2 Room Store material client

`josh_room.r2_room_store_material.R2RoomStoreAuthority` consumes the existing
Cloudflare OAuth session handoff. Construct it with the session ID, short-lived
Room Store capability, physical `roomStoreDomainId`, the protected temporary
age identity path, and the selected age recipients. The capability is held in
memory and sent only in the `Authorization: Bearer` header. Do not persist it
or include it in diagnostics.

`read_material_details()` performs a GET-only read and fails with
`room_store_material_missing` without POSTing when no record exists.
`ensure_material()` reads or initializes the configured bucket's broker record.
The client creates a random 32-byte Restic password and age-encrypts a closed,
versioned payload to the supplied recipients. Its physical SHA-256 binding is
separate from the payload's UUID4 age-encryption domain. The broker record
contains only the opaque ciphertext and CAS metadata. If a first write races
or its response is ambiguous, the client reloads and decrypts the durable
winner before returning or caching it.

`ensure_material_details()` returns the compatible `RoomStoreKeyset` together
with the decrypted UUID4 encryption domain and age key generation. Keep these
identities separate: the physical SHA-256 comes from OAuth scope, while the
UUID4 comes only from the age-encrypted winner.

The returned `RoomStoreKeyset` carries the winning password, physical binding,
outer metadata generation, and optional repository ID. The password is cached
only in the native OS keyring under its encryption-domain and metadata
generation scope. `bind_repository(repository_id)` performs the broker's
one-time CAS binding after Restic initialization and caches the unchanged
password under the winning generation.

The endpoint must use HTTPS. Tests may inject an explicit transport callable;
this does not enable public HTTP. Expired capabilities fail closed. A caller
may obtain a fresh OAuth session through its existing authorized renewal flow;
the client never starts interactive Cloudflare login. The legacy KV backend
returns `room_store_durable_authority_unavailable` and is unsupported.

The client does not wire CLI flows, configure arbitrary buckets, deploy the
worker, or claim live R2 acceptance.

`auth.create_r2_room_store_authority(dimension)` is the private-session factory.
It requires an existing connected R2 session with a valid capability, confirms
that the selected Dimension endpoint and bucket match the OAuth-bound values,
and returns the Room Store password plus an `EncryptionMaterial` built from
the same broker UUID4 and the authenticated age identity. It never starts
OAuth. Its `allow_initialize` option defaults to false and selects the GET-only
read path. Only an explicit true value allows create-once material writes. The
capability is stored only in the private runtime session metadata; when its
separate 10-minute expiry passes, it is removed while still-valid R2 credentials
remain connected.
