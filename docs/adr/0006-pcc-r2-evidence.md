# ADR 0006 — Opaque R2 session evidence and encrypted index events

Session evidence is separate from the mutable workspace catalog. Ciphertext objects use
`evidence/objects/sha256/<ciphertext-sha256>`. Independently encrypted index events use
`evidence/index/v1/<two-hex-content-shard>/<ciphertext-sha256>.age`. The shard is derived
only from the ciphertext digest; keys never contain repository, customer, project, profile,
device, session, title, or source-path identifiers.

Both families are validated separately from `objects/sha256/*`. Uploads are streamed from
the durable #8 ciphertext outbox, create-only (`If-None-Match: *`), and verified by an
independent bounded SHA-256 and exact-size read-back. ETags are completion tokens only and
are never treated as content digests. A duplicate is accepted only after the complete
ciphertext read-back matches. A mismatch is an immutable conflict. Index publication is
after object publication; an uploaded-but-unindexed object remains recoverable from #8.
Index discovery lists only the fixed index prefix, uses bounded `ListObjectsV2` pages, and
deduplicates keys so ordering and duplicate pages/notifications do not affect consumers.

Multipart failures abort when possible and retain the durable ciphertext so a later attempt
can safely restart. Incomplete multipart uploads should also be covered by an R2 lifecycle
rule (the current Cloudflare documentation says R2's default is seven days after
initiation). The configured part size must follow the provider's current minimum in live
configuration; fake-client tests may use smaller chunks for speed. Bucket Lock is optional:
it protects selected evidence from deletion but can also block operator deletion or
historical re-encryption until retention expires.

## Cloudflare baseline consulted (2026-09-22)

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/index.md), last
  updated 2026-07-31: `PutObject`, `HeadObject`, `GetObject`, `ListObjectsV2`, and multipart
  operations are implemented; `PutObject` lists conditional operations, while the
  `CompleteMultipartUpload` row does not list conditional headers.
- [Consistency model](https://developers.cloudflare.com/r2/reference/consistency/index.md),
  last updated 2026-04-30: reads, writes, deletes, and listings are strongly globally
  consistent; concurrent writes to one key are last-writer-wins.
- [Upload objects](https://developers.cloudflare.com/r2/objects/upload-objects/index.md),
  last updated 2026-07-29: multipart is resumable, parts are 5 MiB–5 GiB except the final
  part, and incomplete uploads should be aborted; the older issue URL
  `/r2/objects/multipart-objects/` now returns 404.
- [Limits](https://developers.cloudflare.com/r2/platform/limits/index.md), last updated
  2026-06-08: object keys are limited to 1,024 bytes, metadata to 8,192 bytes, multipart
  to 10,000 parts, and writes above one per second to one key may receive HTTP 429.
- [Event notifications](https://developers.cloudflare.com/r2/buckets/event-notifications/index.md),
  last updated 2026-04-21: object-create notifications cover `PutObject` and
  `CompleteMultipartUpload`, are delivered through Queues, and may be filtered by prefix;
  notifications are an accelerator, not a correctness dependency.
- [Object lifecycles](https://developers.cloudflare.com/r2/buckets/object-lifecycles/index.md),
  last updated 2026-04-21: incomplete multipart uploads default to seven-day expiry and
  lifecycle rules can explicitly abort them or expire selected prefixes.
- [Bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/index.md), last
  updated 2026-04-30: prefix rules can retain objects for a duration or indefinitely and
  take precedence over lifecycle deletion.
- [R2 S3 extensions](https://developers.cloudflare.com/r2/api/s3/extensions/index.md), last
  updated 2026-06-08: destination conditional headers exist for `CopyObject` as a beta
  extension; this implementation does not silently substitute that path for multipart
  completion. If a live endpoint rejects the multipart conditional request, the typed
  `multipart-conditional-create-unsupported` outcome is a NEEDS_SOL gate rather than a
  weakened immutability claim.
