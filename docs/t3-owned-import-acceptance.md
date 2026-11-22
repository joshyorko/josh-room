# Explicit owned T3 history import acceptance

This acceptance concerns `native_agent_t3_import.import_history`, the explicit
product API for importing restored native provider files into a new dedicated T3
profile. It builds on the transport-only receipt in
[t3-provider-import-acceptance.md](t3-provider-import-acceptance.md).

The supported outcome is imported history with retained provider-native identity
and a newly derived T3 app-thread identity. It is not original T3-thread recovery
or native continuation. Save availability and native-new-turn gates remain closed.

## Test boundary

`tests/test_t3_owned_import_acceptance.py` uses real age encryption and the product
single-session materializer before invoking the product import API. Its Codex
`0.171.0` fixture contains genuine native user-event and assistant-response rows;
its Claude Code `2.1.310` fixture contains a linear user/assistant pair. No fixture
rewrites the restored native file. Dimension admission is a controllable synthetic
host authority, not live enrolled-bucket acceptance.

Direct backend runs require a fresh user/network namespace containing only
enabled loopback. That backend refuses the ordinary exposed host namespace. No
existing HOME, authentication, provider execution, model request or external
network egress is authorized. Normal fresh local T3 control authentication and
loopback RPC are permitted. The test opts in through `JOSH_ROOM_TEST_T3`, naming
the actual pinned ELF executable; wrappers and other runtime hashes are refused.

The API owns its fresh temporary child HOME/PATH, native control client, server
process, listener and scoped authentication. Its RPC client uses only the pinned
Node `26.13.1` ELF and built-in WebSocket; no external undici module or SDK is
loaded. It must stop the server and revoke
the local control session before returning. The test examines the resulting
stopped T3 database read-only, rather than manufacturing provider/runtime state.

## Required observations

For each provider, the positive test checks one imported session with native UUID
`11111111-1111-4111-8111-111111111111`, exact canonical fixture cwd, both original
message texts, a stopped runtime and an idle native thread. The new T3 ID is
`import:codex:<UUID>` or `import:claudeAgent:<UUID>`, with `historyOrigin=v1_import`.
Effects, runs, attempts, runtime requests, provider turns, launch workflows and
scheduled work must all remain zero. Native files must remain byte-identical;
provider auth stores must remain absent. A second API call must refuse the now
non-fresh T3 profile, rather than overwrite it. RPC-level idempotence is covered
separately by the earlier transport acceptance.

Negative tests cover pre-existing T3 state, sibling/selected provider auth or
configuration, an active profile operation lock, a changed synthetic authority
between rechecks, and the exposed host network namespace. A hostile PATH Node
executable is refused by the runtime hash guard before execution. Both positive
imports run with hostile ambient `NODE_OPTIONS` preload and `NODE_PATH` undici
fixtures and assert that neither executes. Changed-authority coverage does not
replace live host-grant and current-keyset acceptance.

Codex-only fault cases retain real T3 operations underneath the injected fault:

- After a successful real import RPC, inject `CLICancelled`; require
  `t3-import-outcome-uncertain`, `committed=True`, persisted history, no runnable
  work, a stopped owned process and revoked local authentication.
- After a successful real revoke, inject a cleanup error; require the same
  uncertain outcome and verified inert imported history.
- Fail server start immediately after real local session issuance; require a
  pre-import error, zero imported threads and revoked local authentication.
- Let real issuance persist, then replace its output with malformed JSON or
  inject `CLICancelled` before the caller learns the session ID; require supported
  list/revoke cleanup to find and revoke that fresh local session.

These backend fault cases inject Python exceptions around real operations. The
separate host-worker SIGKILL case below qualifies process containment and lock
release, not a crash-restore transaction.

## Reproduce

Export `JOSH_ROOM_TEST_T3` naming the pinned installed T3 ELF and
`JOSH_ROOM_T3_ACCEPTANCE_TMP` to a new private disk-backed task directory. From the
Linux worktree, run the isolated checks:

```sh
unshare -Urn sh -c 'ip link set lo up; exec .venv/bin/pytest -q tests/test_t3_owned_import_acceptance.py -k "not host_job" --basetemp="$JOSH_ROOM_T3_ACCEPTANCE_TMP"'
```

Use a different fresh private task directory for the exposed-host refusal:

```sh
.venv/bin/pytest -q tests/test_t3_owned_import_acceptance.py -k unisolated_host --basetemp="$JOSH_ROOM_T3_ACCEPTANCE_TMP"
```

Without explicit runtime opt-in, installed-runtime tests skip. Remove private
task directories after the owned processes have stopped; they contain synthetic
keys and fresh local T3 control state, not evidence suitable for Git or Room Save.

## Current verification

