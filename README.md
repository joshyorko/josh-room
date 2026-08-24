# Josh Room

Your development workspaces, on demand.

Josh Room gives logical names to encrypted development-workspace snapshots and
restores them into a Room of Requirement environment.

```bash
josh-room enter hive
```

Room of Requirement provides the development environment. Josh's All the
Things provides Hauler capture and restore. Josh Room provides project
identity, age encryption, private R2 storage, catalog resolution, safe
hydration, and IDE entry.

## Open the Room with DevPod

The repository itself is the personal Room entrypoint; no host enrollment is
required:

```bash
devpod up github.com/joshyorko/josh-room --ide vscode-insiders
```

DevPod clones the repository, discovers the root `.devcontainer`, starts the
Room of Requirement secure image, runs the capability-only bootstrap, and
opens VS Code Insiders with `Josh: Save Room`, `Josh: Enter Room`, and
`Josh: Remove Room` available under `Tasks: Run Task`.

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
platform. It consumes published Room of Requirement images; it does not contain
their Dockerfiles, Brewfiles, image CI, or maintenance machinery.

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

The personal Room template bootstraps `age`, `uv`, stable RCC, Action Server,
Hauler, JAT, Josh Room, and its small VS Code command bridge without running a
JAT workload.

Daily use is:

```bash
josh-room doctor --backend r2
josh-room enter hive
```

The first Save or Enter operation in a disposable Room opens Cloudflare OAuth.
The resulting short-lived session is reused by later operations in that Room
until expiry. R2 and VS Code Insiders are the defaults. `doctor` fails with remediation when
age, Hauler, RCC, JAT, the daily identity, R2, the encrypted catalog, or the IDE
is unavailable. `enter` discovers logical project names from the encrypted R2
catalog, hydrates safely, then launches VS Code Insiders.

The bundled command bridge provides Save, Enter, Remove, and Serve Images through VS Code's
task provider in every opened folder; repositories do not need a Josh Room
`.vscode/tasks.json`. `Josh: Save Room` first opens a native folder picker, then
a Quick Pick of saved Rooms plus “Create a new Room”. The selected folder is
the snapshot source and receives only the non-secret `.josh-room.json` identity
marker after a successful save. Selecting an existing Room appends an immutable
snapshot and advances its `latest` pointer; it does not create another logical
Room. `Josh: Enter Room` hydrates beside the clean bootstrap workspace and
switches the current VS Code window to the restored root.
Save can include all tagged local OCI images in the JAT haul. `Josh: Serve Room
Images` decrypts the chosen snapshot only into private runtime staging and runs
JAT's foreground Hauler registry on `127.0.0.1:5000`; stopping the terminal
removes the temporary haul and registry store.
`Josh: Remove Room` requires explicit modal confirmation, removes the encrypted
catalog entry conditionally, then deletes only objects no remaining Room
references. Storage, encryption, and hydration remain owned by the CLI.

## Commands

```text
josh-room doctor [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room projects list [--backend local|r2] [--json]
josh-room rooms remove <project> [--backend local|r2] [--json]
josh-room snapshots list <project> [--backend local|r2] [--json]
josh-room snapshot create <project> [--source <path>] [--image <ref> ... | --all-images] [--backend local|r2] [--json]
josh-room hydrate <project> --destination <path> [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room enter [<project>] [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room serve <project> [--snapshot latest|<id>] [--backend local|r2] [--json]
```

`enter` lists logical project display names, resolves the encrypted catalog,
hydrates into an adjacent owned stage, atomically promotes the workspace, and
launches the selected IDE only after durable completion.

## Evidence and current limits

The test suite includes real synthetic JAT/Hauler/age local hydration and a
secret-gated private R2 create/read-back/catalog/fresh-hydrate acceptance test.
Generic S3 tests do not substitute for live R2 evidence.

The v0.1 readiness gate also resolves the `secure` Room image to an immutable
digest and runs a clean non-root Podman smoke proving the `vscode` user,
Homebrew, Bash, Git, zstd, and writable home contract before pinning that digest
in the root devcontainer and OCI template.

This remains a review checkpoint, not `v0.1.0`. The secure Room of Requirement
container smoke and dedicated short-lived R2 credential rotation remain open.
RCC Environment Artifacts, Actions Runtime, Hive projections, OpenAI uploads,
periodic capture, garbage collection, and a native VS Code extension are
deliberately deferred.

See [architecture](docs/architecture.md), [ADRs](docs/adr), and
[deferred integrations](docs/DEFERRED-INTEGRATIONS.md).
