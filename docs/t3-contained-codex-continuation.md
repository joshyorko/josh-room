# Contained T3 imported-Codex continuation receipt

2026-11-22, Linux. Two fresh task-native prototypes completed supported T3 import
and one Codex native turn each. These are bounded prototype observations, not a
committed regression suite or hosted-provider acceptance. No production worker,
provider-disable setting, runtime hash guard or capture policy changed.

## Runtime and isolation

| Input | Observed identity |
| --- | --- |
| Actual T3 ELF | `0.1.12-nightly.20261122.5098`, SHA256 `93e6bf69ad395da3a2ba9bec2d8bce99579bc9a5c79167735132af9ac7d301ab` |
| Codex ELF | `0.171.0`, SHA256 `b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0` |
| Public runtime dependencies | Installed T3 version's `node_modules`, including undici `8.12.4`, mounted read-only |
| Research source | T3 `1d028248683f90f405b2d8e7b71682e3b67317ed`; Codex `cf3ff1530d821ec2d26452da4971d8e3e7d18d25`. Binary/source build correspondence is not attested. |

The user-facing T3 executable was a shell launcher, not the ELF. Its different
digest was not runtime drift. The exact ELF above was used for both attempts.

The task-only Debian bookworm-slim image supplied Python, Git, CA certificates,
libatomic and libstdc++; no host package installation occurred. Runtime used
Docker `--network=none --read-only --cap-drop=ALL`, `no-new-privileges`, a 128-PID
limit, 2 GiB memory limit and an owned noexec/nosuid `/tmp` tmpfs. No host ports,
homes, keyrings, authentication directories or sockets were exposed. The only
writable bind was the owned synthetic fixture root, at its original canonical
path. Read-only mounts were the exact public T3 ELF at `/opt/t3`, its public
dependencies at `/opt/node_modules`, and Codex at `/opt/codex`.

Actual network checks found only `lo`, an empty IPv4 route table and no usable
external IPv6 route. Model-protocol and control traffic stayed on loopback.
Fresh local control credentials and a disposable backend key were generated for
the fixture; no existing user/provider credentials or account exchange was used.
Tokens stayed out of argv and evidence. No server security was bypassed.

## Observed cold-import turn

| Gate | Result in each successful attempt |
| --- | --- |
| Cold fixture | Real age capture and fresh single-file materialization; original source profile deleted. Admission was synthetic mocked host authority, **not** the enrolled Room/MinIO installed chain. |
| Supported setup | Dedicated T3 home, `project add`, least-scope five-minute local `auth session issue`, normal desktop server. Supported provider binary/home/custom-model settings and Codex custom-provider loopback routing; empty shadow-home setting. |
| Supported import | `agentSessions.scan` / `agentSessions.import` preserved native UUID `11111111-1111-4111-8111-111111111111` and derived new T3 ID `import:codex:11111111-1111-4111-8111-111111111111`. |
| Supported new turn | `thread.model-selection.set`, `thread.runtime-mode.set`, then `message.dispatch`. Native context recorded approval policy `untrusted` and read-only sandbox. |
| Backend | Exactly one authenticated Responses POST containing old user/assistant history and the new prompt. Backend emitted only `SYNTHETIC_T3_CODEX_CONTINUED` and completion events, never tool calls. |
| Native persistence | Original UUID, cwd and byte prefix retained; new assistant text appended to that native rollout. Workspace content unchanged. Function/custom-tool-call rows: **0**. |
| T3 runtime | One run, one attempt and one provider turn, all completed. `provider-turn.start` and `checkpoint.capture` each succeeded once. Scheduled tasks: **0**; no tool/subagent turn-item types. |

The seven **top-level** declarations were `list_mcp_resources`,
`list_mcp_resource_templates`, `read_mcp_resource`, `update_plan`,
`request_user_input`, `mcp__t3_code` and `mcp__t3_code_direct`. The last two are
namespaces; seven is not their nested function count. Declarations were accepted,
not executed. Legitimate T3 lifecycle/checkpoint effects occurred, so this is
**not** a zero-effects claim. No T3 database/authentication state was archived.

Fresh configuration contained no archived/user hooks, plugins or MCP settings;
native user-hook/plugin/shell discovery features were disabled. Normal T3
adapter integrations were not patched away. The
[trusted Claude Stop callback assessment](t3-synthetic-continuation-restriction.md)
is separate source evidence; Claude-through-T3 was not exercised.

## Limits and retained failures

Setup iterations encountered the shell launcher, missing public libatomic/undici
dependencies and an absent task-home parent. An initial dispatch omitted required
`createdBy`/`creationSource` fields and failed schema validation before any
run/model POST. Correcting supported setup/fields produced the two successes;
no guard was relaxed. The second fresh cold profile repeated the same case to
retain nonsecret declaration names.

This supersedes the blanket empty-tools prerequisite, not the original-T3
identity-preserving API gap. Claude-through-T3, hosted providers/accounts,
installed durable-Room-to-T3 continuation, general native recapture and automatic
Save integration remain unqualified here. Every Save selector remains false.
No hosted/paid calls, real provider auth, tool execution or archived/user hooks
occurred. Owned services/containers and exact task images were removed; shared
public runtime caches/base images were preserved. No prototype payloads or
credentials are committed with this receipt.
