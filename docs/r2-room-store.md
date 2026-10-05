# R2 Room Store material broker

The worker brokers one configured physical R2 bucket, identified by its
Cloudflare account ID and `R2_BUCKET`. The caller cannot select either value.
The current worker configuration does not provide arbitrary bucket access.
The broker derives a stable `roomStoreDomainId` from that physical identity;
the same bucket aliases share the same Durable Object record.

The local client generates the Room Store password and encrypts it to the
configured age recipients. The worker persists only the resulting ciphertext,
the domain ID, keyset generation, and optional Restic repository ID. It never
generates or receives the plaintext password.

## Session handoff

An owner-authorized R2 session's existing one-time `GET /session/{sessionId}`
response retains its existing fields and adds:

```json
{
  "roomStoreDomainId": "<64 lowercase hex characters>",
  "roomStoreCapability": "<opaque bearer value>",
  "roomStoreCapabilityExpiresIn": 600
}
```

The capability is returned only for R2-purpose Durable Object sessions. It is
valid for at most 600 seconds and grants read, create-once, and bind-once rights
for the configured physical bucket. Send it only as `Authorization: Bearer …`;
never put it in a URL, log, receipt, or example. The capability handoff and
Room Store API responses use `Cache-Control: no-store`. After the status read,
the session Durable Object retains only the capability hash, approved R2
purpose, configured account and bucket, and expiry. Temporary R2 credentials
and the age private identity are removed from Durable Object storage. The
original session status remains consumed.

The legacy KV OAuth route keeps its existing response. It does not issue a
Room Store capability; its Room Store endpoints return
`room_store_durable_authority_unavailable` (503) because KV does not provide
the required transactional create-once contract.

## Material API

Both endpoints require the capability header. `sessionId` is the authorized
OAuth session ID, not a credential.

`GET /session/{sessionId}/room-store/material` returns
`200 {"status":"ready","material":…}` or
`404 {"error":"room_store_material_missing"}`.

`POST /session/{sessionId}/room-store/material` accepts exactly these fields:

```json
{
  "format": "josh-room-r2-room-store-material",
  "version": 1,
  "domainId": "<roomStoreDomainId from the session>",
  "keysetGeneration": 1,
  "ciphertext": "<unpadded base64url age ciphertext>"
}
```

The ciphertext must be 1–16,384 base64url characters. The worker constructs a
canonical record with `repositoryId: null`. A successful first write returns
`201 {"status":"created","material":…}`. An exact retry returns
`200 {"status":"existing","material":…}`. A different candidate cannot
replace the winning ciphertext and returns `409
{"error":"room_store_material_conflict"}`. Writes use the configured-bucket
Durable Object's transaction and readback.

## Repository binding

`POST /session/{sessionId}/room-store/repository` accepts exactly:

```json
{
  "repositoryId": "<64 lowercase hex characters>",
  "expectedGeneration": 1
}
```

The first matching bind returns `201` and advances `keysetGeneration` by one.
A retry with the same repository ID and the original or winning expected
generation returns `200` with the winning generation. A different repository
ID returns `409 {"error":"room_store_repository_conflict"}`. A stale or
mismatched generation returns `409
{"error":"room_store_generation_conflict"}`. Binding cannot replace material
or change the domain.

## Fail-closed errors

All API errors use a JSON `error` string. `room_store_capability_required`
(401), `room_store_capability_invalid` (403), `room_store_scope_mismatch`
(403), and `room_store_capability_expired` (404) reject unauthorized or stale
capabilities. `room_store_invalid_material` and
`room_store_invalid_repository` (400) reject malformed or open-ended payloads.
`room_store_material_write_unverified` and
`room_store_repository_write_unverified` (503) report failed readback.
`room_store_material_missing` (404) rejects binding before material creation.
`room_store_not_found` (404) rejects unknown operations.
`room_store_durable_authority_unavailable` (503) identifies the legacy KV
route's unsupported persistence boundary. `room_store_method_not_allowed`
(405) rejects unsupported HTTP methods.

This contract does not enable arbitrary R2 buckets, R2 deployment, Cloudflare
configuration, or live R2 acceptance. A live R2 gate remains skipped until the
required external authority is available and deployment is separately
authorized.
