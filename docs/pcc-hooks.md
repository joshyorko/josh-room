# Codex lifecycle hooks

`josh-room harvest hooks install --tool codex` adds three command hooks to the
user Codex `config.toml`: `Stop`, `SubagentStop`, and `SessionEnd`. The command
is an absolute isolated Python entrypoint. It reads one bounded JSON object,
validates the current Codex command-hook fields, verifies a transcript hint
through the `codex.transcript` adapter boundary, and enqueues only a local
trigger in the PCC outbox.

The runtime never reads transcript bytes, normalizes or encrypts content, uses
the keyring or network, or waits for a worker. It exits successfully when
input, local storage, or source validation fails so Codex remains fail-open.
`josh-room hook codex` is the machine entrypoint used by the installed command.

The configuration uses the upstream command-hook shape:

```toml
[[hooks.Stop]]
matcher = "*"
[[hooks.Stop.hooks]]
type = "command"
command = "/absolute/python -I -S /absolute/hook_entrypoint.py"
timeout = 1
async = true
```

`SessionEnd` is synchronous because current Codex forces that event to run
synchronously; all three hooks remain bounded by the local trigger path. Codex
trust is not bypassed: after installation, trust the hook through Codex's
normal user hook controls. `status`, `repair`, and `remove` preserve unrelated
settings and refuse modified owned blocks or unowned Josh Room conflicts.

The upstream hook baseline examined for this implementation is commit
`0a73d55b80afd2aa88051848bd28524d132fd01e` (2026-09-22). It defines the
supported lifecycle events `Stop`, `SubagentStop`, and `SessionEnd`, with
closed command payload fields and `SessionEnd`'s one-second default/three-
second maximum timeout. The installed command uses a one-second upstream
limit but is designed to complete below 250 ms and fail open below one second.
The receipt also contains a bounded runtime manifest (interpreter, entrypoint,
`hook_runtime`, `pcc_enqueue`, and `pcc_outbox` hashes). Status reports
`stale-runtime` when any installed hot-path file changes.

Hooks are a trigger source, not the only recovery mechanism. Missed events,
crashes, and source transitions are recovered by #6 reconciliation.
