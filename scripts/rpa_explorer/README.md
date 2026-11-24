# Bounded observation-driven Room pilot

This is the owner’s installed Josh Room prototype, not a new product framework.
The original deterministic pilot and its immutable evidence remain in
`scripts/rpa_pilot` and `docs/receipts/rpa-pilot`. No Catalog implementation is
changed. The product candidate remains explicit; separate Dakota fixes are not
qualified by these runs.

## Three distinct lanes

- `explore_batches`: a human/agent chooses typed batches from actual returned
  semantic UI observations. The robot contains reusable actions, not a scripted
  discovery policy. `decision` records why the next unprescribed action was chosen.
- `verify_selection`, `catalog_select_all_regression`: deterministic user-facing
  verification and known-defect controls. They are not new agent discoveries.
- `replay_batches`: fresh fixture/profile/storage reproduction, admitted only
  against retained evidence hashes, exact VSIX and the original attempt-set.
  It explicitly rebinds actions to the new target rather than reusing a stale lease.

## RCC example reused

Read the installed `rcc` router and `rcc-robots` specialist first. The concrete
template is `joshyorko/robot-templates` commit
`89dade6f13c1696cfcb091e95a59484456cb4e46`, `02-python-browser/{robot.yaml,conda.yaml,tasks.py}`.
This robot reuses its `@task`, `get_output_dir`, `browser.configure`, `browser.goto`,
semantic assertions, screenshots and artifact conventions, with freeze-first
environment ordering. The bundled `conda-browser.yaml` is a Robot Framework
example; it was inspected, not falsely reported as a Python Browser template.
`robocorp-browser==2.4.0` was verified against its package metadata and API.
The robot selects the supported `chrome` channel with `install=False`, reusing
installed Chrome instead of modifying the host or downloading another browser.

## Architecture and contracts

Two shapes were considered: one robot subprocess per operation, or one owned
RCC task with a bounded batch inbox. The first loses browser/session state and
was rejected. The second owns one disposable container, installed extension,
private profile, browser and optional synthetic provider for the mission.

`mission.json` is v1 and caps elapsed mission time at 900 seconds, semantic
actions at 100, charged UI calls at 240, batches at 24, identical actions at two,
machine observation output at 40,000 bytes and total artifacts at 16 MiB.
In-robot model calls are capped at zero; parent T3 reasoning and auxiliary
verification children are outside that counter. No token-saving claim is made.
Subprocesses have independent 120-second bounds, startup has 90 seconds and
assertion waits at most 60 seconds. Post-task artifact finalization is measured
and can fail the wrapper verdict. Cleanup is bounded separately.

Every batch carries exact session/generation/candidate/previous-observation/
attempt-set binding. The receiver fences immediately before action admission;
retained DOM/screenshot evidence advances the next observation digest. Charged
uncertain UI calls are not refunded. Logical closure invalidates the lease.
One synchronous robot exclusively owns its page; physical focus, an external
actor and cryptographic correctness are not proved by this fence.

Budget names are measurement scopes: `actions` counts typed semantic actions;
`ui_calls` counts calls routed through the charged adapter, including its
observation read, not every internal Playwright/CDP protocol message. `batches`
counts requests; `output_bytes` counts all `PILOT_STATE` lines including ready
and final replies. Full Robocorp logs are measured separately after finalization.

## Launch, doctor and drive

Follow `.agents/skills/verify-rpa-explorer/SKILL.md` and its feature map. Set
TASK_ROOT to a single private `mktemp -d /tmp/codex-room-explorer-XXXXXX` root.
Package the candidate using the existing `vscode-extension` npm package command.
Set PILOT_VSIX, PILOT_OUTPUT (fresh path), ROBOCORP_HOME (task-private), PILOT_INBOX
(empty task directory), and TASK_LOG (task file). Start through the execution
tool and retain its session handle:

```sh
rcc run -r scripts/rpa_explorer/robot.yaml -t explore_batches --silent > "$TASK_LOG" 2>&1
```

RCC closes its task stdin. The implemented fallback is an atomic bounded file
inbox, not an MCP server, daemon or one-off Playwright process. Once ready,
`doctor.py --mission "$PILOT_OUTPUT/mission" --vsix "$PILOT_VSIX"` verifies the
candidate, installed files, declared session and actual HTTP readiness.

