# Issue 50 explicit legacy Dimension adoption

The 0.1.21 failure occurred during planning, after source authorization and
recovery preparation. The ordinary catalog reader supplied the new local
Dimension ID to a valid legacy v2 catalog carrying its historical ID, causing
`catalog Dimension mismatch`. Credential replacement and recovery-key
regeneration are not remedies for that identity-mapping failure.

## Bounded correction

Ordinary catalog reads and `Catalog.from_body` retain strict Dimension/domain
validation. Only explicit MinIO legacy migration uses the adoption reader. It
checks the selected backend's endpoint, bucket, catalog key, and configured ID,
then uses the existing authenticated HEAD/conditional GET and age decryption.
This proves the selected catalog resource was read; legacy metadata does not
provide independent historical endpoint provenance, and none is invented.

The reader preserves the embedded source ID. Domain-bound foreign catalogs and
known source IDs assigned to another storage binding are rejected. The preview
discloses old/new IDs and the selected physical bucket/endpoint. Confirmation is
bound to that storage scope, configured expected domain, complete source catalog,
and ETag. Changing those inputs cannot reuse a previous approval, even when an
identical catalog/ETag exists in another bucket.

The migration journal retains source ID and binding. Interrupted adoption can
resume only with matching recorded authority. Catalog publication remains
conditional; the destination logical ID changes only at the approved cutover.
Exact encrypted-envelope contents/JAT payloads remain unchanged. Existing source
and recovery backup files are retained.

## Verification

- Real-age mismatch regression failed before the change and passed afterward.
- Focused tests cover preview without mutation, wrong backend/bucket/domain,
  configured-source conflicts, stale approval, retained provenance, recovery
  decryption of the exact envelope, and interrupted resume.
- Full Python suite: 560 passed, 6 skipped. Node 22: 228 passed, 2 skipped.
- Ruff, source/template/controller parity, builds, packaged secret scan and
  diff checks passed. The tracked-source scan has the existing deliberately
  synthetic redaction-test false positive; no scanner rule was weakened.
- Independent bounded review found no P1/P2 issue, including the configured
  expected-domain binding guard.

Installed realistic-fixture and published-asset acceptance are separate gates.
No live storage write, migration, key replacement, metadata rename, JAT payload
mutation, or infrastructure change was performed during diagnosis.

Skipped gates remain private first-run, live MinIO/R2 storage, the unavailable
full JAT/Hauler vertical, registry/tool-gated secure Room, opt-in large streaming,
and the two optional managed-runner Node fixtures. The separately reported R2
hydrate cancellation was not attributed to this deterministic planning failure
and was not diagnosed by this patch.
