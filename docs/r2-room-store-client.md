# R2 Room Store material client

`josh_room.r2_room_store_material.R2RoomStoreAuthority` consumes the existing
Cloudflare OAuth session handoff. Construct it with the session ID, short-lived
Room Store capability, physical `roomStoreDomainId`, the protected temporary
age identity path, and the selected age recipients. The capability is held in
memory and sent only in the `Authorization: Bearer` header. Do not persist it
or include it in diagnostics.

`ensure_material()` reads or initializes the configured bucket's broker record.
The client creates a random 32-byte Restic password and age-encrypts a closed,
versioned payload to the supplied recipients. Its physical SHA-256 binding is
separate from the payload's UUID4 age-encryption domain. The broker record
contains only the opaque ciphertext and CAS metadata. If a first write races
or its response is ambiguous, the client reloads and decrypts the durable
winner before returning or caching it.

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

This module is a client seam only. It does not wire auth or CLI flows, configure
arbitrary buckets, deploy the worker, or claim live R2 acceptance.
