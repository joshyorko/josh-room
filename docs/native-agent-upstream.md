# Native agent compatibility audit

Research checkpoint: 2026-11-22, implementation base
`23c8071` (Josh Room v0.1.52). This is source and installed CLI evidence,
not a successful native cold-resume receipt. No personal profile was opened,
no credential was copied, and no hosted model request was made for this audit.

## Capability matrix

| Harness | Examined version | Supported upstream location / continuation surface | Josh Room acceptance gate |
| --- | --- | --- | --- |
| T3 Code | Installed `0.1.12-nightly.20261122.5085`; public source `a985332c447173750394c0875fb961f88c306730` | `--base-dir` / `T3CODE_HOME`; one server-wide home; `userdata/statev2.sqlite`; provider-specific continuation bindings | Dedicated-profile launch can be isolated. Whole-database capture is blocked: database includes authentication and runnable outbox state. Native cold continuation NOT PROVEN. |
| Codex CLI | Installed `0.171.0`; public source `cf3ff1530d821ec2d26452da4971d8e3e7d18d25` | `CODEX_HOME`; `sessions`, `archived_sessions`; `codex resume SESSION_ID`; `--all` disables cwd picker filtering | Native import/capture allowlist and cold continuation NOT PROVEN, both direct and through T3. |
| Claude Code | Installed `2.1.310` | `CLAUDE_CONFIG_DIR` documented location contract; installed `claude --resume ID` / `--continue`; directory-associated history | Exact storage schema and safe capture allowlist NOT VERIFIED. Cold continuation NOT PROVEN, both direct and through T3. |
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

**OpenCode native continuity qualification:** pin executable/source and license;
document explicit database/session APIs and auth separation; establish a closed
allowlist with WAL/stopped-writer guarantees; restore to an absent isolated
target; prove a new real synthetic turn with original identity; qualify direct
and T3-mediated modes separately. Until then show unavailable, not included.

**Antigravity native continuity feasibility:** identify a vendor-supported,
versioned session storage/export/import/continuation contract and license terms;
prove credentials and machine identity can be omitted; test synthetic cold
restore and a new real turn. Without those prerequisites, retain unsupported
status and do not back up opaque home/database trees.
