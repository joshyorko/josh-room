import copy
import json
import tempfile
import unittest
from pathlib import Path

from contracts import BudgetExceeded, Ledger


class ResourceContract(unittest.TestCase):
    def test_limits_fail_closed_and_keep_uncertain_dispatch_spent(self):
        mission = json.loads(Path(__file__).with_name("mission.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            for resource in ["actions", "ui_calls", "batches", "evidence_bytes", "output_bytes", "model_calls"]:
                case = copy.deepcopy(mission)
                case["budgets"][resource] = 0 if resource == "model_calls" else 1
                ledger = Ledger(case, Path(folder))
                if resource != "model_calls":
                    ledger.charge(resource)  # Outcome can be uncertain; never refund.
                with self.assertRaises(BudgetExceeded):
                    ledger.charge(resource)
                self.assertEqual(ledger.counts[resource], case["budgets"][resource])

    def test_repetition_is_bounded(self):
        mission = json.loads(Path(__file__).with_name("mission.json").read_text())
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(mission, Path(folder))
            for _ in range(2):
                ledger.admit({"kind": "dismiss"})
            with self.assertRaises(BudgetExceeded):
                ledger.admit({"kind": "dismiss"})


if __name__ == "__main__":
    unittest.main()
