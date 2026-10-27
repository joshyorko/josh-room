# Josh Room for Oh My Pi

This package adds a terminal-native OMP surface for the existing Josh Room CLI. The VS Code extension continues to own graphical Room setup, storage, Save/Enter, and JAT tools. This extension owns validated local context, read-only browsing, explicit checkpoints, and local haul inspection.

The initial implementation used OMP `18.4.12`; acceptance was revalidated against installed OMP `18.6.1` on 2026-10-05 and its release-tagged public `@oh-my-pi/pi-coding-agent` and `@oh-my-pi/pi-tui` APIs. Package discovery uses the current `package.json#omp.extensions` contract. The native status, `/room` hub, snapshot browser, and Save loader were exercised against installed OMP `18.12.6` (tag `v18.12.6`, commit `45c35e4`) on 2026-10-27 with a synthetic Josh Room CLI.

## Development

From the Josh Room repository root, explicitly load the entrypoint for one OMP run:

```bash
omp --extension ./omp-extension/src/index.ts
```

Or link the package into OMP's normal plugin discovery:

```bash
omp plugin link ./omp-extension
```

Run focused tests with Node 22.6 or newer (CI runs them on the pinned Node 22):

```bash
cd omp-extension
npm test
```

## Commands

- `/room` opens a keyboard-first `SelectList` action hub in the interactive TUI (↑↓/j/k, Enter, Esc). RPC and ACP fall back to the host `select` dialog; model tools never need UI.
- Tab completion offers `/room` subcommands and `/jat inspect` as OMP autocomplete items.
- `/room status`, `/room snapshots`, `/room save`, `/room inspect`, `/room rooms`, and `/room doctor` run directly.
- `/jat inspect <absolute-local-haul-path>` delegates inspection to `josh-room jat inspect`.

The discoverable tools are `room_context`, `room_status`, `room_snapshots`, `room_save`, and `jat_inspect`. Read operations use OMP's `read` approval tier; Save uses `write`. Save is always explicit. No restore, delete, provider setup, generic command execution, or automatic checkpoint tool is exposed.

Startup calls only `josh-room context --workspace <cwd> --json`. Outside a valid Room it is silent. Status, snapshot browsing, Save, JAT inspection, and diagnostics run only after an explicit command or tool call. Child processes receive a small environment allowlist and bounded output; workspace paths, raw CLI JSON, and JAT image names are omitted from model results and checkpoint provenance.

The OMP status indicator is a presentation hint. Context comes from the versioned Josh Room CLI contract, and Save still runs through the authoritative Josh Room operation. Session checkpoint entries are namespaced convenience metadata and are never recovery authority.

## Room status

The `josh-room` status entry is themed through the active OMP palette in the TUI and plain text elsewhere: `✓` verified clean, `●` known changed, `?` unknown (needs `/room status`), `!` blocked because Josh Room returned an unsupported or mismatched result. Startup shows identity only (`?`). A successful `edit`, `write`, `apply_patch`, or `ast_edit` marks the Room `●`; a successful `bash` or `eval`, or any failed one of these tools, turns `✓` into `?`. Subagent tool calls do not change the parent session's indicator. An action that finds no validated Room clears the entry. Nothing is re-hashed and nothing saves automatically.

`/room save` and `room_save` show progress (a cancellable bordered loader in the TUI; Esc terminates the owned process tree), update the status from the real receipt, and append checkpoint provenance. A Save that fails or is cancelled before `snapshot create` runs, or that Josh Room itself reports as failed, leaves the previous snapshot authoritative. A timeout, cancellation, unreadable or mismatched receipt, or provenance write failure after `snapshot create` started is reported as an unknown outcome (`room_save` details carry `save_outcome: "unknown"`) and sets the indicator to `?` (`!` for an unreadable or mismatched receipt) until `/room status` or `room_status` refreshes it. Tool results carry structured `details`, set `isError` on failure, and render success, failure, cancellation, and partial progress distinctly.
