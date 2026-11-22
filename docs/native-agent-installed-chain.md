# Installed durable-reference continuation acceptance

Verified 2026-11-22 on Linux with synthetic workspaces, freshly enrolled MinIO,
isolated native Secret Service and a rebuilt, privately installed VSIX. Both
providers passed the same combined chain:

1. Registered installed capture published an encrypted exact-point Room reference.
2. The original native profile was deleted and the workspace quarantined.
3. Normal Room hydration restored the exact point at its original canonical cwd.
4. Registered installed restore wrote the native file into a fresh authorized profile.
5. The actual native CLI resumed that exact file's original provider ID and
   completed one turn against a deterministic local test backend.

No second native materialization or reconstructed history was inserted between
steps 4 and 5. The combined test checks original file bytes, absent source
profiles, the actual clean path-bound Room marker and source/package/installed
controller parity before passing those exact restored paths to the native CLI.
It checks that the Room remains clean afterward.

## Artifact and runtime binding

| Artifact | Observed identity |
| --- | --- |
| Packaged source | `1b9bea6bcab949519aca323aa44e5b33b16e78e1`, including reviewed `e052d6b` publication/removal race repair. This follow-up changes tests/docs only; production/controller bytes remain unchanged. |
| Rebuilt VSIX SHA256 | `2cd3a4cda7ab51d4e64828e2119583a078c3bfadc3cb2664e9a310ee48808b5f` |
| Installed controller | All 81 canonical top-level Python modules and extension source matched source/package/installation. Installed package JSON differed only by `__metadata`. The combined native test independently repeated controller/extension byte comparisons. |
| Workbench | VS Code Insiders `1.148.0-insider`, commit `0d15d4d60248e35fac1a3fe234d74ed780e15691`, own signed-out profile/extensions/HOME, task-private Xvfb/Xauthority, no TCP X server. Telemetry, updates, extension auto-update and experiments disabled. |
| Controller override | Existing `__test__.setRuntimeForTests` hook, `local-build-fallback`, Python `3.12.14`, **installed controller source** on PYTHONPATH. No new production hook or immutable RCC qualification. |
| Storage/custody | Official MinIO `RELEASE.2025-09-07T16-13-09Z`, SHA256 `7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f`; fresh bucket/keyset and isolated native Secret Service, no existing provider/user credentials or host socket. |
| Native providers | Codex `0.171.0` and Claude `2.1.310`, with the exact binary pins and supported no-tools routing in the [synthetic continuation receipt](native-agent-synthetic-continuation.md). |

The storage/workbench phase used only the task's loopback MinIO and private
Secret Service socket. Native model-protocol processes and deterministic backends
ran separately in a new user/network namespace with only loopback, no external
routes and clean environments without storage credentials or D-Bus access.
No hosted inference, paid request, user/provider account login or credential
copy occurred. Backend responses are fixed wire messages, not inference.

## Observed combined results

| Gate | Codex | Claude |
| --- | --- | --- |
| Registered callbacks | `captureAgentRecovery: captured`, `agentRecoveryStatus: inspected`, `restoreAgentRecovery: restored` | Same three successful callbacks |
| Actual workbench interaction | Native capture confirmation, pinned reference status, UUID session picker and no-overwrite/no-model restore confirmation observed/clicked | Same four observed native screens |
| Cold durable chain | Original source profile absent; normal exact-point Room hydrate; registered restore into a fresh per-tool profile | Same |
| Native bytes before turn | Exact admitted 1,068-byte original file | Exact admitted 1,250-byte original file |
| Original provider identity | `11111111-1111-4111-8111-111111111111`, same cwd | Same UUID and original cwd |
| Actual native continuation | `thread/resume`, `turn/start`, completed turn and native persistence | `--resume` plus one print turn; new user/assistant ancestry and native persistence |
| Local backend | One authenticated Responses request with old history, new prompt and `tools: []` | One authenticated Messages request with old history, new prompt and `tools: []` |
| After turn | Original native prefix unchanged, new reply persisted, Room clean | Same; richer runtime rows remain outside capture policy |

Combined acceptance: **2 passed in 2.19s** in
`tests/test_native_installed_chain.py`. Separate helper-extraction regression:
2 passed in 2.26s. No private receipt opt-in: 2 skipped in 0.07s. Explicit host
namespace opt-in was refused before receipt reads/native invocation: two expected
failures in 0.09s. Independent static review found no required changes; it did not
independently rerun the UI/runtime or audit all filesystem/syscall reads.

The private installed receipt/callback log and eight native screenshots establish
the UI/provider steps separately from test assertions. A receipt assertion alone
would not prove those steps. Screens, raw runtime state and private paths are not
published. All owned workbenches/Xvfb/native processes were stopped before cleanup.

## Concrete Claude format gap

After the successful installed restore/native turn, the actual **installed
controller** ran admitted `agents capture` against that same stopped Claude
profile into a fresh local-reference target through the real native Secret
Service, avoiding the separate occupied Room-point collision gate. It refused with
`claude-native-row-unsupported`. All 14 native rows remained unchanged and no
archive was created. The private probe initially expected the wrong error name;
the corrected probe recorded the existing controller's exact refusal, without
changing source or weakening admission/validation.

Setup failures were kept separate from product results. The first workbench
launch exceeded the Linux IPC socket path limit; shortening only its private UI
runtime directory fixed it without changing the controller/provider runtime.
A recapture-probe restart encountered its already-created task HOME before any
CLI ran; a fresh private HOME was then used for the recorded refusal.

This means the qualified narrow input can resume, but ordinary richer post-turn
Claude history is not yet generally preservable. Recapture needs a separate
versioned allowlist review and tests for those native rows/metadata. No row was
dropped, summarized or rewritten to force success. The existing immutable
profile/tool-per-point capture limit also remains.

## Reproduce and remaining scope

Prepare a fresh disposable provider/keyring using the
[nonsecret recipe](native-agent-live-admission.md). Rebuild/install the branch
VSIX into an isolated workbench. Use its actual registered commands, normal
same-cwd Room hydration, deleted source profiles and fresh authorized targets;
observe the real dialogs rather than mocking prompt results. Preserve the
original admitted native bytes outside all native homes.

The task-private `josh-room.native-installed-chain.v1` receipt is acceptance
metadata only, not a production contract. It records package/install paths and
digest, exact points, original-byte paths, source/target profile paths and actual
registered results. Set `JOSH_ROOM_TEST_INSTALLED_CHAIN_RECEIPT` to that mode-0600
receipt, record the outer network namespace and run
`tests/test_native_installed_chain.py` inside the same loopback-only setup used
by the [direct native reproduction](native-agent-synthetic-continuation.md#reproduce-on-the-linux-host).
It is a one-turn fixture; a repeat requires newly registered fresh restores.

The combined installed/source-override/local-test-backend gate is now qualified.
It does not qualify immutable RCC, hosted accounts, general native formats,
Windows, automatic Save/Enter integration or original-T3 restoration. Optional
T3-mediated continuation remains blocked by the separate
[strict-policy restriction](t3-synthetic-continuation-restriction.md). Production
capabilities and all Save selectors remain unchanged/false. No merge, release
staging or release publication was performed. See the
[usable-mode matrix](native-agent-usable-modes.md) for owner review.
