# T3 native recovery API gap

Audit checkpoint: 2026-11-22. Installed T3 is
`0.1.12-nightly.20261122.5098`, newer than the earlier `.5085` startup evidence.
Public source research pin is `1d028248683f90f405b2d8e7b71682e3b67317ed`.
The installed binary's build/source SHA is unverified.

## Evidence and supported surfaces

The installed CLI was inspected with empty, task-owned HOME, T3CODE_HOME,
CODEX_HOME, and CLAUDE_CONFIG_DIR. No existing profile or auth was opened. Native
`project add` created a synthetic project and migrated a fresh dedicated database.
A read-only schema inspection confirmed these tables in that installed build:

| Table | Recovery blocker |
| --- | --- |
| `auth_pairing_links` | Contains a `credential` column and authorization scopes |
| `auth_sessions` | Session identifiers and authenticated client metadata |
| `auth_connect_devices` | Approved device identities |
| `orchestration_v2_effect_outbox` | Effect types, executable JSON payloads, status and leases |
| `provider_session_runtime` | Resume cursor and runtime payload JSON |

The fresh fixture had zero provider-runtime, run and effect-outbox records. No
thread-start/send, model/provider turn, login, or paid request was invoked. The
only database access by the audit was read-only schema and aggregate counts;
no rows were seeded or edited to manufacture a restoration result. No server was
started and no background service was changed.

Installed `t3 thread` exposes `list`, `start`, `send`, and `show`. `start` requires
an agent message, so it was not used as a no-model synthetic thread constructor.
It exposes no whole-thread export/import operation.

The official [recovery guide](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/docs/user/thread-migration.md#keeping-a-recovery-copy)
explicitly says T3 has no whole-thread export command and recommends copying
`userdata` for a stopped-server recovery copy. That advice cannot meet Josh Room's
credential/effect exclusion boundary. The same guide says V1 migration starts a
fresh provider session on its first new message; it is not native continuity.

## Native provider import does not restore an arbitrary T3 identity

T3 has a supported `agentSessions.scan` / `agentSessions.import` RPC surface.
Its [input schema](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/agentSessions.ts#L52)
accepts a project ID and optional expected canonical workspace root. It does not
accept an original T3 thread ID or a closed T3 recovery snapshot.

The [importer](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/project/AgentSessionImporter.ts#L277)
derives the T3 ID from the provider-instance and native-session IDs. It imports
history as `v1_import`, records a stopped runtime, and retains the provider-native
resume cursor. That can expose provider-native history without a model turn, but
it cannot restore an arbitrary pre-existing T3 thread's original identity or its
complete T3 state. This identity behavior is source evidence, not a runtime-import
acceptance receipt. No import or continuation was exercised in this audit.

[Portable handoffs](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/docs/user/portable-handoffs.md)
transfer selected context and omit reasoning, tool-call state, and attachments.
They cannot substitute for the original provider session's recovery.

## Exact missing primitive and minimal upstream change

Josh Room needs a versioned, provider-aware **inert thread recovery export/import**
API. A stopped SQLite file copy, generic text export, or replay of arbitrary
internal events is insufficient. The smallest upstream addition is:

1. Export one explicitly authorized thread/project at a fenced revision. Refuse
   active runs, pending effects, queued turns and background work unless the
   recovery contract explicitly converts them to terminal historical facts.
   Produce a closed schema of original app-thread, provider-thread and native
   provider-session IDs; approved message/history facts and ordering; non-secret
   model/mode settings; canonical workspace identity; and verified attachment
   references. Preserve native identity without an implicit new-session handoff.
2. Exclude auth, pairing, device, peer-link secrets, live ownership/lease state,
   executable outbox/command payloads and scheduled/delegated work structurally.
   Driver-specific continuation references must have a closed non-secret schema.
   Do not round-trip arbitrary runtime JSON.
3. Import transactionally into an absent/inert destination identity. Preserve the
   original T3 and provider-native IDs, fail on collisions or path/provider
   mismatch, validate separately restored native session files, regenerate local
   environment/auth state, and schedule no provider action. Require a separate
   explicit resume after fresh supported native authentication.

The relevant existing types are
[app thread](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/orchestrationV2.ts#L475),
[provider thread](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/orchestrationV2.ts#L1220),
and [thread projection](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/orchestrationV2.ts#L2283).
They are not already a safe recovery envelope. In particular,
[provider runtime storage](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/persistence/ProviderSessionRuntime.ts#L38)
types both resume cursor and runtime payload as unknown JSON. The
[current RPC schema set](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/orchestrationV2.ts#L4683)
provides reads and command dispatch, not this closed identity-preserving inert
recovery transaction.

## Josh Room decision

Keep T3 capture/restore and #190 selection unavailable. Dedicated-profile launch
is separate existing functionality. Provider-native files can be qualified
independently, but their import must not be advertised as restoring the original
T3 thread. No local database-projection implementation is added to evade the
missing upstream contract. Native continuation remains unverified until supported
recovery, fresh auth/account compatibility and an actual new turn are observed.
No genuine user-only prerequisite was found for this API gap; upstream behavior
must change before a user credential or paid-call approval would help.
