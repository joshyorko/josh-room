---
name: verify-rpa-explorer
description: Launch, doctor, drive and clean the real installed Josh Room VSIX through task-owned RCC Robocorp Browser automations; verify scoped Catalog selection, Save and cold child hydration with synthetic fixtures.
---

# Verify the installed Room explorer

Read `scripts/rpa_explorer/roles.json` explicitly before assigning roles. This is
invocation-scoped Codex/T3 configuration, not a Cursor rule or a global override.
Use live `orchestrator_capabilities` and `delegate_task` provider/model/options.
Never invent provider IDs, substitute an unavailable model or exceed xhigh.
Coordinator Sol 6.2 high and independent judgment Astra 6.2 high ran in this
session; Luna is catalog-only and Antigravity Neon is unqualified. Preserve
existing user pins. This skill explicitly loads the role file; no automatic
Codex routing from a Cursor model rule is claimed.

## Launch

Check disk space. Create TASK_ROOT with `mktemp -d /tmp/codex-room-verify-XXXXXX`.
Package the unchanged product using its existing `vscode-extension` npm package
command into TASK_ROOT/candidate.vsix. Set PILOT_VSIX, ROBOCORP_HOME to the task's
rcc-home, and PILOT_OUTPUT to a fresh output directory. Existing Docker access,
host Google Chrome and access to pinned runtime artifacts are prerequisites.
Never change host configuration to satisfy them.

From repo root run `rcc run -r scripts/rpa_explorer/robot.yaml -t verify_selection --silent`
for deterministic baseline verification. For adaptive exploration, create a
private empty inbox, set PILOT_INBOX and run `explore_batches` instead. Retain the
execution tool's process session and redirect stdout/stderr to TASK_LOG. Ready
must include an exact candidate/session binding, not merely a listening port.
One mission owns one browser/profile/container until teardown. Do not reset the
browser by spawning a robot for each batch, or drive a user's existing editor.

## Doctor

While active run `python3 scripts/rpa_explorer/doctor.py --mission "$PILOT_OUTPUT/mission" --vsix "$PILOT_VSIX"`.
It checks candidate identity, verified installed files, the declared session,
actual loopback HTTP readiness and absence of prior cleanup. Missing prerequisites
or stale receiver binding are harness/precondition failures, not product bugs.

## Drive

Use `python3 scripts/rpa_explorer/send.py --log "$TASK_LOG" --inbox "$PILOT_INBOX" --actions '<typed JSON array>'`.
The sender copies the last returned binding, refuses duplicates and refuses to
continue after a failed batch. Schema and budgets are in mission.json and the
README. Start with a command and a corresponding semantic assertion. Inspect
returned title/rows/notices before choosing the next action. Record a decision
for previously unprescribed follow-ups. Deterministic verify/regression/replay
tasks are separate from adaptive discovery. No arbitrary JavaScript or shell is
accepted by the browser action contract.

## Evidence

PILOT_STATE supplies compact semantic states, counters, receiver binding and
receipt references. Keep full screenshots/DOM, product output and Robocorp logs
in PILOT_OUTPUT, loading only needed evidence. surface.json inventories every
manifest command, view, menu and setting. Invoked does not mean passed. Verify
visible reviews/results and restored bytes/Git/pins separately. Provider setup
is synthetic fixture provisioning. CLI cold parent restoration is distinct
from native child entry and cold-discovery UI qualification. No test runtime,
provider stub, hidden setter or chooser injection is allowed.

## Cleanup

Submit `send.py --stop`; await the actual exit, target.json cleanup fields and
finalized artifact budget. The runner removes only its container, listener and
private profile/scratch. Retain sanitized evidence before deleting TASK_ROOT and
generated output/__pycache__. Remove only images/build artifacts this task made.
Never prune unrelated resources. SIGTERM raises through cleanup; SIGKILL cannot
promise cleanup. Evidence survives at the retained report location named by the
run, not in the disposable target.

## Helpers and feature map

send.py is transport-only; doctor.py is read-only. The actual UI uses
robocorp.browser configure/goto/page in an RCC task. Features are indexed in
features/README.md. Exact commands and setup boundaries are in
scripts/rpa_explorer/README.md. Use /maintain-verification-skill as the surface
changes and /verify-rpa-explorer-safety when receiver/accounting code changes.
