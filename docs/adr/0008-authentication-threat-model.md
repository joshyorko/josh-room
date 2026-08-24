# ADR 0008 — Non-interactive host-keyring authentication

Normal `enter`, `hydrate`, and `snapshot create` operations do not invoke a
Cloudflare login or ask for credentials. A bootstrap/setup action imports a
known-good dedicated bucket-scoped R2 S3 credential into the host OS
Secret Service/keyring under an opaque Josh Room profile. The private XDG file
contains only endpoint, bucket, profile, and temporary-credential preference.

The container receives non-secret config and, on Linux, only the host session
bus socket needed for Secret Service lookup. Credential values remain in
operation memory and never enter the image, repository, logs, argv, receipts,
or catalog. Age identities remain separate; offline recovery uses its own
identity.

The MVP does not implement OAuth. Reserve `josh-room login/setup` for a future
Cloudflare OAuth Authorization Code + PKCE flow; OAuth is authorization UX,
while large snapshot transfer remains the private R2 S3-compatible data plane.
