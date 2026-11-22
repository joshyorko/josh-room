# Native agent recovery checkpoint

This opt-in checkpoint isolates a Room-owned T3 process and preserves allowlisted
Codex session files in an independently versioned age-encrypted local reference.
It does not yet establish native continuation across disposable machines.
Normal Save and Enter do not enable, capture, or restore agent profiles.

## Capability and release gates

| Provider | This checkpoint | Native cold-resumed new turn |
| --- | --- | --- |
| T3 Code | Dedicated process home and provider environment; database capture blocked | NOT RUN; unsafe database projection unresolved |
| Codex CLI | Dedicated sessions-only encrypted capture, verification, and absent-target restoration; history-only | NOT RUN; provider authentication and a real new-turn acceptance remain required |
| Claude Code | Dedicated process environment only; capture blocked | NOT RUN; versioned safe capture policy unresolved |
| Antigravity | Research only | Unsupported |
| OpenCode | Research only | Unsupported |

See [upstream contracts](native-agent-upstream.md) for pinned source evidence.
T3's database includes authentication credentials and pending executable effects.
Copying the database would violate the recovery boundary. A future implementation
must audit a closed projection, preserve attachments consistently, and prove a new
turn on a cold-restored process. Importing history is insufficient.

## Ownership and storage

The versioned host-owned profile binds the Dimension, Room, authorized profile,
and canonical real workspace path. Configure an absent dedicated root outside the
workspace. Never point this command at an existing shared T3, Codex, or Claude home.
The foreground launcher supplies the actual child process with its dedicated T3
base directory, CODEX_HOME, and CLAUDE_CONFIG_DIR. It does not change an already
running server, terminal, extension host, or global login configuration.

Credentials are not recovered. Reauthenticate through each provider's supported
login on the replacement machine. Do not copy auth.json, configuration, MCP stores,
server signing keys, provider databases, caches, plugins, or whole homes.
The secret scan can reject recognizable credentials; it is not DLP. Authorize only
synthetic or reviewed personal material. Work material has no upload path here.

The encrypted file is a standalone recovery reference, separate from logical JAT
v1 and PCC evidence. Keep it on a durable, authorized destination before deleting
a machine. A local file on a disposable machine is not off-machine durability.
Room Store copy, export, verify, remove, and reachability do not manage this file.
Copy the encrypted file with ordinary file tools, verify the destination with the
agents CLI, and remove only the explicitly selected retained file when no longer
needed. Never delete provider state as part of reference removal.

No periodic capture runs. Messages after the last successful explicit capture are
not recoverable from that reference. The first checkpoint supports Linux only;
Windows requests fail closed until ACL and process-isolation qualification exists.
Path relocation fails closed. It does not rewrite session paths or create symlinks.
Restore requires an absent target and stages its profile before atomic promotion.

## CLI

Run `josh-room agents --help` and each subcommand's `--help` for the exact flags.
`capabilities` is offline and content-free. Profile `plan` and `status` report the
binding and current support without reading conversations or starting providers.
`configure` authorizes a new dedicated root. `launch` runs T3 in the foreground;
stop it before capture. `capture`, `verify`, and `restore` operate only on the
explicit encrypted local reference and profile binding.

The VS Code Room context menu exposes **Agent Sessions Status**. It uses the same
controller capability contract. It does not add another Save chooser or offer an
unverified Continue action. Issue #190 owns the shared native-component chooser.

## Draft-only safety limits

This is an incomplete implementation checkpoint. Do not enable automatic Room
capture or release it as durable native-session support.

- Recipient authorization is manual and local. Configure pins two explicitly
  selected public age recipients in the private profile policy; capture rejects
  substitutions. The profile is not enrollment in an existing Dimension keyset.
  Automatic Dimension recipient resolution and work-policy admission remain
  blocked before integration with Room Save or any upload.
- The operation lock excludes servers started by this launcher. A provider
  started outside the launcher does not honor it. Such writers are unsupported;
  stop them before capture. Full direct-provider quiescence remains a release gate.
- Restore removes private plaintext staging on normal exceptions, cancellation,
  and SIGTERM. SIGKILL or power loss can leave a private `.native-restore-*`
  directory beside the destination. Crash-safe staging requires more work before
  claiming the issue's interruption guarantee. Do not mistake permissions for
  encryption or secure deletion.
- Only the pinned Codex `0.171.0` JSONL subset is accepted. Other versions,
  compressed rollouts, referenced dependencies, and archived sessions are
  unsupported. Byte restoration proves history recovery, not provider acceptance.

Evidence is limited to tested encrypted byte restoration and an actual isolated
T3 startup. Native continuation remains unverified until each provider completes
a new turn after cold restoration.
