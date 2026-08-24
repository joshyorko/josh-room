# ADR 0003 — Snapshot envelope and catalog

Each snapshot is an age-encrypted tar envelope containing exactly
`manifest.json` and `payload.haul.tar.zst`. The payload is already compressed and
is not recompressed by the outer tar. The manifest is versioned and records the
logical project, stable snapshot ID, UTC creation time, plaintext payload digest
and size, producer identity, and optional source state. The catalog is a
separate encrypted v1 document mapping display projects to explicit `latest`
snapshot references. Latest is never inferred from object listing order.

`snapshot_id` identifies an immutable snapshot event and is independent from the
payload content digest. Repeated capture of identical content therefore creates
distinct events while retaining the same payload SHA-256. Source provenance is
recorded only when Git can prove it; non-Git sources use an empty source object
rather than claiming a clean state.
