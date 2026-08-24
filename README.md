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

Production identities and credentials are bootstrapped outside the repository.
Never commit an age identity, R2 credential, real catalog, or snapshot.

## Private instance

Non-secret instance metadata lives under `$XDG_CONFIG_HOME/josh-room/` and
conforms to [the private config schema](schemas/private-config.schema.json).
Persistent R2 credentials live in the host OS Secret Service and are retrieved
only for an operation. See [private R2 setup](docs/R2-SETUP.md).

Production snapshots require independent daily-use and offline-recovery age
recipients. Every recipient must decrypt independently. The private R2 bucket
remains private; object keys contain only ciphertext SHA-256 digests.

## Josh's golden path

The personal Room template bootstraps `age`, `uv`, stable RCC, Action Server,
Hauler, JAT, and Josh Room without running a JAT workload. One-time
`josh-room setup` stores R2 credentials and the daily age identity in the host
OS keyring while writing only non-secret metadata to the private XDG config.

Daily use is:

```bash
josh-room doctor --backend r2
josh-room enter hive
```

R2 and VS Code Insiders are the defaults. `doctor` fails with remediation when
age, Hauler, RCC, JAT, the daily identity, R2, the encrypted catalog, or the IDE
is unavailable. `enter` discovers logical project names from the encrypted R2
catalog, hydrates safely, then launches VS Code Insiders.

The `Josh: Enter Room` VS Code task is native `tasks.json` and calls the CLI.
No extension is required for remembered selection because the encrypted catalog
and CLI picker own that state. The likely extension behind the earlier
"remember variable" idea is Command Variable, but Josh Room does not install or
depend on it.

## Commands

```text
josh-room doctor [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room projects list [--backend local|r2] [--json]
josh-room snapshots list <project> [--backend local|r2] [--json]
josh-room snapshot create <project> --source <path> [--backend local|r2] [--json]
josh-room hydrate <project> --destination <path> [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
josh-room enter [<project>] [--backend local|r2] [--ide terminal|vscode|vscode-insiders] [--json]
```

`enter` lists logical project display names, resolves the encrypted catalog,
hydrates into an adjacent owned stage, atomically promotes the workspace, and
launches the selected IDE only after durable completion.

## Evidence and current limits

The test suite includes real synthetic JAT/Hauler/age local hydration and a
secret-gated private R2 create/read-back/catalog/fresh-hydrate acceptance test.
Generic S3 tests do not substitute for live R2 evidence.

This remains a review checkpoint, not `v0.1.0`. The secure Room of Requirement
container smoke and dedicated short-lived R2 credential rotation remain open.
RCC Environment Artifacts, Actions Runtime, Hive projections, OpenAI uploads,
periodic capture, garbage collection, and a native VS Code extension are
deliberately deferred.

See [architecture](docs/architecture.md), [ADRs](docs/adr), and
[deferred integrations](docs/DEFERRED-INTEGRATIONS.md).
