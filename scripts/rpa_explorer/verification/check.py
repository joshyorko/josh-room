#!/usr/bin/env python3
"""Bounded checks against the real fence and Ledger; include real-source mutants."""
import argparse
import hashlib
import itertools
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ownership import SessionFence
from contracts import BudgetExceeded, Ledger


def exercise_fence(cls):
    original = cls("session-a", 1, "a" * 64, "0" * 64, ("attempt-a",))
    states = [original, original.observe("1" * 64), original.close(), original.observe("1" * 64).close()]
    checked = accepted = 0
    for state in states:
        for sid, gen, candidate, obs, attempts in itertools.product(
            ("session-a", "session-b"), (0, 1, 2), ("a" * 64, "b" * 64),
            ("0" * 64, "1" * 64), (["attempt-a"], ["attempt-b"], ["attempt-a", "attempt-b"])
        ):
            binding = dict(session_id=sid, generation=gen, candidate_sha256=candidate,
                           observation_sha256=obs, attempt_set=attempts)
            wanted = not state.closed and sid == state.session_id and gen == state.generation and candidate == state.candidate_sha256 and obs == state.observation_sha256 and tuple(attempts) == state.attempt_set
            got = True
            try:
                state.admit({"binding": binding})
            except ValueError:
                got = False
            checked += 1
            accepted += got
            if got != wanted:
                return {"pass": False, "checked": checked, "counterexample": {"closed": state.closed, "current": state.binding(), "submitted": binding, "accepted": got}}
    assert original.close().close() == original.close()
    try:
        original.admit({"binding": {**original.binding(), "generation": True}})
    except ValueError:
        pass
    else:
        raise AssertionError("Boolean generation accepted")
    assert accepted == 2, "Reachable current dispatch and new-observation dispatch required"
    return {"pass": True, "checked": checked, "positive_dispatches": accepted, "closed_states": 2}


def mutant(source, name, old, new):
    assert source.count(old) == 1, "Mutation seam drifted"
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source.replace(old, new), "<fence-mutation>", "exec"), module.__dict__)
    return exercise_fence(module.SessionFence)


def check_ledger():
    mission = json.loads((ROOT / "mission.json").read_text())
    ledger = Ledger(mission, ROOT)
    ledger.limits = dict(ledger.limits, ui_calls=1)
    ledger.charge("ui_calls")
    # A dispatched call may raise or time out. There is no refund operation.
    try:
        raise TimeoutError("synthetic unresolved UI dispatch")
    except TimeoutError:
        pass
    try:
        ledger.charge("ui_calls")
    except BudgetExceeded:
        assert ledger.counts["ui_calls"] == 1
    else:
        raise AssertionError("Uncertain dispatch reservation was lost")
    return {"retained_uncertain_ui_calls": 1, "next_call_rejected": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = (ROOT / "ownership.py").read_text()
    good = exercise_fence(SessionFence)
    no_comparison = mutant(source, "mutant_comparison", "or binding != self.binding():", "or False:")
    no_close = mutant(source, "mutant_close", "if self.closed:\n", "if False:\n")
    assert good["pass"] and not no_comparison["pass"] and not no_close["pass"]
    result = {"schema_version": 1, "scope": "real Python bounded receiver equality and close guards; Ledger retained dispatch charge", "fence": good,
              "mutants": {"identity-comparison-removed": no_comparison, "closed-lease-guard-removed": no_close},
              "ledger": check_ledger(), "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "ownership.py", ROOT / "contracts.py")}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"pass": True, "checked": good["checked"], "mutants_killed": 2, "receipt": str(args.output)}))


if __name__ == "__main__":
    main()
