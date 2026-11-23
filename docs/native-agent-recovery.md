# Native agent recovery checkpoint

The explicit [Room-bound encrypted reference lifecycle](native-agent-references.md)
extends this local history slice with v3 host opt-in, exact historical bindings,
Copy/export/remove/reconcile handling and registered native status/capture/restore.
Closed JAT/PCC schemas and Save selectors stay unchanged. V1/v2 grants remain
local-only; remote work references and general native continuation remain unqualified.

[Synthetic native continuation](native-agent-synthetic-continuation.md) separately
qualifies original-ID Codex/Claude resume plus one turn against deterministic
loopback test backends. The later [combined installed chain](native-agent-installed-chain.md)
also qualifies durable capture, cold Room hydrate, registered installed restore
and a native turn using the exact restored file, with a disclosed source-runtime
override. Neither enables Save or proves hosted/account compatibility.

This opt-in checkpoint isolates a Room-owned T3 process and preserves allowlisted
Codex and qualified Claude session files in independently versioned age-encrypted
local references.
It does not yet establish native continuation across disposable machines.
Normal Save and Enter do not enable, capture, or restore agent profiles.

## Capability and release gates

| Provider | This checkpoint | Native cold-resumed new turn |
| --- | --- | --- |
| T3 Code | Dedicated process home and provider environment; database capture blocked | NOT RUN; unsafe database projection unresolved |
| Codex CLI | Admitted native-file capture/verification and explicit single-session materialization; history-only, whole-profile restore blocked | PASS only for original-ID resume and one deterministic loopback test turn; hosted/account and T3-mediated acceptance remain unqualified |
| Claude Code | Opt-in 2.1.310 main linear text-only native capture/verification/single-file materialization | PASS only for original-ID resume and one deterministic loopback test turn; richer post-turn recapture is refused, hosted/account and T3-mediated acceptance remain unqualified |
| Antigravity | Research only | Unsupported |
| OpenCode | Research only | Unsupported |

See [upstream contracts](native-agent-upstream.md) for pinned source evidence.
T3's database includes authentication credentials and pending executable effects.
Copying the database would violate the recovery boundary. A future implementation
must audit a closed projection, preserve attachments consistently, and prove a new
turn on a cold-restored process. Importing history is insufficient.

## Ownership and storage

The versioned host-owned profile binds the Dimension, Room, authorized profile,
and canonical real workspace path. A separate host grant must authorize its exact
dedicated profile root. Configure an absent dedicated root outside the
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
Copy the encrypted file only to an authorized local reference root, verify it
with the agents CLI, and remove only the explicitly selected retained file when no longer
needed. Never delete provider state as part of reference removal.

No periodic capture runs. Messages after the last successful explicit capture are
not recoverable from that reference. The first checkpoint supports Linux only;
Windows requests fail closed until ACL and process-isolation qualification exists.
Path relocation fails closed. It does not rewrite session paths or create symlinks.
Restore is blocked before decryption or filesystem mutation. The earlier named
plaintext staging path has been removed because it cannot satisfy SIGKILL safety.

## CLI

Run `josh-room agents --help` and each subcommand's `--help` for the exact flags.
`capabilities` is offline and content-free. Profile `plan` and `status` report the
binding and current support without reading conversations or starting providers.
`configure` creates a dedicated root only after host admission. `launch` runs T3
in the foreground; stop it before capture. `capture` and `verify` require current
Dimension admission and an explicitly approved encrypted local reference.
`restore` always reports `crash-safe-materialization-unavailable`.

The VS Code Room context menu exposes **Agent Sessions…**. It reports profiles
matching the selected Dimension, Room and active canonical workspace, then offers
explicit read-only Codex/Claude history discovery. `agents room-status` returns
opaque profile selectors and metadata without reading conversations or keysets.
`agents discover` revalidates the host grant and current keyset, tool scope,
recipients, stopped-profile lock and native-file consistency. Revoked selectors,
busy profiles and mismatched workspaces fail closed. Private profile roots and
conversation previews are not sent to VS Code. Discovery performs no T3 import,
assigns no new app-thread ID and offers no Continue action. Issue #190 owns the
shared native-component chooser; every provider remains unselectable there.

