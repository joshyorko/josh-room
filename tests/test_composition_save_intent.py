"""Metadata-only checks of whole-Catalog admission bounds."""
import copy

import pytest
from test_composition_retirement import Authority
from test_composition_updates import reserved

from josh_room import composition_save_intent as intent_module
from josh_room.composition_safety import plan_digest
from josh_room.composition_save_intent import (
    INTENT_FORMAT,
    projected_capacity,
    validate_intent_transition,
)


def example():
    envelope = Authority().body
    _, pending = reserved()
    child_plan = pending["child_updates"]["update"]["plan"]
    intent = {
        "operation_id": "aggregate", "room_id": "parent", "baseline": "jat-parent",
        "approval_sha256": "a" * 64, "root_approval": "b" * 64, "parent_output": "jat-parent-new",
        "retained_missing_children": [],
        "children": [{"path": "child", "room_id": "child", "operation_id": "update",
                      "approved_output": "jat-update", "approval_sha256": "c" * 64, "writes_snapshot": True}],
    }
    plan = {
        "source_path_sha256": "d" * 64, "capture_policy_sha256": "e" * 64,
        "parent_room_id": "parent", "parent_logical_jat_id": "jat-parent-new",
        "expected_parent_logical_jat_id": "jat-parent", "capture_mode": "source-only",
        "children": [{"path": "child", "room_id": "child", "logical_jat_id": "jat-update"}],
    }
    intent["parent_plan_sha256"] = plan_digest(plan)
    return envelope, intent, {"update": child_plan}, plan


@pytest.mark.parametrize(("collection", "limit", "reserved_count"), [
    ("operations", 32, 1), ("child_updates", 128, 1), ("capture_claims", 192, 2),
])
@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_projected_capacity_at_each_count_bound(collection, limit, reserved_count, delta):
    envelope, intent, plans, parent = example()
    table = envelope["capture_claims"] if collection == "capture_claims" else envelope["state"].setdefault(collection, {})
    table.update({f"synthetic{i}": {} for i in range(limit - reserved_count + delta - len(table))})
    if delta == 1:
        with pytest.raises(ValueError, match="remaining operation"):
            projected_capacity(envelope, intent, plans, parent)
    else:
        projected_capacity(envelope, intent, plans, parent)


def test_capacity_accounts_for_terminal_records_and_escaped_metadata(monkeypatch):
    envelope, intent, plans, parent = example()
    envelope["state"]["dimension_catalog"]["projects"]["parent"]["display_name"] = "\u2028" * 300
    calls = []
    original = intent_module.canonical

    def capture(value):
        encoded = original(value)
        if isinstance(value, dict) and ("operations" in value or "save_intent" in value):
            calls.append(len(encoded))
        return encoded

    monkeypatch.setattr(intent_module, "canonical", capture)
    projected_capacity(envelope, intent, plans, parent)
    state_size, outer_size = calls[-2:]
    assert outer_size > state_size
    reserved = {**envelope, "format": INTENT_FORMAT, "save_intent": intent, "retirements": []}
    assert len(original(reserved)) < outer_size
    for delta in (-1, 0, 1):
        monkeypatch.setattr(intent_module, "MAX_BYTES", outer_size + delta)
        if delta < 0:
            with pytest.raises(ValueError, match="encrypted authority capacity"):
                projected_capacity(envelope, intent, plans, parent)
        else:
            projected_capacity(envelope, intent, plans, parent)


def test_competing_writer_and_early_intent_release_fail_closed():
    original, intent, _, _ = example()
    admitted = {**original, "format": INTENT_FORMAT, "save_intent": intent, "retirements": []}
    validate_intent_transition(original, admitted, "aggregate")
    with pytest.raises(ValueError, match="exact operation"):
        validate_intent_transition(admitted, admitted, "different-operation")
    with pytest.raises(ValueError, match="verified publication"):
        validate_intent_transition(admitted, {**admitted, "save_intent": None,
            "completed_saves": {"aggregate": intent["approval_sha256"]}}, "aggregate")
    competing = copy.deepcopy(admitted)
    competing["state"]["child_updates"] = {"different-child": {}}
    with pytest.raises(ValueError, match="another operation"):
        validate_intent_transition(admitted, competing, "aggregate")


def test_lost_admission_cas_only_resumes_matching_intent(monkeypatch):
    from josh_room.composition_updates import _start_save

    envelope, intent, children, parent = example()
    monkeypatch.setattr("josh_room.composition_retirement.completed_capture_claims", lambda _: set())

    class LostCAS:
        def __init__(self, accepted):
            self.accepted = accepted
            self.attempts = 0

        def put(self, body, etag, *, intent_id):
            assert etag == "synthetic-etag"
            assert intent_id == "aggregate"
            self.attempts += 1
            if self.accepted:
                self.current = body
            else:
                self.current = envelope
            raise ValueError("synthetic lost reply")

        def read(self):
            return self.current, "new-etag"

    rejected = LostCAS(False)
    with pytest.raises(ValueError, match="changed before exclusive admission"):
        _start_save(rejected, envelope, "synthetic-etag", intent, children, parent)
    assert rejected.attempts == 1
    accepted = LostCAS(True)
    writer = _start_save(accepted, envelope, "synthetic-etag", intent, children, parent)
    assert writer.operation_id == intent["operation_id"]
    assert accepted.attempts == 1


def test_completed_operation_cannot_be_admitted_again_or_overwrite_receipt():
    previous, intent, _, _ = example()
    previous["state"]["operations"]["aggregate"] = {
        "phase": "committed", "parent_pin": {"logical_jat_id": intent["parent_output"]},
    }
    receipts = {"aggregate": "a" * 64}
    previous.update(format=INTENT_FORMAT, save_intent=None, completed_saves=receipts)
    foreign = {**intent, "approval_sha256": "f" * 64}
    with pytest.raises(ValueError, match="already belongs"):
        validate_intent_transition(previous, {**previous, "save_intent": foreign}, "aggregate")
    with pytest.raises(ValueError, match="already recorded"):
        validate_intent_transition({**previous, "save_intent": foreign},
                                   {**previous, "completed_saves": {"aggregate": "f" * 64}}, "aggregate")
