"""Version 1 mission/action/observation admission and resource accounting."""
import hashlib
import json
import time
from collections import Counter


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    def __init__(self, mission, output):
        if mission.get("schema_version") != 1 or mission.get("target") != "task-owned-code-server":
            raise ValueError("Unsupported mission")
        self.mission = mission
        self.output = output
        self.started = time.monotonic()
        self.counts = Counter()
        self.repeats = Counter()
        self.decisions = []
        self.records = []
        self.limits = mission["budgets"]
        if set(self.limits) != {"seconds", "actions", "ui_calls", "batches", "model_calls", "evidence_bytes", "repetitions", "output_bytes"}:
            raise ValueError("Incomplete budgets")
        if any(type(x) is not int or x < 0 for x in self.limits.values()):
            raise ValueError("Budgets must be nonnegative integers")
        if self.limits["seconds"] > 900 or self.limits["actions"] > 100 or self.limits["model_calls"] != 0:
            raise ValueError("Pilot ceiling exceeded")

    def charge(self, resource, amount=1):
        if time.monotonic() - self.started > self.limits["seconds"]:
            raise BudgetExceeded("seconds")
        if self.counts[resource] + amount > self.limits[resource]:
            raise BudgetExceeded(resource)
        self.counts[resource] += amount

    def admit(self, action):
        signature = json.dumps(action, sort_keys=True)
        if self.repeats[signature] >= self.limits["repetitions"]:
            raise BudgetExceeded("repetitions")
        self.charge("actions")
        self.repeats[signature] += 1

    def write(self, name, data):
        self.charge("evidence_bytes", len(data))
        (self.output / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def persist(self, **extra):
        summary = {"schema_version": 1, "mission": self.mission, "counts": dict(self.counts),
                   "elapsed_seconds": round(time.monotonic() - self.started, 2),
                   "decisions": self.decisions, "batches": self.records, **extra}
        (self.output / "ledger.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
