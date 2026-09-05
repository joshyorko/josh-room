# Private R2 setup and evidence boundary

## Lifecycle boundary

Normal use requires no host setup, host keyring, private XDG mount, Kubernetes
Secret, DevPod environment variable, parent R2 credential, or local age
identity. The first R2 operation starts the Josh Room Authority browser OAuth
flow. After the owner allowlist succeeds, the Worker returns a short-lived,
bucket-scoped R2 session plus the operational age material to mode-0600 files
inside the disposable Room's runtime directory. Later CLI processes reuse that
session until expiry; a fresh Room authenticates again.

Josh Room's R2 data plane is the private Cloudflare S3-compatible API. It does
not use the Cloudflare REST object endpoint for snapshot transfer, and normal
operations never ask Josh to copy a password, API token, account ID, AWS
credential, or age key. Snapshot payloads still transfer directly over R2's
S3-compatible API and never pass through the Worker.

Use a dedicated bucket-scoped credential and separate private Josh Room bucket.
The fixed encrypted catalog key is `catalog.jroom.age`; immutable objects use
`objects/sha256/<ciphertext-digest>`. Upload uses conditional create-only
semantics, multipart abort cleanup, streamed read-back, and digest/size
verification. Catalog updates use conditional ETag writes and record
unreferenced-object receipts when publication fails.

Generic S3 behavior is covered by synthetic fake-client tests. A live gate must
separately prove private R2 create, read-back, conditional catalog update, and
fresh-state hydration. At this checkpoint that live gate passed against the
separately provisioned private Josh Room bucket using the authorized bootstrap
credential. The current production path uses OAuth for human authorization and
Cloudflare temporary credentials for the S3-compatible large-object data plane.
The official hosted Josh Room authority is used by default; set
`JOSH_ROOM_AUTH_URL` only when overriding it for a self-hosted or custom
deployment.

R2 is a concrete provider backend, and each physical R2 bucket / Dimension is
one encryption domain. Cloudflare remains the R2-only authority for OAuth and
bucket-scoped R2 sessions; Cloudflare is not a generic encryption authority for
MinIO. Provider credentials and encryption material remain separate, with no
private identities or credentials in config, workspace files, logs, receipts,
argv, Git, or examples.

Runtime, CI, and live gates are distinct. This public-first, native
extension-aware setup does not add web UI, webview, daemon, generic provider
framework, automatic bucket administration, JAT changes, or MinIO
infrastructure changes.

Current source references:

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [Upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/)