Verified 2026-11-22 on Linux against installed T3
`0.1.12-nightly.20261122.5098`, actual ELF SHA-256
`93e6bf69ad395da3a2ba9bec2d8bce99579bc9a5c79167735132af9ac7d301ab`.
Pinned Node ELF SHA-256 is
`f2a178edacbe90a6ce67aa2c7b675f2c079eece8e761eb4363e2b6d719693b7e`.
The installed T3 binary's correspondence to a public source revision is not
attested.

- Isolated product acceptance: **22 passed, 2 skipped in 35.85s**. The skips are
  the exposed-host refusal cases, which correctly run separately.
- Exposed-host refusal: **2 passed, 16 deselected in 0.12s**, retained from the
  earlier run because the namespace guard did not change during the RPC/security
  fixes.
- No runtime opt-in after adding host-wrapper cases: **27 skipped**.
- Focused Ruff formatting/lint and whitespace checks passed.

Both actual imports preserved both messages and native UUIDs, generated the
expected new app IDs, and returned with all checked runnable tables empty. Each
successful profile contained exactly one native local control session, already
revoked before the API returned. Native auth stores remained absent. Every
unsafe-profile, busy-lock, changed-authority, runtime-injection and fault case
passed. Unknown issued-session IDs were recovered through supported local
session listing and revocation; no auth table was modified by the test.

The namespace proof uses only-loopback interface enumeration, a single fresh
user-namespace UID mapping, and the kernel's `NS_GET_USERNS` ownership relation
between the current network and user namespaces. It does not require access to
the host init process. The ordinary exposed namespace was refused before native
T3 state appeared. External/provider egress was impossible in the successful
runs; authenticated loopback RPC was intentional.

This receipt does not qualify healthy-provider startup, an original T3 app-thread
restore, a new native turn, hosted authentication, Windows execution, or crash
recovery of a partially populated T3 destination. A populated destination is
refused on a later API call. No release, merge or publication action occurred.


## Ordinary-host admission bridge

`native_agent_import_job.import_history` is the transient ordinary-host wrapper.
The parent retains Dimension/current-keyset admission reads in its authorized
host environment and answers bounded requests over a private pipe. The child
creates a fresh user/network namespace with loopback enabled and invokes the
same owned-server backend. Provider sessions, native T3 control credentials and
server/client processes stay in that isolated worker; host Dimension credentials
must never cross the bridge. Parent Dimension reads are separate from the child
server's zero external/provider-egress claim.

The provider-positive `host_job` cases invoke this real wrapper for cold Codex
and Claude fixtures from the ordinary host. They require multiple host admission calls and
the same native/derived IDs, exact cwd, both messages, inert database, revoked
local authentication and unchanged native files. The host admission boundary is
mocked with synthetic receipts; these tests do not qualify live enrolled
Dimension reads. Run them from the ordinary host with the same exported runtime
and a fresh private task directory:

```sh
.venv/bin/pytest -q tests/test_t3_owned_import_acceptance.py -k host_job --basetemp="$JOSH_ROOM_T3_ACCEPTANCE_TMP"
```

Final ordinary-host wrapper acceptance passed **3 tests, 24 deselected in 14.66s**
on 2026-11-22. Both successful provider imports preserved the expected IDs, cwd
and two messages, revoked their local control sessions, and made multiple parent
admission requests. All checked runnable tables were empty, including the
separate worker-SIGKILL case. This is real
wrapper/worker execution with a synthetic admission boundary, not live Dimension
network acceptance. Prior import/auth backend and fault receipts above were
retained; these final host cases additionally qualify the launcher hardening.
Private test fixtures and local control
state were removed after the owned worker/server exited.


The Codex-only host-worker fault case intercepts the parent's may-import
acknowledgement after locating the worker's actual pinned T3 direct child. It
sends SIGKILL to the worker immediately before that acknowledgement, independently
observes the T3 process gone or zombie, and acquires the profile operation lock
**before returning to the parent wrapper's cleanup**. The parent then reports
`t3-import-outcome-uncertain` with `committed=True`. Read-only runnable-table counts
remain zero and restored native bytes remain unchanged. The test keeps a pidfd
for cleanup if its observation fails; it does not rely on parent descendant
termination for its successful containment assertion.

The desktop T3 launcher arms kernel parent-death SIGKILL before exec and checks
the expected parent to close the launch race. Ordinary foreground launch behavior
is separate. The observed process containment does not prove successful import,
restoration, checkpoint recovery, or native continuation after worker SIGKILL.
Local control-session revocation is **not** claimed for this abrupt death: its
normal two-minute TTL bounds the task-private session, and the populated T3
destination remains quarantined by the fresh-only guard. The test's private
quarantined fixture/auth state was removed after the owned processes stopped.
