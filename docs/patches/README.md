# Disposable Rustic qualification patches

These patches target Rustic v0.11.4 commit
`6b96f976ff437a0528a4742358b172b32c3e3932` and its locked published
`rustic_core` 0.13.0 and `rustic_backend` 0.7.0 crates. They are qualification
source, not an adopted fork or distributed runtime. Product manifests and
Restic's default selection remain unchanged; Windows product selection stays
blocked for the official Rustic binary.

- `rustic_core-0.13.0-windows-readonly.patch` captures Go-compatible Windows
  writable/read-only mode values and restores the read-only attribute. It does
  not claim NTFS DACL, alternate stream or arbitrary Unix mode preservation.
- `rustic_core-0.13.0-verified-restore.patch` verifies each decrypted tree's
  SHA-256 before parsing, including roots and subtrees. On Windows it applies
  final read-only permission after timestamps, whose pinned implementation
  requires write access. Exact and replayed valid-tree unit cases accompany it.
- `rustic-0.11.4-json-nodes.patch` emits an exact full-ID snapshot/root/path
  header, native node records and a required final count. The explicit
  qualification adapter validates the complete stream before exposing rows or
  caching. Existing product callers use authenticated tree blobs.
- `rustic_backend-0.7.0-scoped-ca.patch` consumes a bounded `cacert` option,
  decodes exactly one real X.509 trust anchor per strictly delimited PEM block,
  rejects nested/non-certificate/malformed input, augments normal TLS trust and
  attaches its transport to one OpenDAL operator. It retains chain and hostname
  verification and does not modify system trust or a global transport.

```sh
uv run python scripts/build_rustic_candidate.py --destination /tmp/owned-rustic-candidate --verify-tests
```

The builder verifies archive checksums against the upstream lock, preserves
package versions/checksums, records compiler and binary identities, and checks
a complete prepared-source manifest plus actual upstream HEAD before and after
builds and on every resume. A changed preparation requires a fresh destination.
Native candidates are built in disposable Linux and Windows runners; only
receipts and review packaging are retained, not candidate engine binaries.

See [current qualification evidence](../rustic-candidate-qualification-2026-10-17.md)
for observed results and unrun gates. Historical core-only Windows proof is
separate from full native CLI and managed-consumer qualification. The generated
TLS/S3 fixture does not verify AWS signatures or establish live R2/MinIO
acceptance. No upstream issue, upstream PR or maintained fork was created.
