# Private MinIO setup

MinIO is an explicit, private S3-compatible backend. In the normal native
flow choose **Connect Storage → MinIO**, enter the endpoint and masked access
and secret keys once, then choose one of the buckets visible to those
credentials. The selected bucket becomes a Dimension; additional buckets on
the same server reuse the Provider Connection.

The non-secret persisted model is:

```text
connections/<connection-id>
  provider: minio
  endpoint: <user-supplied endpoint>
  credential_profile: <opaque keyring reference>

dimensions/<dimension-id>
  connection_id: <connection-id>
  bucket: <selected bucket>
```

Access and secret keys are handed to the CLI over its bounded stdin channel and
remain in the host OS Secret Service. They never enter argv, logs, catalogs,
markers, or persisted Dimension JSON. If Secret Service is unavailable,
connection setup fails rather than writing plaintext credentials. Advanced
provider options include `region`, `verify_tls`, `ca_bundle`, and `path_style`
(`true` by default); normal HTTP endpoints do not require a TLS questionnaire.

Legacy top-level `minio`/Dimension records remain readable for compatibility;
new connections and Dimensions use the reusable connection model. Use an
explicit `--dimension` for CLI operations when more than one destination is
available.

The default `r2` backend remains unchanged and is the only backend that starts
Cloudflare OAuth/session acquisition. Josh Room verifies SHA-256 and byte size
itself; provider ETags are never treated as content integrity.

## Encryption domain and enrollment

The selected physical bucket is the Josh Room Dimension and its encryption
boundary. A fixed, versioned keyset is enrolled in that bucket; the operational
identity is unique to the bucket and recovery recipients are public-only in the
keyset. This is an intentional bucket-credential trust decision: a credential
that can read the keyset and snapshots can enroll and decrypt that Dimension.
Recovery private identity is never stored in the bucket.

MinIO keyset enrollment and catalog reads do not require Cloudflare. Provider
credentials and encryption material are separate, and neither private
identities nor credentials may appear in config, workspace files, logs,
receipts, argv, Git, or examples.

## Legacy migration

Legacy MinIO migration downloads and verifies the existing ciphertext, decrypts
the existing outer trusted envelope, and re-encrypts those exact envelope bytes
for the destination Dimension. It never rebuilds or restores JAT payloads; the
embedded `payload.haul.tar.zst` remains byte-identical. Production migration
requires explicit approval and is distinct from runtime, CI, and live-provider
gates.

This setup remains public-first and native-extension-aware. It does not add web
UI, webview, daemon, generic provider framework, automatic bucket administration,
JAT changes, or MinIO infrastructure changes.

The secret-gated integration test uses exactly `JOSH_ROOM_MINIO_LIVE=1`,
`JOSH_ROOM_MINIO_ENDPOINT`, `JOSH_ROOM_MINIO_BUCKET`, and
`JOSH_ROOM_MINIO_PROFILE`. The preferred authority is a dedicated,
bucket-scoped `josh-room` service account stored in the existing OS
Secret Service/keyring profile. For ephemeral automation,
`JOSH_ROOM_RUNTIME_CREDENTIALS` may point to a mode-0600 JSON handoff.
Never use raw MinIO root or Kamal credentials. The test never creates users,
policies, or buckets; write/delete remains skipped until explicit authority is
approved and provisioned.
