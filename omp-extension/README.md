# Josh Room for Oh My Pi

This package adds a terminal-native OMP surface for the existing Josh Room CLI. The VS Code extension continues to own graphical Room setup, storage, Save/Enter, and JAT tools. This extension owns validated local context, read-only browsing, explicit checkpoints, and local haul inspection.

The initial implementation used OMP `18.4.12`; acceptance was revalidated against installed OMP `18.6.1` on 2026-10-05 and its release-tagged public `@oh-my-pi/pi-coding-agent` and `@oh-my-pi/pi-tui` APIs. Package discovery uses the current `package.json#omp.extensions` contract.

## Development

From the Josh Room repository root, explicitly load the entrypoint for one OMP run:

```bash
omp --extension ./omp-extension/src/index.ts
```

Or link the package into OMP's normal plugin discovery:

```bash
omp plugin link ./omp-extension
```

Run focused tests with Node 26 or newer:

```bash
cd omp-extension
npm test
```

## Commands

- `/room` opens the native selector.
- `/room status`, `/room snapshots`, `/room save`, `/room inspect`, `/room rooms`, and `/room doctor` run directly.
- `/jat inspect <absolute-local-haul-path>` delegates inspection to `josh-room jat inspect`.

The discoverable tools are `room_context`, `room_status`, `room_snapshots`, `room_save`, and `jat_inspect`. Read operations use OMP's `read` approval tier; Save uses `write`. Save is always explicit. No restore, delete, provider setup, generic command execution, or automatic checkpoint tool is exposed.

Startup calls only `josh-room context --workspace <cwd> --json`. Outside a valid Room it is silent. Status, snapshot browsing, Save, JAT inspection, and diagnostics run only after an explicit command or tool call. Child processes receive a small environment allowlist and bounded output; workspace paths, raw CLI JSON, and JAT image names are omitted from model results and checkpoint provenance.

The OMP status indicator is a presentation hint. Context comes from the versioned Josh Room CLI contract, and Save still runs through the authoritative Josh Room operation. Session checkpoint entries are namespaced convenience metadata and are never recovery authority.
