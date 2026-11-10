# Experimental portable Room Store authority

This route runs the same `RoomStore` Durable Object class on Cloudflare DO and
self-hosted celld. Snapshot objects, catalogs and Restic packs stay on the
existing R2/MinIO data path. The route is opt-in. Existing MinIO Dimensions
continue to work without another service until explicitly migrated.

Read the [decision and deletion gate](adr/portable-room-store-authority.md).
No real Dimension adoption, production deployment, release or merge is
authorized by the implementation PR.

## Disposable verification on Linux

Use the repository Python environment and Node. The fleet test starts only
loopback listeners, generates disposable credentials, creates a disposable
MinIO directory and buckets, kills only its own child processes, and removes
its temporary files. It does not connect to a configured user bucket.

Required tools are age/age-keygen, Restic, Node, esbuild 0.25.12, Miniflare
4.20260730.0 (with the checked-in patched dependency overrides), celld 0.6.3 and MinIO built from
`RELEASE.2025-04-22T22-12-26Z`. Resolve the celld release digest from the ADR.
Do not use `celld dev` as persistence evidence.

Run inside the development environment with those tools available:

```sh
node --test worker/test/*.test.js
uv run pytest tests/test_r2_room_store_material.py tests/test_portable_room_store.py tests/test_room_store_material.py
ROOM_STORE_TEST_TOOLS=/path/to/task-local/node-tools node scripts/portable-room-store/workerd.mjs
uv run python scripts/portable-room-store/fleet.py \
  --celld /path/to/celld --minio /path/to/minio --esbuild /path/to/esbuild
```

`ROOM_STORE_TEST_TOOLS` names a directory with a package.json and the pinned
Miniflare/esbuild modules. The workerd lane executes the shared contract and
injects a corrupt record through a test-only subclass. The fleet lane uses the
same contract, two storage-backed nodes, SIGKILL and cache removal, cold restore,
real age encryption, an existing Restic repository, conditional keyset switching,
sealed rollback, incremental backup, restore and `restic check`.
The migration lane injects loopback transport and bypasses local keyring cache
writes; it does not qualify production HTTPS or the OS keyring.

## Service security and enrollment

celld needs a separate control bucket and fleet credentials. Snapshot-bucket
readers must not receive fleet credentials or the capability signing key.
Keep peer/operator listeners on trusted private interfaces; some operator
routes are unauthenticated. Terminate client HTTPS through a trusted ingress.
Do not expose internal listeners to clients or the internet.

`worker/celld/wrangler.jsonc` is disabled by default. A private deployment config
must set `ROOM_STORE_EXPERIMENTAL` to `enabled`, `ROOM_STORE_DOMAIN_ID` to the
SHA-256 of Josh Room's canonical physical MinIO bucket identity, and
`ROOM_STORE_SIGNING_KEY` to a random canonical base64url 32-byte key. Keep this
config private. celld's deployed bundle contains these variables, so control
bucket access is operator-level trust. Do not commit the private config.
Do not change the script/class/name after adoption; those names select state.

The local Dimension configuration accepts two non-secret options,
`room_store_authority_endpoint` (HTTPS origin only) and
`room_store_authority_profile` (OS keyring profile). Configuring these alone
does not migrate data or select a new writer. The versioned bucket marker
selects the route and must match the local authority URL and physical domain.

Issue capabilities from a private operator environment. The signing key is
read from a private file, not argv. Tokens are scoped to one physical domain,
read or write access, and at most one hour. The default lifetime is ten minutes.
Key rotation revokes outstanding tokens. No Cloudflare login is involved.

```sh
uv run python scripts/portable-room-store/issue-capability.py \
  --signing-key-file /private/operator/signing-key \
  --domain PHYSICAL_DOMAIN_SHA256 --access write --expires-in 600 \
  --output /private/operator/new-capability --keyring-profile lab-authority
```

Use the real computed domain instead of the placeholder. The output file is
created exclusively with private permissions. Remove it after secure transfer.
The consumer requires its own keyring capability and existing operational age
identity; signing keys and fleet storage credentials are never consumer inputs.
Expired or unavailable authority credentials fail closed. Normal legacy MinIO
headless behavior is preserved; migrated clients require the enrolled keyring.

## Migration and rollback

Stop every client before switching authority. Test these steps on disposable
Dimensions first. Real data requires separate owner approval.

1. Verify the existing format-2 keyset, recovery recipient and private operational
   identity can decrypt existing data. Keep the current supported software
   available for rollback. Do not rotate the repository password.
2. Configure the private service and local non-secret options. Enroll a scoped
   write capability in the OS keyring. Run `manage.py plan --dimension lab`.
3. Run `manage.py migrate --dimension lab --execute --quiesced`. The tool uploads
   only an age-encrypted password record, verifies the winning secret/domain/
   repository/generation, and conditionally replaces the versioned keyset with
   an authority marker. A concurrent legacy mutation prevents publication.
4. Verify read-only list/restore, then incremental Save through the packaged
   consumer before resuming other clients. Old clients reject the marker;
   an authority outage never silently activates the legacy writer.
5. For rollback, quiesce clients again and run
   `manage.py rollback --dimension lab --execute --quiesced`. It permanently
   seals the DO writer before reading the latest material and conditionally
   restoring format 2. Verify restore before resuming legacy clients.

Invoke the tool through `uv run python scripts/portable-room-store/manage.py`.
The plan reads state without initializing the authority. Writes are explicit.
Interrupted marker publication requires fresh readback; never manually copy an
old format-2 keyset over a newer marker. A sealed authority remains sealed.
Do not reuse it for another migration without a reviewed namespace transition.

Migration preserves the existing bucket enrollment trust, including the existing
operational identity in the S3 enrollment record. Neither private operational
nor recovery identities are sent to the DO. The marker removes the plaintext
Restic password from the current keyset, but historical bucket versions/readers
can retain it. This custody change does not revoke previously disclosed secrets.

## Evidence and limits

Source and runtime receipts are recorded in
[the execution trail](adr/portable-room-store-execution.md). Live Cloudflare,
TLS ingress, installed VSIX, native Windows, real keyring, default celld fleet
partition tolerance, production MinIO qualification and real-data migration are
separate gates. The baseline format-2 authority remains only during the explicit
compatibility window. Its deletion requires the ADR's migration and rollback
exit criteria.
