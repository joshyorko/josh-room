# Private R2 setup and evidence boundary

## Lifecycle boundary

Do not run `josh-room setup` inside the Room. Run it once on the Bluefin host
after host login, before `devpod up`. The command reads private bootstrap JSON
from stdin, imports sensitive values into the host OS Secret Service, and writes
only non-secret metadata to `$XDG_CONFIG_HOME/josh-room/config.json`.

The personal Room mounts that config read-only and mounts the host Secret
Service session bus for local containers. For the Kubernetes DevPod provider,
the host initialize hook reads the same keyring, uses the stored Cloudflare API
authority to mint six-hour bucket-scoped R2 credentials, and creates a narrowly
named per-workspace Kubernetes Secret. Bootstrap consumes it into pod-lifetime
`/tmp` files and deletes the Kubernetes Secret immediately. Daily `doctor`,
`enter`, `hydrate`, and `snapshot create` operations do not require DevPod
environment variables or credential prompts.

Host setup input therefore includes `cloudflare-api-token` and
`cloudflare-account-id` in addition to the parent R2 S3 credential and age
identity. Those authority values are stored only in the host keyring; neither
is written to `config.json`.

Josh Room's R2 data plane is the private Cloudflare S3-compatible API. It does
not use the Cloudflare REST object endpoint for snapshot transfer, and normal
operations never open a browser or ask for a password, API token, account ID,
AWS credential, or age key.

One-time setup imports credentials into the host OS Secret Service. Values must
come from a private provider or protected bootstrap process; they must not
appear in shell history, argv, files, logs, or the public repository. Private
XDG config contains only endpoint, bucket, separate R2/age keyring profiles,
public age recipients, workspace/JAT paths, defaults, and catalog-key metadata.
The container brokers only that config and the host Secret Service socket.

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
credential. Dedicated temporary-token rotation remains a follow-up. OAuth is a future authorization seam, not a
substitute claim for S3 large-object credentials.

Current source references:

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [Upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/)
