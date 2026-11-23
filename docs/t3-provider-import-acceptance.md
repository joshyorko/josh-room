# T3 imported-history fallback acceptance

Verified 2026-11-22 on Linux with installed T3
`0.1.12-nightly.20261122.5098`, Codex `0.171.0` native JSONL and Claude Code
`2.1.310` native JSONL. The installed executable's source revision is not
attested. Public source references below pin
`1d028248683f90f405b2d8e7b71682e3b67317ed`.

## What passed

`tests/test_t3_native_import_acceptance.py` exercises the actual installed server,
its authenticated WebSocket RPC, and real age capture and single-session cold
materialization. Separate fresh dedicated T3 and provider homes are used for each
provider. No database rows are seeded, copied or modified by the test. The test's
Dimension authority is synthetic; it does not qualify live Dimension admission.

| Observation | Codex | Claude Code |
| --- | --- | --- |
| Native session ID | `11111111-1111-4111-8111-111111111111` | Same synthetic UUID |
| Newly derived T3 app ID | `import:codex:<native UUID>` | `import:claudeAgent:<native UUID>` |
| Supported scan | One session, exact canonical fixture workspace | Same |
| Supported import / retry | `importedCount=1`, then `0` | Same |
| Wrong expected workspace | RPC failure before successful import | Same |
| Imported messages | User and assistant | User and assistant |
| Preserved native reference | Strong native UUID; resume cursor uses UUID | Strong native UUID; resume cursor `resume` uses UUID |
| Runtime / native thread | `stopped` / `idle`, exact workspace cwd | Same |
| Effects, runs, attempts, runtime requests, provider turns, launch workflows, scheduled tasks | All zero | All zero |
| Original native file | Byte-identical after import | Same |
| Provider auth / new turn | Not invoked / unverified | Same |

Both imported threads carry T3's `historyOrigin=v1_import`. These are **imported
history with a native-provider fallback**, not restoration of an arbitrary
original T3 app thread. The original app-thread identity/provenance must remain
distinct from these derived IDs. No original T3 state database is restored.

The successful Codex fixture includes its native assistant `response_item`
message before encryption. An earlier event-message-only assistant fixture
produced a partial import: T3 retained the user message but omitted the
`event_msg/agent_message` answer. This is an observed parser limit, not evidence
that all accepted Codex histories import completely. The original native file
remained unchanged in both cases. The
[scanner's message selection](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/project/AgentSessionScanner.ts#L676)
explains this difference.

## Isolation and executable boundaries

The successful qualification ran inside a fresh user/network namespace containing
only loopback. No external/provider egress was possible; authenticated loopback
HTTP/WebSocket control traffic was intentional. Fresh task-private T3 local
control credentials were issued with two-minute TTL through the normal CLI;
tokens stayed in process memory/stdin, never argv or evidence output. The server
used the task's actual `HOME`, `T3CODE_HOME`, `CODEX_HOME`, and
`CLAUDE_CONFIG_DIR`. Existing profiles, authentication and services were untouched.

Providers were explicitly unavailable: configured executable paths did not exist,
and task-only PATH stubs rejected non-version invocations. The successful runs
observed exactly one unsuccessful `codex --version` probe and **zero non-version
provider invocations**, including the Claude run. Native files were parsed by
T3's supported importer; provider replies were not simulated. This qualifies
history import with unavailable providers, not a healthy-provider startup or
continuation path.

Earlier available-version stubs caused startup probes to attempt non-version
Codex commands; those commands were denied. Merely configuring an absent
instance binary did not suppress T3's global Codex installation probe. Therefore
normal unrestricted startup cannot be described as free of provider-process or
provider-auth inspection. The namespace and explicit unavailable-provider setup
are material acceptance conditions, not optional precautions omitted from the
claim. Owned servers were stopped before read-only SQLite verification.

## Supported transport and production limit

The public
[RPC contracts](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/packages/contracts/src/agentSessions.ts#L52)
provide `agentSessions.scan` with `{}` and `agentSessions.import` with
`{projectId, expectedWorkspaceRoot}`. The scan's `alreadyImported` field can
already be true when only the project exists; it does not prove that this session
has been imported.

The tested transport is `/ws?orchestrationProtocol=2`, normal bearer authentication
in the `Authorization` header, and Effect RPC JSON serialization. Requests use
`_tag=Request`, string `id`, method `tag`, `payload`, and `headers=[]`; unary
results arrive as `_tag=Exit` with a success/failure exit. Installed undici supplies
the Node WebSocket client with header support. This is a source-derived contract
exercised against the installed server, not a stable standalone Josh Room SDK.
The test never disables server authentication. Minimal scopes are
`orchestration:read` and `orchestration:operate`; T3's
[own CLI client](https://github.com/pingdotgg/t3code/blob/1d028248683f90f405b2d8e7b71682e3b67317ed/apps/server/src/cli/runningServer.ts#L49)
also issues a bounded local session and sends bearer-authenticated requests.

Production automatic import remains unavailable until Josh Room can attest a
fresh dedicated server's profile, environment, version, ownership and quiescence,
and prevent implicit startup/provider actions. Import is project-wide, not a
single-session selector. The importer records `full-access` runtime metadata in
this fixture, despite executing no run; a later resume needs an explicit supported
policy and fresh user authentication. A status/discovery surface must not imply
that import, original-T3 recovery, or native continuation is included in Save.

The original-T3 recovery API gap remains documented in
[t3-native-recovery-audit.md](t3-native-recovery-audit.md). That earlier audit's
statement that import was not exercised is superseded only by this fallback
receipt. Native continuation remains unverified until an actual supported new
turn is observed. No model, paid, login, provider-auth, merge, release or
publication action was performed.

## Reproduce

Install the repository's existing development dependencies. Set
`JOSH_ROOM_TEST_T3` to the intended installed executable and
`JOSH_ROOM_TEST_T3_UNDICI` to that runtime's `node_modules/undici/index.js`.
Use a fresh private, disk-backed task directory for pytest's `--basetemp`.
Run from the worktree on the Linux host:

```sh
unshare -Urn sh -c 'ip link set lo up; exec .venv/bin/pytest -q tests/test_t3_native_import_acceptance.py --basetemp="$JOSH_ROOM_T3_ACCEPTANCE_TMP"'
```

Observed result: **2 passed in 6.72s**. Ruff formatting and focused lint passed.
The test skips without explicit installed-runtime opt-in and refuses an exposed
network namespace. It stops every owned server, including on failure. Remove the
private task directory afterward: it contains fresh local T3 control state and
synthetic test keys, neither of which belongs in a Room snapshot or Git.
