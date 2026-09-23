# PCC harvest

`josh-room harvest` is the bounded operator boundary for the PCC trigger queue.
Hooks enqueue metadata only; they do not read a transcript, prepare ciphertext, or
contact a provider.

```text
josh-room harvest plan [event-id] [--outbox-root DIR] [--json]
josh-room harvest run [--offline] [--limit N] [--outbox-root DIR] [--json]
josh-room harvest drain [--limit N] [--outbox-root DIR] [--json]
josh-room harvest status [--outbox-root DIR] [--json]
josh-room harvest inspect [event-id] [--outbox-root DIR] [--json]
josh-room harvest retry EVENT [--reason CODE] [--json]
josh-room harvest quarantine EVENT [--reason CODE] [--json]
josh-room harvest discard EVENT [--json]
josh-room harvest reconcile [--limit N] [--json]
```

`plan`, `status`, and `inspect` are content-free/metadata-only. `run` performs a
bounded source reconciliation, normalization, encryption, and local prepared-outbox
handoff; `--offline` ends at `prepared-encrypted`. `drain` consumes only prepared
records and never rescans Codex. A failed operation remains retryable; quarantine and
discard are explicit operator actions.

All harvest JSON uses `schema: josh-room.harvest` and major version `1`. Human output
contains no ANSI control sequences. Exit `0` means `ok: true`; operational failures
return exit `2`, while the dedicated `hook codex` machine entrypoint retains its
fail-open exit contract.

## One-shot scheduler

```text
josh-room harvest schedule install [--interval SECONDS] [--json]
josh-room harvest schedule status [--json]
josh-room harvest schedule remove [--json]
```

Linux writes a user systemd service/timer, macOS writes a user LaunchAgent plist, and
Windows uses a user Task Scheduler task. Definitions contain only a stable executable
name and opaque scheduler-context identifier; validated private context (including
paths and credentials) remains in a mode-0600 owner-only state file. Installation is
idempotent; removal is reversible.
Unsupported platforms return the stable diagnostic `scheduler-unsupported-platform`.
Definitions are one-shot and guarded against overlap by the native scheduler contract.

`harvest hooks install|status|repair|remove --tool codex` remains the machine hook
installation family; `hook codex` remains the stdin JSON entrypoint.