The shared Save chooser additionally uses `agents inventory --workspace PATH
--json` for bounded ordinary Codex/T3 metadata detection on Linux. It reports
exact-folder and child-folder counts independently of host admission. The
probe recognizes the default T3 development layout when `VITE_DEV_SERVER_URL`
is set in the extension host. The picker shows an unavailable tool's bounded
grant revalidation reason even when the other tool is admitted. Finding ordinary
history does not enroll or capture it. See the [Save inventory limits
and admission diagnostics](native-agent-save-adapter.md#ordinary-sessions-and-admission-diagnostics).
This does not broaden `room-status`, authorize work uploads, or enable original
T3 restoration or automatic shared-home adoption.

T3's supported provider-history scan/import was separately qualified against
cold-materialized synthetic Codex and Claude files using a fresh authenticated
local server. It preserves the provider-native UUID but derives a **new** T3
app-thread ID. Both messages, exact cwd, retry behavior and zero runnable effects
passed with external egress blocked. This is an imported-history/native-provider
fallback, not original T3-thread restoration or a proven new turn. See the
[transport acceptance conditions and parser limits](t3-provider-import-acceptance.md).

`agents import-history` is the separate explicit owned-server operation. It
accepts an authorized profile and qualified T3 executable, never a PID, URL or
existing server. The host controller rechecks the current Dimension keyset over
its normal admission path; a temporary private-pipe job owns a fresh user/network
namespace containing only loopback. T3 and its authenticated RPC client run there
with fresh HOME, sanitized environment, disabled provider executables and pinned
T3/Node builds. The existing dedicated-profile launcher and operation lock remain
the process authority. PID/start-time, executable, cwd, actual provider homes and
process-owned listener checks precede authenticated RPC.

Only a fresh T3 directory and allowlisted provider-history roots are accepted.
The operation imports all discovered histories for the exact project, then stops
its server, revokes disposable local control sessions through supported T3 APIs,
and checks stopped native state for runnable work. It does not attach to or alter
a running shared server. Example with synthetic private paths:

```sh
josh-room agents import-history --dimension demo --room demo-room \
  --profile personal --workspace /workspaces/demo \
  --profile-root /secure/native/demo --tool codex \
  --t3-executable /opt/t3-qualified/t3 --json
```

Only the exact Linux runtime hashes in
[owned-import acceptance](t3-owned-import-acceptance.md) are qualified. Unsupported
builds and platforms fail closed. Cancellation or verification/cleanup failure
after a possible import reports `commit-uncertain`; a populated T3 destination
is refused on another call. Crash recovery of partially imported T3 state is
unavailable. This explicit history fallback does not enable automatic import,
native continuation, original T3 identity recovery or Room Save inclusion.

## Draft-only safety limits

This is an incomplete implementation checkpoint. Do not enable automatic Room
capture or release it as durable native-session support.

- Configure, launch, capture, and verify require the host grant and current
  MinIO/S3 keyset described below. R2 admission and credential-file/runtime
  handoffs are unsupported. Missing native credentials or keyset reads fail
  closed without enrollment, login, or upload.
- Linux kernel read leases reject existing writers and detect later write opens
  on every selected Codex file. Capture reads retained descriptors and checks the
  original inventory before publication. Unsupported filesystems, lease breaks,
  source changes, and changed admission reject publication. This proves selected
  file consistency, not that every unmanaged T3 server is stopped.
- Crash-safe whole-profile native materialization remains unavailable. Restore never creates
  plaintext staging, including on cancellation or SIGKILL. In-memory verification
  remains available. A separate single-session operation is described below;
  it does not qualify whole-profile restore or native continuation.
- Only the pinned Codex `0.171.0` JSONL subset is accepted. Other versions,
  compressed rollouts, referenced dependencies, and archived sessions are
  unsupported. Encrypted native-byte verification does not prove provider acceptance.

Earlier draft byte-restoration evidence applies only to its recorded head. The
current checkpoint supports encrypted capture/verification and isolated T3 startup. Native continuation remains unverified until each provider completes
a new turn after cold restoration.


## Host admission contract

The host owns `$XDG_CONFIG_HOME/josh-room/native-agent-admission.json`, beside its
existing private `config.json`. The policy directory must be owner-only and the
policy/config files must be regular owner-only files, with no links. The policy
must remain outside the workspace and native profile. The command does not write
this authority from workspace metadata or transcript content.

The document has exactly `format` and `grants`, a bounded list. Original format
`josh-room.native-agent-admission.v1` remains codex-only. To opt into Claude, use
`josh-room.native-agent-admission.v2`; every grant must additionally include a
nonempty unique `tools` list drawn from `codex` and `claude`. The selected tool must
be explicitly listed. T3 recovery remains unqualified. Upgrading Josh Room never
expands an old grant automatically. Each grant has these fields:

| Field | Required binding |
| --- | --- |
| `dimension`, `room`, `profile` | Exact operator-authorized identifiers |
| `tools`, v2 only | Explicit `codex` and/or `claude` opt-in; v1 implicitly permits only codex |
| `workspace` | Canonical real Git top-level directory |
| `profile_root` | Exact dedicated native root, outside workspace/reference roots |
| `repository` | Exact normalized `host`, `namespace`, `name` matched against host-observed local Git origin |
| `scope` | `personal`, `work`, `oss`, or `homelab`; named scope profiles cannot claim another scope |
| `local_reference_root` | Separately approved canonical private local directory outside the workspace/profile |
| `encryption_domain_id`, `key_generation` | Existing configured Dimension and current keyset |
| `recipients` | Exactly two distinct native age public recipients, matching the current operational and recovery recipients |

MinIO/S3 keyset verification performs an existing control-record read using native
OS secret custody. It does not enroll, export an identity, mutate the bucket, or
accept plaintext runtime credentials. A failed read denies the operation. Capture
revalidates the authority hash after encryption and before publication. All scopes,
including work, have only the approved local reference destination. Upload/export
operations are denied. This implementation does not classify whether a local path
is backed by remote storage; operators must not authorize a work destination that
violates their policy. Grants do not alter PCC's existing capture policy.

`capabilities`, `plan`, and `status` do not read provider keysets or conversations.
A configured profile's status requires admission revalidation before operations.

## Chooser contract for issue #190

`agents capabilities --json` has `contract_version: 1` and the closed provider IDs
`t3`, `codex`, and `claude`. Every provider reports `selectable: false` and
`restore: "blocked"`. Codex's `capture: "history-only"` describes the independent
local command, not a restorable Room component. Unknown versions, providers, or
states must be unavailable in Save. No row can report included from these fields.
The [coordination comment](https://github.com/joshyorko/josh-room/issues/190#issuecomment-7080438208)
records this boundary. Save/Enter and logical JAT v1 have no agent-state component.


## Single-session history materialization

`agents materialize-session` takes the existing explicit profile binding, encrypted
`--source`, private `--identity` path, and original `--session-id`. It selects exactly
one approved Codex JSONL file from the encrypted reference. Ambiguous/missing IDs
fail closed. The target must be a separately configured, host-admitted fresh
profile at the canonical workspace location. Existing T3/Claude state, other Codex
files, unknown directories, mismatched native content, or a busy owned profile
prevent materialization. The original general `restore` operation stays blocked.

The Linux implementation writes verified native bytes into an anonymous
`O_TMPFILE` inode. After file fsync and renewed host admission, it uses
`linkat(AT_EMPTY_PATH)` to publish one complete, absent-target file. Parent
directories are fsynced and their identity rechecked. An unsupported filesystem,
missing kernel feature/privilege, or path collision has no named-file fallback.
It does not copy auth, indexes, configuration, attachments, or other session files.

Before commit, SIGKILL releases the anonymous inode without a visible plaintext
stage. Empty native directory metadata can remain and is safe to retry. After
commit, the complete final history file can remain even if the process never
returns a receipt. An uncertain post-link cancellation, fsync, or parent-path
outcome reports `commit-uncertain` with `committed: true`. Do not delete or overwrite
that file automatically. Retry the same operation; a matching complete final file
in an otherwise fresh profile is verified under read leases and acknowledged with
`recovered_commit: true`. Different existing content is preserved and rejected.

This is a single-file namespace transaction, not a multi-file/home transaction,
forensic block erasure, or native-resume acceptance. SIGKILL/retry behavior was exercised
on Linux 7.3.1 x86_64 with both tmpfs and verified disk-backed btrfs. Actual
physical power-loss recovery remains unqualified. A local tmpfs profile is volatile and must not be treated
as durable storage. The independent encrypted reference remains the recovery unit.
Every provider still reports `restore: "blocked"` and `selectable: false` for
issue #190; Codex additionally reports its separate history-only
`materialize_session` capability. No Save/Enter integration is enabled.


## Native discovery acceptance and remaining continuation gate

Codex 0.171.0 discovered and selected the cold-materialized synthetic session
through its own app-server, retaining the original UUID, workspace, native path,
and user/assistant history. The process used isolated empty homes, no account/auth
store, and a custom loopback-only provider. An owned provider guard observed zero
connections throughout process lifetime. Only persisted-history methods were sent;
the thread remained `notLoaded`.

The stricter source contract requires the native timestamp/UUID filename,
mandatory metadata and row timestamps, and valid optional ordinals. Older malformed
draft fixtures are now rejected without modifying their encrypted references.
See [the pinned upstream protocol](native-agent-upstream.md#qualified-codex-01710-persisted-history-selection)
and [reproducible acceptance evidence](native-agent-verification.md#disk-backed-and-native-codex-discovery-follow-up).

That earlier qualification did not exercise runtime resume or a new turn.
Hosted/account compatibility remains unverified.
Fresh native authentication must be established separately; no auth material is
recovered from the reference. Whole-profile restore, all #190 Save selections,
T3/Claude native restore, and the release hold remain unchanged.

That no-turn receipt is historical. The later
[synthetic continuation test](native-agent-synthetic-continuation.md) observes
direct Codex/Claude native turns with generated local-only credentials, without
qualifying hosted accounts, whole profiles or T3 continuation.


## Qualified Claude native history slice

Use `--tool claude` explicitly with configure, capture, verify, and
materialize-session. Codex remains the default. Claude references use the separate
`josh-room.native-claude-history.v1` envelope and are rejected by Codex operations.
A v2 host grant must name Claude; original v1 grants remain codex-only.

Only Claude Code 2.1.310 main-session JSONL under the selected native
`projects/<encoded canonical workspace>/<session UUID>.jsonl` is accepted.
Qualification is limited to canonical short ASCII workspace encodings of at most
200 characters. Every row must retain the exact cwd, session ID, version,
canonical message UUID, timestamp, external-user/main-session status, and a
complete linear parent chain. Messages are text-only user/assistant records;
the file must end with an assistant message. Qualified optional cache usage
counters are preserved. Content is scanned before encryption and after decryption.

Sidechains/subagents, tools/results, images/attachments, file-history snapshots,
compaction/progress/queue records, unknown fields, non-text payloads, missing or
branched ancestry, other versions and long-path hashing are unavailable. This
conservative subset is not a claim that every daily Claude session is portable.
Unsupported records fail the entire capture; they are not stripped or rewritten.
Credentials/settings/auth roots and other projects are never recursively copied.

Cold materialization retains original native bytes, session/message identity and
on-disk ancestry. The matching SDK 0.3.310 discovered/read that history with all
network, socket and subprocess APIs prohibited. The SDK message view returns
original message IDs and `session_id`; raw `parentUuid` ancestry is verified in
the unchanged native file, not invented as an SDK return field.

Do not use the SDK's SessionStore-backed resume loader for this recovery path.
The audited loader creates named plaintext staging and copies authentication/
configuration from another root. Josh Room instead uses its admitted anonymous
single-file transaction and only filesystem SDK readers in acceptance tests.
No SDK code or binaries are distributed by Josh Room.

See [Claude source/provenance and API audit](claude-native-recovery-audit.md).
T3's [missing native recovery API](t3-native-recovery-audit.md) is a separate
upstream blocker. No user credential or paid request resolves that API gap.
Every #190 row remains unavailable and no native-new-turn claim is enabled.
