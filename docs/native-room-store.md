# Native Room Store CLI behavior

## Save a MinIO Room

`snapshot create` uses the native Room Store for a selected MinIO Dimension when the CLI has that Dimension's encryption material. The CLI keeps the Python `operations.create_snapshot()` API on the portable JAT path.

Run `snapshot preview` to scan the workspace before saving:

```sh
josh-room snapshot preview my-room --source . --dimension archive --json
```

The preview reports scanned bytes, the previous and current entry counts, and any suspicious deletion confirmation token. For a workspace with `robot.yaml`, it sets `rcc_capture_pending`. Preview does not run RCC, initialize the Room Store repository, enroll a keyset, or install Restic.

Save with:

```sh
josh-room snapshot create my-room --source . --dimension archive --json
```

The first Save initializes the Dimension's Room Store repository and publishes a complete logical JAT. Later Saves reuse unchanged Restic data. A no-op Save returns `status: already-saved` and adds no recovery point. Human output says `Already saved — 0 bytes uploaded`.

If a Save reports `deletion-confirmation-required`, review a fresh preview and pass its token back with `--confirm-deletion`:

```sh
josh-room snapshot create my-room --source . --dimension archive --confirm-deletion TOKEN --json
```

Save installs the checksum-pinned Restic 0.19.1 runtime on demand. Preview does not install it. Preview and Enter require an already prepared, verified runtime.

When the source contains `robot.yaml`, Save captures the RCC Environment Artifact and stores its RCCA as a Room Store component. Save requires RCC v18.19.5 and fails closed if RCC cannot publish or export that artifact. The source workspace and its robot files also remain part of the complete Restic snapshot.

The CLI does not capture requested Hauler images yet. It fails closed when you pass `--image` or `--all-images`.

## Enter a saved Room

`enter` and `hydrate` inspect the selected catalog record's `payload_kind`:

- `room-store-v1` restores the logical JAT through Restic.
- A record without `payload_kind` uses the existing portable JAT and RCC restore path.
- Any other `payload_kind` fails closed.

For example:

```sh
josh-room enter my-room --dimension archive --snapshot latest --ide vscode
```

The Room Store adapter currently supports MinIO Dimensions. R2, local storage, and explicit portable JAT creation keep their existing paths.
