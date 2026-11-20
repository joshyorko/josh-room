---
name: verify-discovery-admission
description: Verify directory discovery sibling admission when changing DirectoryInventory.advance, its staged/queued/finalizing states, iterator error handling, cancellation, aliases, or candidate pagination. Runs a bounded TLA+ model and generated schedules against real Python code; does not qualify Save, hydration or provider operations.
---

# Verify discovery admission

**Property:** Never enter or offer a directory until its parent's complete sibling
scan and alias validation succeeded, the parent is recorded complete, and the
child's name has no case alias in that scope.
**Harm:** A late alias or read failure could otherwise expose an unsafe subtree
as an eligible capture choice.

From the repository root, run:

```sh
uv run --no-project --python 3.12.14 python scripts/verify_discovery_formal.py --self-test
```

This needs Linux descriptor-relative traversal, uv, TLC **1.7.4** and Java
Temurin **25.0.5+8**. No Python packages or project venv are needed. The script
finds the two pinned tools through existing mise installations, or explicit
`TLA2TOOLS` (jar) and `JAVA` (binary) environment variables. It verifies the jar's
SHA256. If tools are missing, follow the installed `/statechart` skill's setup
instructions; the checker never installs tools or changes host configuration.

Allow at most 60 seconds overall and 256 MiB Java heap. The baseline self-test
was 28 seconds on CPython 3.12.14. All fixtures, indexes and TLC state are synthetic
and temporary, outside the workspace, and cleaned on normal/error completion.
Do not point this checker at a user workspace or add provider/credential reads.

## Coverage and source synchronization

Last manually confirmed against commit
`445a0a60b7116b7f65db009f1ee6e7e6b0bf59b7`:

| Production source | Covered behavior |
| --- | --- |
| `src/josh_room/directory_inventory.py:DirectoryInventory.advance` | INSERT state 1 staging; iterator/stat failure; state 4 sibling finalization; state 0 admission; queue priority; pause/resume at work boundaries |
| `src/josh_room/directory_inventory.py:DirectoryInventory._open` | Entry attempts observed by the harness; its mount/link implementation is outside the proof |
| `src/josh_room/directory_inventory.py:DirectoryInventory.page` | No unsafe offered paths; bounded keyset pagination returns every safe fixture path without duplicates |
| `scripts/verify_discovery_formal.py` | Executes the actual production module with controlled directory ordering/failures, not a duplicate scanner |
| `verification/discovery/Admission.tla` | Local sibling-scope admission abstraction; see the action-to-code mapping in `verification/discovery/README.md` |

The product module SHA256 at synchronization is
`cbe32948e4c5e30421add2aa852a35903df41e77d449c6ce02f4b7cd96ffdbd5`.
The runner prints the current digest. A changed digest requires reviewing the
covered functions and action mapping even if checks pass. Update this sync commit
and digest **only after** confirming that the model and harness still describe
the changed code. Never update them merely to quiet drift.

## Read and act on results

Read **every TLC result with `/statechart`**, using its currently installed
instructions. Pass it `verification/discovery/Admission.tla`, the applicable
config and `view.mjs`, in an external scratch directory. Do not copy old skill
command paths. The full three-name config has 742 states, exceeding the chart
budget; `Admission-chart.cfg` provides a two-name view with 158 states. That
smaller chart does not replace the full check. Read broken-model results too.

A normal pass checks 742 model states and 5,233 real-code schedules. The self-test
must catch three broken TLC configs and scratch changes to the actual code:
early state-0 insertion, reversed queue priority, and offering staged rows. It
requires specific open/offer admission violations, not unrelated failures, then
reruns the unchanged real code. The oracle checks each immediate parent and
queries pages immediately while paused.
The shortest mutation story is: observe a child, fail the parent iterator, then
attempt to enter the child. Expected mutation failure is not a product finding.

If current code fails, preserve the synthetic trace as a finding. Replay it using
the actual production module, one event at a time. Check fixture/syscall and TLA+
action assumptions first; do not weaken the invariant to get green. Product fixes
require the user's authorization. Missing mutation anchors mean the checker needs
rebinding, not that the property passed.

Bounds: at most three sibling names (`safe`, `SAFE`, `other`), all subsets,
file/directory assignments and orders, one iterator/stat fault at every
root position and each reachable child position, depth two, and zero/one pause at every real work boundary. The model
allows repeated pause/resume. Blocking calls, concurrent filesystem mutation,
crashes, arbitrary Unicode, mounts, symlinks, Git guards, policy freshness,
provider authority, installed UI, and arbitrary-size correctness are **not proven**.
There is no refinement proof connecting every Python execution to TLA+.

After changes that bypass this check, use `/maintain-formal-verification-skill`
to bring the coverage and sync record back in line.
