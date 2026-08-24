# ADR 0005 — R2 immutable objects and conditional catalog

Production R2 uses a private bucket and opaque immutable keys of the form
`objects/sha256/<ciphertext-sha256>`. Upload is create-only, followed by read-back
digest/size verification and conditional encrypted-catalog update. A stale
writer fails explicitly. The local filesystem backend implements the same
logical contract for deterministic offline tests; it is not a plugin framework.

The implementation uses one boto3 S3-compatible transport with configured
timeouts and bounded retries. It performs conditional immutable writes,
multipart abort cleanup, streamed downloads, HEAD/read-back, and conditional
catalog ETag publication. Generic fake-client tests are not R2 proof; the live
private-bucket gate remains separate.

Cloudflare's current compatibility documentation lists `CompleteMultipartUpload`
but does not enumerate conditional headers for that row. Josh Room therefore
keeps a real secret-gated multipart acceptance test. The checkpoint test used a
6 MiB object with a 5 MiB first part and proved conditional completion plus
streamed digest/size read-back against private R2.