```sh
python3 scripts/rpa_explorer/send.py --log "$TASK_LOG" --inbox "$PILOT_INBOX" --actions '[{"kind":"command","command":"joshRoom.new"},{"kind":"assert_text","text":"Configure Catalog Room"}]'
```

Inspect the latest `PILOT_STATE` title/rows/notices before the next batch. Full
DOM and screenshots are references, not automatically returned to the model.
Actions include command, choose, folder, text, button, dismiss, assert_text,
assert_selection, decision and the explicitly named fixture/receipt operations.
No arbitrary shell, URL or JavaScript is admitted from batch input. The folder
chooser may accept its current root while asynchronously handling a typed path;
corroborate the observed paths before selection instead of trusting the requested
path. This harness limitation is recorded, not treated as a product discovery.

Submit `send.py ... --stop`; await actual task completion and target cleanup.
Do not double-submit an existing request while awaiting its result.

## Real Save and cold hydration

Build `Dockerfile.provider` only inside Docker, record its actual local content
ID and pass it as PILOT_PROVIDER_IMAGE. Pass the checksum-verified repository
MinIO binary as PILOT_MINIO. No workstation HOME, source tree, Docker socket,
real D-Bus socket or real credentials are mounted. Only task scratch is mounted.
Private Secret Service and MinIO live inside that one container; only the
editor is published on host loopback. The distro package resolution is recorded
by the built image ID, not claimed immutable across rebuilds.

After normal installed controller acquisition, `fixture/enroll` provisions the
synthetic keyring/bucket/encryption through the managed environment. It is fixture
setup, not UI enrollment coverage. Choose the observed destination, inspect the
actual child/Git review, approve Save and assert its success notification.
Then `copy_recovery_receipt` clicks the product’s explicit public receipt action
and reads only its four validated identity fields in the disposable browser
clipboard. It never captures keys or provider credentials.

`fixture/cold_restore` consumes that exact receipt, restores the parent through
normal managed CLI into an absent destination and records absent children.
`open_cold_parent` navigates the editor fixture there. Use the installed
`joshRoom.enterChild` command and observed path, then `fixture/verify_child`
corroborates the exact pin, README bytes, Git HEAD/index/status/fsck, unchanged source README and Git state, and
absent sibling. Parent CLI restore and fixture navigation are not native parent
cold-discovery UI proof. Current child paths are taken from the authenticated
saved manifest, not assumed from a requested chooser path.

## Reproduction and checks

Set PILOT_REPLAY to the retained ledger, PILOT_REPLAY_SHA256 to its verified
digest and PILOT_REPLAY_ATTEMPTS to the JSON attempt-set from its final binding.
Use the same candidate/image/provider inputs and a fresh PILOT_OUTPUT, then run
`replay_batches`. Missing/changed evidence, candidate or attempt-set is refused.
It verifies the retained DOM/screenshot hashes before creating the fresh target.

`contract_checks` runs budget controls and a bounded exhaustive check of the
actual product `composition_safety._pin`, with two source mutations. The
separate `.agents/skills/verify-rpa-explorer-safety` skill checks receiver binding
and the small TLC model. Neither replaces installed/provider postconditions.

## Bootstrapper and host adapter

The available authenticated bootstrapper was evaluated, not started anew. Its
approved root excludes this worktree and validation actually rejected it.
Inspected `rcc_cli` code drops typed inputs; work items switch to local Python.
Owner-authorized upstream issue #111 / PR #112 fixes the silent acceptance by
rejecting unsupported combinations. No trust/root expansion, bootstrapper
service or source change is required by this pilot. Direct RCC plus the narrow
inbox remains the honest adapter for persistent observation-driven interaction.

PStack 0.15.48 matches the official generated payload at upstream
`ca782ed8b3c2c521cead8e1720d55c3a5dbf2347`; no installation churn. The local
verification skill explicitly reads roles.json for invocation-scoped T3 roles.
It does not claim that upstream’s Cursor rule configures Codex. Code Mode is
optional transport; native UI still executes via RCC/Robocorp Browser.

## Cleanup and limits

Only owned container/listener/private target scratch is removed automatically.
Retain sanitized receipts before deleting TASK_ROOT, generated output and pycache;
remove only task-created images/build artifacts. No broad pruning. SIGKILL cannot
promise cleanup. All 54 manifest commands plus views/menus/settings are inventoried;
invoked commands are not all qualified workflows. Provider cancellation audits,
multi-provider/platform matrices, all components, native cold parent discovery,
and latest Dakota product fixes remain explicit gaps.
