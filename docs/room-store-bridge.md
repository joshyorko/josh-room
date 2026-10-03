# MinIO logical Room Store bridge

`josh_room.room_store_bridge` connects the isolated Restic save/restore engine
to one selected MinIO Dimension, the selected age material, and Dimension
catalog v3. It does not change the existing portable JAT Save path.

The public entry points are:

- `preview_room_store(instance, dimension, project_id, source,
  selected_material, *, snapshot_id="latest", components, rcc_runtime=None,
  required_components=())` reads the selected catalog, descriptor, and existing
  Restic tree. It never upgrades/binds the keyset, creates a repository,
  publishes data, captures RCC, or writes the marker. Its result reports
  `scanned_bytes`, `deleted_paths`, a stat signature, and a confirmation token
  when mass deletion is detected. `rcc_capture_pending` reports that Save will
  need to inspect/capture the root robot environment.
- `save_room_store(instance, dimension, project_id, source, selected_material,
  *, components, display_name=None, confirmation_token=None,
  on_progress=None, cancellation=None, rcc_runtime=None,
  required_components=())` performs an explicit native Save. `components` must
  contain the exact `rcc_environment`, `homebrew_recovery`, and
  `hauler_content` slots, or be `[]` when no external component was selected.
  The bridge resolves the root robot's RCCA after Restic initialization and
  repository binding, reusing its exact prior ref when source inputs match.
  Brew and Hauler references remain caller-supplied; a requested missing
  component fails closed. It never runs JAT `build`, composes a Hauler, or
  encrypts the workspace as one large JAT object.
- `hydrate_room_store(instance, dimension, project_id, destination,
  selected_material, *, snapshot_id="latest")` downloads and decrypts the
  selected small descriptor, corroborates every catalog-index field, opens
  only the already-bound Restic repository, restores through the sibling
  staging directory, then writes a fresh v3 stat marker.

The bridge derives the Restic locator as
`s3:<Dimension endpoint>/<bucket>/room-store/v1`. Provider credentials come
from `keyring.lookup()` and enter Restic only through its allowlisted AWS
environment (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional
`AWS_SESSION_TOKEN`, and region). MinIO and Restic preserve TLS verification
and the selected custom CA bundle. The Room Store secret is fetched/upgraded
through the Dimension-scoped keyset API; the operation writes it only to a
short-lived 0600 password file outside the workspace and removes that file on
exit. The Restic cache is stable across aliases of the same physical
endpoint/bucket and lives outside the workspace.

Restic is pinned to 0.19.1 and verified against the checked-in runtime manifest
and adjacent digest marker. An explicit Save can lazily install that pinned
binary beneath `instance.parent/josh-room-runtime`. Preview and Hydrate only
verify an already prepared runtime; they never install it. `JOSH_ROOM_RESTIC_EXE`
is accepted only when it resolves to that exact verified binary.

Publication order is: Restic backup, age-encrypt the canonical descriptor,
create/verify `objects/sha256/<digest>`, add one v3 logical snapshot record,
age-encrypt the catalog, and conditionally publish the catalog with the
observed ETag. Catalog conflicts remain rejected; applied writes with unknown
readback remain committed-but-unverified; unclassified transport outcomes
remain uncertain. No orphan receipt, unlock, prune, or garbage collection is
attempted. The workspace marker is updated only after a known catalog commit.

The marker stores `josh-room-stat-v1`, its workspace signature, and the capture
policy hash. The signature is a metadata/path baseline, not the legacy
content-hash fingerprint and not a measure of uploaded bytes. Result fields
name Restic `data_added` as `data_added_bytes`; `scanned_bytes` is the sum of
source file sizes.

The bridge delegates temporary-file and private-directory protection to the
native `private_paths` ACL/mode helpers, including Restic password and cache
validation. The bridge vertical uses a synthetic S3 backend and injected
keyring with real local Restic and age on Linux; it does not pass the live HTTPS
MinIO/provider gate or constitute a Windows bridge acceptance run. The root
integration lane retains those checks.
