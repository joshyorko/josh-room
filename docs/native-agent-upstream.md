# Native agent compatibility audit

Research checkpoint: 2026-11-22, implementation base
`23c8071` (Josh Room v0.1.52). This is source and installed CLI evidence,
not a successful native cold-resume receipt. No personal profile was opened,
no credential was copied, and no hosted model request was made for this audit.

## Capability matrix

| Harness | Examined version | Supported upstream location / continuation surface | Josh Room acceptance gate |
| --- | --- | --- | --- |
| T3 Code | Earlier `.5085`; current installed `0.1.12-nightly.20261122.5098` separately audited at public source `1d028248683f90f405b2d8e7b71682e3b67317ed` | `--base-dir` / `T3CODE_HOME`; one server-wide home; `userdata/statev2.sqlite`; provider-specific continuation bindings | Dedicated-profile launch can be isolated. Whole-database capture is blocked: database includes authentication and runnable outbox state. Native cold continuation NOT PROVEN. |
| Codex CLI | Installed `0.171.0`; public source `cf3ff1530d821ec2d26452da4971d8e3e7d18d25` | `CODEX_HOME`; `sessions`, `archived_sessions`; `codex resume SESSION_ID`; `--all` disables cwd picker filtering | Synthetic native-file discovery and original-ID history selection verified through the installed standalone app-server. Runtime resume/new turn NOT PROVEN; T3-mediated continuity remains unqualified. |
| Claude Code | Installed `2.1.310` | `CLAUDE_CONFIG_DIR` documented location contract; installed `claude --resume ID` / `--continue`; directory-associated history | Narrow main linear text-only 2.1.310 native-file slice qualified via matching SDK0.3.310 offline readers. Runtime/new-turn and T3-mediated continuation NOT PROVEN. |
| OpenCode | Public source `ee5fe34e136488a1839ac73bcca8321c51a31b75`; CLI package `2.3.0` | Explicit `OPENCODE_DB`; channel-aware SQLite filename; public session/storage APIs need version-specific qualification | Optional follow-up only. Current database credential service stores OAuth/API credential values; copying the database is unsafe. No installed synthetic cold-resume evidence. |
| Antigravity | No installed/versioned native persistence contract qualified | Official documentation entry point inspected; no supported portable native storage/restore contract established | Optional follow-up only. No safe allowlist, versioned schema, licensing qualification or synthetic cold-resume evidence. |

The upstream source revisions are research pins, not a claim that the installed
executables were built from those revisions. Cross-platform native continuation
is unqualified: Linux requires a new real turn; Windows and macOS are NOT RUN.

## T3: root selection is necessary, not sufficient

The installed `t3 --help` calls the option **`--base-dir`**, equivalent to
`T3CODE_HOME`. Do not use the older issue wording `--home-dir` without checking
the executable. [Pinned config](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/config.ts),
`deriveServerPaths`, derives `userdata/statev2.sqlite`, attachments, settings,
logs, browser artifacts, environment identity, runtime state and secrets from
one server-wide root. Explicit root selection prevents the development-server
default from silently selecting another state directory.

The database is **not credential-free**:

- [Migration 031](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/persistence/Migrations/031_AuthAuthorizationScopes.ts)
  creates `auth_pairing_links.credential` and `auth_sessions`, including scopes
  and client metadata.
- [Migration 090](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/persistence/Migrations/090_AuthConnectDevices.ts)
  persists approved device identities and pending connection metadata.
- [Migration 070](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/persistence/Migrations/070_PeerLinks.ts)
  persists remote environment links and secret-store references.
- [V2 foundation](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/persistence/Migrations/OrchestrationV2/Foundation.ts)
  persists an effect outbox with pending/running commands, leases and arbitrary
  JSON payloads. Restoring this needs a replay/side-effect review as well as
  authentication separation.

Filename exclusions around `secrets/` cannot make this database safe. Even a
stopped-server SQLite backup needs a versioned closed-table/field policy,
unused-page handling, attachments barrier, active-task semantics and native
restore evidence. A generic regex scan is not a substitute. Never experiment
on the user's existing server home or overwrite a shared database.

