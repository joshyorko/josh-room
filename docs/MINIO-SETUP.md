# Private MinIO setup

MinIO is an explicit, private S3-compatible backend. Set `minio.endpoint`,
`bucket`, `region`, `credential_profile`, and optionally `verify_tls`,
`ca_bundle`, and `path_style` (true by default) in the private config schema.
Only the opaque keyring profile is stored in configuration; access and secret
keys remain in the host OS Secret Service. Use `--backend minio` for commands.

The default `r2` backend remains unchanged and is the only backend that starts
Cloudflare OAuth/session acquisition. Josh Room verifies SHA-256 and byte size
itself; provider ETags are never treated as content integrity.
