# Josh Room

Your development workspaces, on demand.

Josh Room gives logical names to encrypted development-workspace snapshots and
restores them into a local or remote development workspace.

```bash
josh-room enter hive
```

Josh's All the Things provides the separate Hauler capture and restore
substrate. Josh Room provides project identity, age encryption, private R2
storage, catalog resolution, safe hydration, and IDE entry. Room of Requirement
is an optional golden host; it is not part of the Josh Room runtime contract.

## Standalone VSIX

On a plain supported Linux amd64 VS Code or VS Code Insiders host, install the
released VSIX directly:

```bash
code-insiders --install-extension /path/to/josh-room-0.1.10.vsix
```

The extension acquires its pinned RCC binary and JAT Environment Artifact under
VS Code global storage. The JAT artifact already contains Hauler, installed by
JAT's RCC `rccPostInstall` hook before freeze. No Homebrew, host RCC, host
Hauler, Action Server, global Josh Room CLI, or Room of Requirement image is
required. The standalone VSIX requires no host enrollment.

## Optional Room of Requirement golden host

The repository still offers a pre-optimized Room of Requirement entrypoint for
development and comparison. This path is optional and is not required by the
VSIX:

```bash
devpod up github.com/joshyorko/josh-room --ide vscode-insiders
```

DevPod clones the repository, discovers the root `.devcontainer`, starts the
digest-pinned golden image, copies the extension into the user-scoped VS Code
server directory, and opens VS Code Insiders with the Josh Room Activity Bar
view. The bootstrap does not install a global Josh Room CLI or runtime tools.

The same configuration is published for standard Dev Container template
consumers:

```bash
devcontainer templates apply \
  --template ghcr.io/joshyorko/josh-room/templates/room:0.1.0
```

The OCI template is an additional standards-based distribution path. DevPod
does not require users to apply it when opening this repository directly.

## What it is not

Josh Room is not an image factory, backup daemon, source-control system, remote
development provider, agent harness, package runtime, scheduler, or multi-user
platform. It may run in a published Room of Requirement image, but the
standalone VSIX does not consume or require that image.

## Synthetic local demo

Install the CLI and point it at a checked-out Josh's All the Things tree:

```bash
uv tool install -e .
export JOSH_ROOM_JAT_ROOT=/path/to/josh-all-the-things
export JOSH_ROOM_IDENTITY=/path/to/synthetic-daily.agekey
export JOSH_ROOM_RECIPIENTS='age1daily...,age1recovery...'

josh-room doctor --backend local
josh-room snapshot create demo-project --source ./examples/demo-project
josh-room hydrate demo-project --destination ./demo-restored --ide terminal
```

Never commit an age identity, R2 credential, real catalog, or snapshot.

## Private instance

The Cloudflare Worker authenticates Josh in the browser and returns only a
short-lived, bucket-scoped runtime session to the disposable Room. Persistent
R2 authority and the operational age identity do not live on the host or in
the repository. See [private R2 setup](docs/R2-SETUP.md).

Production snapshots require independent daily-use and offline-recovery age
recipients. Every recipient must decrypt independently. The private R2 bucket
remains private; object keys contain only ciphertext SHA-256 digests.

## Josh's golden path

### Room use

The optional golden-host template copies the native extension without running a
JAT workload. On a plain host, the packaged extension owns RCC bootstrap and
the JAT artifact owns Hauler.

Daily use is:

```bash
josh-room doctor --backend r2
josh-room enter hive
```

Cloudflare R2 uses the existing OAuth flow: the first R2 operation opens a local
browser authorization and reuses its short-lived session until expiry. MinIO
uses a user-supplied endpoint and masked credentials entered once through the
native Connect Storage flow. A reusable Provider Connection owns that authority;
each Dimension is one bucket-backed encrypted catalog, containing Rooms and
their immutable JAT history. R2 and VS Code Insiders are the defaults.
Extension-mode `doctor` reports the managed RCC/JAT runtime and controller
state; standalone CLI mode may report optional host integrations separately.
`enter` discovers logical project names from the encrypted R2 or MinIO catalog
selected for the Room, hydrates safely, then launches VS Code Insiders.