[T3 recovery guidance](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/docs/user/thread-migration.md)
recommends stopping the server before a recovery copy, but explicitly says V1
to V2 thread migration starts a **fresh** provider session on the next message.
That mechanism is history/handoff evidence, not native continuation evidence.

## Provider processes and authentication

[T3 instance environment](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/packages/provider-core/src/server/instanceEnvironment.ts)
constructs child-process environment overrides. Changing variables only in the
VS Code extension cannot change an already-running T3 service or terminal.
Use explicit launch environment on the actual server process, and separately
verify per-provider settings before claiming isolation through T3.

[CodexHomeLayout](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/apps/server/src/provider/Drivers/CodexHomeLayout.ts)
supports a direct home and an auth-overlay home. It treats `auth.json`,
`auth.lock` and `models_cache.json` specially and shares many other directories
using links. This upstream mechanism does not authorize Josh Room to archive
those links or copy an entire Codex home. Continuation identity includes the
shared-home path; root relocation is therefore a compatibility question.

[Codex rollout source](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/rollout/src/lib.rs)
defines native session directories and rollout parsing, compression,
reference-index, metadata, SQLite and writer-lock coordination. A future
allowlist must account for referenced rollout dependencies and stopped writers,
not assume that one JSONL file is always sufficient. Installed CLI help confirms
cwd filtering and explicit session IDs, but does not prove cold restoration.

