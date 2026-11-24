---
name: verify-rpa-explorer-safety
description: Check the exploratory RCC robot's receiver binding and retained UI-call reservation when session identity, generation, observation binding, attempt sets, closure or dispatch accounting changes. Does not qualify Catalog, providers or browser cleanup.
---

# Verify the explorer receiver fence

Property: a stale, foreign or closed session cannot admit a UI request, and an
unresolved dispatched UI call remains spent. Otherwise a batch could target the
wrong installed candidate/context or retry beyond its admitted budget.

## Run the actual-code check

From repository root, choose a task-private output path and run:

```sh
python3 scripts/rpa_explorer/verification/check.py --output "$TASK_ROOT/fence-check.json"
```

This standard-library checker exercises the real `SessionFence` over 288 bounded
bindings and four reached lifecycle states. It also runs two in-memory mutations
of the real source: remove binding comparison, and remove the closed-lease guard.
Both must produce counterexamples. It positively admits the current binding
before and after a new observation. It checks `Ledger.charge` retains one
unresolved UI-call reservation and refuses the next call at its one-call bound.
It never modifies the checkout or uses a browser/provider.

## Check the small design model

Read the installed pstack `/statechart` skill and use its current invocation to
check `scripts/rpa_explorer/verification/SessionFence.tla` with these configs:

- `SessionFence.cfg`: invariants must hold.
- `SessionFence-mutant.cfg`: disabling receiver fencing must violate
  `NoStaleDispatch`; the shortest path is a foreign request followed by dispatch.
- `SessionFence-reachability.cfg`: `NotYetDispatched` must fail intentionally;
  its counterexample witnesses an accepted current request. This is a
  non-vacuity check, not a product defect.

Use the adjacent `view.mjs` to interpret the states. Read every TLA+ result with
`/statechart`. Keep logs and generated charts under task scratch, retain only
sanitized receipts, then remove tool/runtime scratch. The code checker and model
are complementary evidence; neither proves the other automatically.

## Coverage, correspondence and maintenance

- `ownership.py:SessionFence.admit` corresponds to model `Dispatch/Current`.
- `binding`, `observe`, `close` correspond to `Queue`, `Observe`, `Close`.
- Caller requirement: check the binding at the final rejectable boundary before
  UI mutation. Retain evidence before `observe` advances its digest. Class
  admission alone does not establish those call-site facts.
- `contracts.py:Ledger.charge` is checked by executable code, not represented in
  this TLA+ model. Charged failures are never refunded in the tested seam.

The bounded model has one immutable session/candidate/attempt-set identity class
versus three foreign classes, two observation values and two generations. It
models logical closure, not expiry, OS focus, physical clicks, Docker teardown,
storage retention, paid model billing or eventual completion. The browser is
assumed exclusively owned by one synchronous robot. UI changes inside one
admitted batch are not represented as independently changing observations.

Base source commit is `87c51089c2dccf6b03663544a761fee714541299`; these pilot files
are uncommitted at initial qualification. The checker records actual source
SHA256 values in its receipt. Recheck code and call-site correspondence after
changes; update the coverage record only after rerunning both positive and
negative controls. Use `/maintain-formal-verification-skill` after later drift.

## Actual product pin guard (separate bounded scope)

Run `rcc run -r scripts/rpa_explorer/robot.yaml -t contract_checks --silent` with
PILOT_OUTPUT and task-private ROBOCORP_HOME. This additionally checks the real
`src/josh_room/composition_safety.py:_pin` over 48 finite room/logical-pin/digest/
reserved-slot combinations using the repository's existing synthetic Catalog
fixture. Two exact parent/child pins are positively admitted. Removing the
reserved-identity comparison or descriptor-digest comparison in memory must
produce a counterexample. The source is never edited; the installed candidate's
copy is separately hash-bound to this checked source in the run evidence.

This is a bounded exhaustive real-code check, not a cryptographic proof or proof
of every namespace transition. Provider authentication, capture/publication
atomicity and actual restored bytes remain separate installed/live-fixture
postconditions. Do not generalize the receiver TLC model to this product guard.
