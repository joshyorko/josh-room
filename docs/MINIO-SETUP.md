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

The secret-gated integration test uses exactly `JOSH_ROOM_MINIO_LIVE=1`,
`JOSH_ROOM_MINIO_ENDPOINT`, `JOSH_ROOM_MINIO_BUCKET`, and
`JOSH_ROOM_MINIO_PROFILE`. The preferred authority is a dedicated,
bucket-scoped `josh-room` service account stored in the existing OS
Secret Service/keyring profile. For ephemeral automation,
`JOSH_ROOM_RUNTIME_CREDENTIALS` may point to a mode-0600 JSON handoff.
Never use raw MinIO root or Kamal credentials. The test never creates users,
policies, or buckets; write/delete remains skipped until explicit authority is
approved and provisioned.
