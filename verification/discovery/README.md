# Discovery sibling-admission verification

A child may be entered or offered only after its parent has completed its full
sibling scan, validated aliases and been recorded complete, with no alias for
the child's name in that scope. Otherwise a late
alias or directory read failure could expose an unsafe subtree for selection.

The project-local `verify-discovery-admission` skill owns the runnable procedure
and source sync record. Run from the repository root:

```sh
uv run --no-project --python 3.12.14 python scripts/verify_discovery_formal.py --self-test
```

The standard-library runner verifies TLC 1.7.4 by SHA256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`, uses the
installed Temurin 25.0.5+8 runtime, and never installs tools. Explicit `TLA2TOOLS`
and `JAVA` overrides are supported. All process/state output and fixtures live in
one temporary task directory. Java heap is capped at 256 MiB, subprocesses have
timeouts, and a 60-second alarm bounds the complete Linux verification run.
No third-party Python packages or new project venv are needed.

## Why this tool and scope

TLC explores every reachable state in the finite admission model, including
orders and failure points that example-based regression tests can miss. It does
not verify arbitrary Python. Generated executions therefore exercise the real
`DirectoryInventory` methods as a separate check on the abstraction: no hand
translation of `advance()` replaces the production code. This is a bounded model
check plus bounded implementation checking, not an unbounded proof or a formal
refinement theorem between the two.

`Admission.cfg` allows every subset of three names, every assignment of those
names to file/directory types, all name-observation and child-finalization orders,
local-parent read failure at any observation boundary, and repeated pause/resume.
`SiblingAdmission` checks both entered and offered children. `TypeOK` checks model
state representation. Completion/liveness/fairness are not proven. Deadlock
checking is disabled; correctly finished scopes may stutter.

The real-code harness covers all corresponding enumeration permutations, one
iterator failure at each position including EOF, one stat failure at each entry (also inside each reachable child scope),
and zero/one pause at every observed `advance(budget=1)` boundary. Each directory
child contains a `leaf`, so attempted traversal into depth two is observed.
Each step queries actual paginated results, not just counters. Successful scopes
must offer exactly the fixture's safe directories and leaves without duplicates.
The oracle checks the immediate parent's EOF, failure state, full alias set and
completed SQLite record for every open/offer, including `leaf`. Pages are also
checked immediately while paused. The root's fixed fixture does not change during a schedule. Directory entries are
synthetic OS entries wrapped to control ordering/failure, SQLite and the scanner
are real, and the deadline clock is fixed to make work-budget steps repeatable.

## Model to code mapping

Validated source: `445a0a60b7116b7f65db009f1ee6e7e6b0bf59b7`.

| TLA+ action/state | `src/josh_room/directory_inventory.py` seam |
| --- | --- |
| `ReadEntry` | `advance`: successful `next`/`stat`, name insertion, directory INSERT with state **1** |
| `EndRead` | `advance`: `StopIteration` branch updates parent to state **4**, closes active stream |
| `ReadFailure` | `advance`: iterator/stat `OSError` branch sets parent state **3** and closes stream; staged children remain hidden |
| `FinalizeChild` | `advance`: state **4** branch checks all sibling aliases, updates one child to **0** (queued) or **3** (rejected) |
| `FinishParent` | `advance`: no staged child remains, parent becomes state **2**; the separately modeled priority rule prefers state 4 before queued state 0 |
| `PrioritizeFinalization`, `OpenChild` | `advance` queue selection followed by `_open`; parent status is established by staging/priority, not an extra guard invented in the model |
| `OfferChild` | `page` selects completed state-2 rows; child scan/validation and terminal request gating are abstracted |
| `Pause`, `Resume` | `advance(cancelled=True)` and the next advance; model pauses the admission core, not the entire global scan status machine |

One model step is an observation of a single serialized `advance` work unit.
The real code performs several syscalls/SQL statements within that unit; these
are **not** claimed to be physically atomic. No competing scanner/index writer or
filesystem mutation is allowed during this local abstraction's unit. The model
abstracts successful child processing into opening/offering; it is not a model
of the full recursive tree or source freshness algorithm. The generated harness
checks the actual implementation at these work boundaries and intercepts each
child open attempt before it happens.

Explicitly outside scope: concurrent filesystem mutation, changing source or
policy, root replacement, crashes/persistent resume, arbitrary depth/name sets,
Unicode normalization beyond the two ASCII aliases, symlinks, mounts, Git/Room
guards, payload protection, Save, publication, retention, hydration and native UI.
Passing does not close those PR D acceptance gaps or authorize any capture.

## Baseline and deliberate failure

On CPython 3.12.14, TLC 1.7.4 and the stated source:

- PASS full model: **742 distinct states**, 1,330 generated, depth 12.
- PASS real scanner: **5,233 schedules**, **50,023 work iterations**.
- PASS expected negative model: `SiblingAdmission` fails when
  `BrokenAdmission=TRUE` promotes an observed child directly to queued.
- PASS real-code mutation detection: a temporary copy of the actual module
  changes the directory INSERT from state 1 to state 0. It fails on a one-child
  scope: open parent, observe `safe`, fail the iterator at EOF, attempt to open
  `safe`. The child never receives a complete, successful sibling-scope proof.
- PASS restoration: discard the mutated copy and rerun the unchanged production
  source; the same 5,233 schedules pass. Product source is never edited.

Strengthened complete self-test took 27.778 seconds. This is a local observation, not
a CI latency guarantee. The runner prints actual counts, duration, Python version
and product-module digest. Tool errors are failures, not verification passes.

Use `/statechart` for every model result. The default graph exceeds its 500-state
chart budget; `Admission-chart.cfg` draws a two-name projection (158 checked
states), with no reduction of the main verification bounds. The view merges
internal iterations and no-op steps; it does not show full inventories. Broken
model paths must be replayed against the real source before reporting a product
finding. All three mutation paths were replayed by the real-code self-test.

The generated [two-name passing chart](Admission-chart.mmd) shows the paused,
reading, alias-checking, validated, entered and offered states. Internal iterations
that keep the same visible label are merged; this does not remove them from TLC.
The [deliberately broken chart](Admission-broken.mmd) shows only counterexample
paths because its expanded graph also exceeds 500 states.

For the mutation only, `/statechart` reports the shortest `SiblingAdmission`
counterexample as three transitions: (1) observe a sibling, (2) the directory
read fails, (3) enter the admitted child. This is the same failure replayed by
the scratch production mutation, not a finding against the unchanged source.
The first `/statechart` attempt found a non-executable installed wrapper; using
its documented TypeScript entry point worked. Its first broken-view attempt
correctly refused a hidden transition on the bug path; the view now labels that
transition. Initial lint findings were fixed. None of these tool/harness errors
was counted as a verification pass.

Independent Astra review requested four improvements to the initial harness;
all were incorporated before handoff: immediate-parent evidence (including nested
faults), scheduler-priority mutation coverage, specific admission failure
identifiers rather than any failing test, and offering/pause observations.

The negative controls now also reverse state-4 queue priority and broaden the
page SQL to offer staged rows. The former must fail specifically on an **open**
while the immediate parent is still state 4; the latter must fail specifically
on an **offer** after the parent iterator failed. Unrelated pagination, timeout
or completeness failures cannot count as mutation detection. Separate broken
TLC configs cover priority and offering defects independently of early staging.
Their shortest paths have four and three transitions respectively.

Final read-only Codex GPT-6.2 Astra HIGH re-review approved this bounded sibling
admission check. The review did not independently execute the checker. Its
nonblocking coverage caveat was also fixed: a scheduled fault must actually fire
in its designated scope to count as a passing run. Faults are injected into the
root and eligible first-level directories; empty depth-two leaves receive open
and offer checks, but no injected EOF fault. This does not prove arbitrary trees.