[Claude settings](https://code.claude.com/docs/en/settings) and
[troubleshooting](https://code.claude.com/docs/en/troubleshooting) are the
official location/authentication references. Automated retrieval of those pages
was unavailable in this audit; the issue's detailed storage assertions must
therefore be reverified before a Claude capture policy is enabled. Installed
CLI help verified resume selection only. Do not infer a credential-free home
from `CLAUDE_CONFIG_DIR`, nor copy account/MCP/trust state.

## Licensing and optional providers

T3's [license](https://github.com/pingdotgg/t3code/blob/a985332c447173750394c0875fb961f88c306730/LICENSE)
and OpenCode's [license](https://github.com/anomalyco/opencode/blob/ee5fe34e136488a1839ac73bcca8321c51a31b75/LICENSE)
are MIT according to their GitHub license metadata. Codex has its own
[license](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/LICENSE).
Claude Code is proprietary: its
[license notice](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)
reserves rights and points to Anthropic commercial terms. This PR must not
redistribute proprietary CLI binaries or claim compatibility from licensing.

OpenCode's [database path](https://github.com/anomalyco/opencode/blob/ee5fe34e136488a1839ac73bcca8321c51a31b75/packages/cli/src/database-path.ts)
supports explicit `OPENCODE_DB` and channel-specific filenames. Its
[credential service](https://github.com/anomalyco/opencode/blob/ee5fe34e136488a1839ac73bcca8321c51a31b75/packages/core/src/credential.ts)
reads/writes credential-table secret values, including refresh/access OAuth
material. Older advice to exclude only an `auth.json` file is not sufficient
for this source version. A closed native session export/import policy and a
fresh-process real turn are prerequisites to support.

[Antigravity documentation](https://antigravity.google/docs/home) does not
establish a portable native session contract in the evidence collected here.
Do not infer support from a VS Code-like filesystem layout or private state
files. No Antigravity binary or user profile was inspected.

## Follow-up issue scope

**OpenCode native continuity qualification ([#211](https://github.com/joshyorko/josh-room/issues/211)):** pin executable/source and license;
document explicit database/session APIs and auth separation; establish a closed
allowlist with WAL/stopped-writer guarantees; restore to an absent isolated
target; prove a new real synthetic turn with original identity; qualify direct
and T3-mediated modes separately. Until then show unavailable, not included.

**Antigravity native continuity feasibility ([#212](https://github.com/joshyorko/josh-room/issues/212)):** identify a vendor-supported,
versioned session storage/export/import/continuation contract and license terms;
prove credentials and machine identity can be omitted; test synthetic cold
restore and a new real turn. Without those prerequisites, retain unsupported
status and do not back up opaque home/database trees.


## Qualified Codex 0.171.0 persisted-history selection

The installed binary accepted a genuinely native synthetic session after encrypted
capture and cold single-file materialization. Its stdio API listed the original
UUID and returned both retained user/assistant items by that same identity. The
result was `notLoaded`. This is native history discovery and selection, not runtime
resume, authentication acceptance, or a new provider turn.

The acceptance uses `initialize`, `initialized`, `thread/list`, then
`thread/read` with `includeTurns: true`. Explicit cwd filtering is verified; a
second cwd returns no thread. Cold discovery permits JSONL scanning with
`useStateDbOnly: false`, and uses `currentAccountOnly: false` because the fixture
has no account. Do not use `includedThreadIds` for the initial cold discovery:
that option forces indexed lookup before the new JSONL has been discovered.
An isolated account-free fixture is not permission to bypass account restrictions
for user profiles.

The native file name must encode its timestamp and thread UUID, with matching date
directories; a reverted rollout may append a second rollout UUID. Every native
record requires an outer timestamp. Session metadata requires its original ID,
timestamp, cwd, originator, and CLI version. Nullable `ordinal` values are unsigned
64-bit numbers when present. The capture validator now enforces these contracts,
accepts valid ordinals, and rejects NaN/Infinity/non-finite numbers. Earlier minimal
fixture bytes were byte-recovery evidence, not native-parser acceptance.

These contracts are pinned to the official
[rollout file parser](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/rollout/src/rollout_file_name.rs#L39)
and [session metadata](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/protocol/src/protocol.rs#L3568).
The source pin is research evidence, not a verified build SHA for the installed
binary. Its own generated JSON schemas separately confirmed the request fields.

The [read handler](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/app-server/src/request_processors/thread_processor.rs#L3243)
projects persisted history. The
[resume handler](https://github.com/openai/codex/blob/cf3ff1530d821ec2d26452da4971d8e3e7d18d25/codex-rs/app-server/src/request_processors/thread_processor.rs#L4053)
also performs an account/auth check and starts a runtime. The next distinct gate
is original-ID `thread/resume` with separately established supported native auth;
a subsequent observed `turn/start` result is required for native-new-turn evidence.
Neither method was sent in this qualification. Paid turns remain approval-gated.
There is no established distinct raw `source: t3` value; do not invent one to claim
T3-mediated preservation.


## T3 and Claude recovery follow-up

The [authenticated provider-history import acceptance](t3-provider-import-acceptance.md)
now qualifies the existing T3 scan/import surface for both cold-materialized
synthetic native formats, with original provider UUIDs and new derived T3 IDs.
This supersedes earlier source-only import evidence, not the original T3 identity
gap. Providers were explicitly unavailable; loopback control was authenticated
and external networking was blocked. Room's UI exposes status/discovery only;
production automatic import and native continuation remain unavailable.

The [current T3 audit](t3-native-recovery-audit.md) separates installed `.5098`
no-turn evidence from earlier `.5085` startup tests. T3's native provider import
preserves provider history but derives a new app identity; it cannot recover an
arbitrary original T3 thread. The audit specifies the missing closed inert
export/import API, including auth/effect exclusion, original IDs, revision fences,
transactional collision/path checks and separately authorized continuation.

The [Claude audit](claude-native-recovery-audit.md) pins SDK0.3.310 to the official
CLI2.1.310 manifest, whose Linux digest matches the installed native binary.
Filesystem readers list/get original synthetic sessions/messages without any
network or process operation. A narrow native main linear text slice is now
implemented independently of the SDK runtime. The unsafe SessionStore-backed
resume loader is excluded because it stages plaintext and inherits auth/config.
No proprietary SDK code or binary is redistributed. Native continuation remains
unverified for both providers, and every Save selector stays unavailable.