The bundled extension provides a native Rooms TreeView, toolbar actions,
per-Room Enter/Serve/Delete actions, Docker-style operation progress, and a
timestamped Josh Room log channel. Save, Enter, Delete, and JAT
Build/Restore/Serve show their real auth, encryption, R2 transfer, and
RCC/Hauler stages with percentages where the underlying operation exposes real
measurements. A visible pulse travels through the status-bar progress track;
indeterminate JAT phases use a moving scanner instead of a fake percentage. The
current Room quietly changes to `Needs save` after workspace edits and returns
to `Saved` when those contents are reverted to the captured baseline. Clicking
its status-bar Save indicator opens the existing Save flow.
Repositories do not need a Josh Room `.vscode/tasks.json`.
`Josh: Save Room` first opens a native folder picker, then
a Quick Pick of saved Rooms plus “Create a new Room”. The selected folder is
the snapshot source and receives only the non-secret `.josh-room.json` identity
marker after a successful save. Selecting an existing Room appends an immutable
snapshot and advances its `latest` pointer; it does not create another logical
Room. `Josh: Enter Room` reopens an already materialized `(Dimension, Room)` on
the device without downloading it again. A missing Room is restored beneath
the configured Josh Room workspace root; selecting another historical JAT reuses
that same folder after an explicit clean/dirty-state confirmation.
Save can include all tagged local OCI images in the JAT haul. `Josh: Serve Room
Images` decrypts the chosen snapshot only into private runtime staging and runs
JAT's foreground Hauler registry on `127.0.0.1:5000`; stopping the terminal
removes the temporary haul and registry store.

A separate `JAT Tools` view preserves one-off automation outside the Room/storage
workflow: pack any folder into a portable haul, restore a JAT-compatible haul
into a new destination, or serve any Hauler haul as a foreground registry. These
commands use the same typed RCC Build/Restore/Serve tasks but require no Room,
catalog, OAuth, or R2 operation.
The single trash action lists the selected Room's snapshots and requires exact
modal confirmation before deleting one. Removing Latest safely promotes the
newest remaining snapshot; deleting the final snapshot removes the now-empty
Room. Objects remain protected whenever another snapshot references them.
Storage, encryption, and hydration remain owned by the CLI.

## Commands

```text
josh-room doctor [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room projects list [--backend local|r2] [--json]
josh-room rooms remove <project> [--backend local|r2] [--json]
josh-room snapshots list <project> [--backend local|r2] [--json]
josh-room snapshots remove <project> <snapshot> [--backend local|r2] [--json]
josh-room snapshot create <project> [--source <path>] [--image <ref> ... | --all-images] [--backend local|r2] [--json]
josh-room hydrate <project> --destination <path> [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room enter [<project>] [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room serve <project> [--snapshot latest|<id>] [--backend local|r2|minio] [--dimension <id>] [--json]
josh-room provider connection list [--json]
josh-room provider connection create|update|reconnect|disconnect [options] [--json]
josh-room provider bucket list|create|check [options] [--json]
josh-room dimensions list [--dimension <id>] [--with-hierarchy] [--json]
josh-room auth start|wait|cancel|status [options] [--json]
josh-room jat build --source <path> --output <haul.tar.zst> [--image <ref> ... | --all-images] [--json]
josh-room jat restore --haul <haul.tar.zst> --destination <path> [--json]
josh-room jat serve --haul <haul.tar.zst> [--json]
```

`provider connection` and `provider bucket` are the canonical storage boundary.
The older `connections` and `buckets` command families remain compatibility
aliases only. `enter` resolves the selected Dimension and Room, reuses a
corroborated device-local materialization when present, or hydrates into the
configured workspace root and launches the selected IDE only after durable
completion.

## Evidence and current limits

The test suite includes real synthetic JAT/Hauler/age local hydration and a
secret-gated private R2 create/read-back/catalog/fresh-hydrate acceptance test.
Generic S3 tests do not substitute for live R2 evidence.

The optional golden-host readiness gate resolves the `secure` Room image to an
immutable digest and runs a clean non-root Podman smoke proving its image
contract. That evidence does not substitute for the standalone VSIX gate.

This remains a review checkpoint, not the final portability release. The standalone clean-container
VSIX acceptance and dedicated short-lived R2 credential rotation remain open.
Josh Room consumes the immutable JAT RCC v18.19.2 Environment Artifact during
extension bootstrap, and Save requests JAT `rcc_environment=auto` with optional
typed receipt metadata. RCC/JAT retain ownership of acquisition and production
details. Actions Runtime, Hive projections, OpenAI uploads, periodic capture,
and garbage collection remain deliberately deferred.

Cloudflare OAuth uses the official hosted Josh Room authority by default.
`JOSH_ROOM_AUTH_URL` is an optional override for self-hosted or custom
deployments; the public repository contains no personal account configuration.

See [architecture](docs/architecture.md), [ADRs](docs/adr), and
[deferred integrations](docs/DEFERRED-INTEGRATIONS.md).
