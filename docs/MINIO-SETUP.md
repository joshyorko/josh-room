# Private MinIO setup

MinIO is an explicit, private S3-compatible backend. Set `minio.endpoint`,
`bucket`, `region`, `credential_profile`, and optionally `verify_tls`,
`ca_bundle`, and `path_style` (true by default) in the private config schema.
Only the opaque keyring profile is stored in configuration; access and secret
keys remain in the host OS Secret Service. Use `--backend minio` for commands.

The default `r2` backend remains unchanged and is the only backend that starts
Cloudflare OAuth/session acquisition. Josh Room verifies SHA-256 and byte size
itself; provider ETags are never treated as content integrity.

The secret-gated integration test uses exactly `JOSH_ROOM_MINIO_LIVE=1`,
`JOSH_ROOM_MINIO_ENDPOINT`, `JOSH_ROOM_MINIO_BUCKET`, and
`JOSH_ROOM_MINIO_PROFILE`. The preferred authority is a dedicated,
bucket-scoped `josh-room` service account stored in the existing OS
Secret Service/keyring profile. For ephemeral automation,
`JOSH_ROOM_RUNTIME_CREDENTIALS` may point to a mode-0600 JSON handoff.
Never use raw MinIO root or Kamal credentials. The test never creates users,
policies, or buckets; write/delete remains skipped until explicit authority is
approved and provisioned.
