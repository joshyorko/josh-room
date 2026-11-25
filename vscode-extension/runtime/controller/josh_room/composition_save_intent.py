"""Capacity reservation and exclusive authority access for an aggregate Save."""

from __future__ import annotations

import copy
import uuid

from .composition import validate_paths
from .composition_safety import MAX_BYTES, _digest, _identifier, plan_digest
from .composition_transaction import canonical

INTENT_FORMAT = "composition-authority-v3"
MAX_OPERATIONS = 32
MAX_CHILD_UPDATES = 128
MAX_CLAIMS = 192
MAX_DESCRIPTOR_CIPHERTEXT = 2 * MAX_BYTES
COMPONENTS = ("rcc_environment", "homebrew_recovery", "hauler_content")
HASH = "f" * 64


def derived_claim(namespace, output, component):
    return uuid.uuid5(uuid.UUID(hex=namespace), f"{output}:{component}").hex


def validate_intent(intent):
    if intent is None:
        return
    if not isinstance(intent, dict) or set(intent) != {
        "operation_id", "room_id", "baseline", "approval_sha256", "root_approval", "parent_output", "children",
        "retained_missing_children", "parent_plan_sha256",
    }:
        raise ValueError("invalid Catalog Save intent")
    for key in ("operation_id", "room_id", "baseline", "parent_output"):
        _identifier(intent[key])
    for key in ("approval_sha256", "root_approval", "parent_plan_sha256"):
        _digest(intent[key])
    children = intent["children"]
    missing = intent["retained_missing_children"]
    if not isinstance(children, list) or not isinstance(missing, list) or len(children) + len(missing) > 32:
        raise ValueError("invalid Catalog Save intent inventory")
    updates, outputs = set(), {intent["parent_output"]}
    for child in children:
        if not isinstance(child, dict) or set(child) != {
            "path", "room_id", "operation_id", "approved_output", "approval_sha256", "writes_snapshot",
        } or type(child["writes_snapshot"]) is not bool:
            raise ValueError("invalid Catalog Save child intent")
        for key in ("room_id", "operation_id", "approved_output"):
            _identifier(child[key])
        _digest(child["approval_sha256"])
        if child["operation_id"] in updates or child["approved_output"] in outputs:
            raise ValueError("duplicate Catalog Save child binding")
        updates.add(child["operation_id"])
        outputs.add(child["approved_output"])
    validate_paths([child["path"] for child in children] + missing)


def require_writer(envelope, operation_id=None):
    intent = envelope.get("save_intent")
    if intent is not None and intent["operation_id"] != operation_id:
        raise ValueError("another Catalog Save intent is pending; inspect and resume that exact operation")


class IntentWriter:
    def __init__(self, authority, operation_id):
        self._authority = authority
        self.operation_id = operation_id

    def __getattr__(self, name):
        return getattr(self._authority, name)

    def put(self, body, etag):
        return self._authority.put(body, etag, intent_id=self.operation_id)

    def put_descriptor(self, descriptor):
        return self._authority.put_descriptor(descriptor, intent_id=self.operation_id)


def _largest_record(binding, room, output, *, child):
    # The catalog serializer has a closed field set. _capture never returns an
    # origin_room_id. Digests, times and accounting use their producer bounds.
    ref = {"snapshot_id": HASH, "tree_id": HASH}
    return {
        "snapshot_id": output, "payload_kind": "room-store-v1",
        "dimension_id": binding["dimension_id"], "encryption_domain_id": binding["encryption_domain_id"],
        "room_id": room, "object_key": f"objects/sha256/{HASH}", "ciphertext_sha256": HASH,
        "ciphertext_size": MAX_DESCRIPTOR_CIPHERTEXT, "created_at": "9999-12-31T23:59:59.999999999+23:59",
        "repository_id": HASH, "repository_format": 2, "workspace_snapshot_id": HASH,
        "tree_id": HASH, "capture_policy_sha256": HASH, "workspace_signature": HASH,
        "signature_algorithm": "josh-room-stat-v1", "logical_bytes": (1 << 63) - 1,
        "data_added": (1 << 63) - 1, "data_added_packed": (1 << 63) - 1,
        "component_refs": {name: ref if child else None for name in COMPONENTS},
    }


def _record_bound(state, binding, room, output, *, child):
    catalog = state["dimension_catalog"]
    project = catalog["projects"][room]
    if output in project["snapshots"]:
        return
    record = _largest_record(binding, room, output, child=child)
    project["snapshots"][output] = record
    # Replacing latest or the display name can shorten the final form relative
    # to a reservation. Keep the longer serialization in the upper envelope.
    project["latest"] = max((project["latest"], output), key=lambda value: len(canonical(value)))
    if child:
        project["display_name"] = max((project["display_name"], "Child Room"), key=lambda value: len(canonical(value)))
    catalog["revision"] += 1


def projected_capacity(envelope, intent, child_plans, parent_plan):
    """Bound every intermediate state using the union of maximum-size fields."""
    projected = copy.deepcopy(envelope)
    projected.update(format=INTENT_FORMAT, save_intent=intent)
    projected.setdefault("retirements", [])
    projected.setdefault("completed_saves", {})[intent["operation_id"]] = intent["approval_sha256"]
    state = projected["state"]
    # The child reservation may upgrade an older contract. The longest contract
    # name remains in the projection even if today's input happens to be shorter.
    state["contract"] = max(state["contract"], "composition-safety-v5", key=len)
    operations = state["operations"]
    updates = state.get("child_updates", {})
    claims = projected["capture_claims"]
    for child in intent["children"]:
        if not child["writes_snapshot"]:
            continue
        key, room, output = (child[name] for name in ("operation_id", "room_id", "approved_output"))
        plan = child_plans[key]
        if key not in updates:
            if "child_updates" not in state:
                state["child_updates"] = updates
            updates[key] = {"phase": "committed", "plan": plan, "plan_sha256": HASH,
                            "pin": {"room_id": room, "logical_jat_id": output, "descriptor_sha256": HASH}}
            state["revision"] += 2
        else:
            if updates[key]["phase"] != "committed":
                state["revision"] += 1
            updates[key]["phase"] = "committed"
            updates[key]["pin"] = {"room_id": room, "logical_jat_id": output, "descriptor_sha256": HASH}
        claims.setdefault(output, "f" * 32)
        component_modes = plan.get("component_plan", {}).get("selection", {}).get("components", {})
        for name in COMPONENTS:
            if component_modes.get(name) == "refresh":
                claims.setdefault(derived_claim(envelope["namespace"], output, name), "f" * 32)
        _record_bound(state, envelope["binding"], room, output, child=True)
    if parent_plan is not None:
        room, output = intent["room_id"], intent["parent_output"]
        key = intent["operation_id"]
        current = operations.get(key)
        if current is None:
            state["revision"] += 4
        elif current["phase"] != "committed":
            state["revision"] += {"reserved": 3, "children-recorded": 2, "parent-prepared": 1}[current["phase"]]
        operations[key] = {
            "phase": "children-recorded",  # Longest phase name.
            "plan": parent_plan, "plan_sha256": HASH, "manifest_sha256": HASH, "parent_policy_sha256": HASH,
            "child_pins": {child["path"]: {"room_id": child["room_id"], "logical_jat_id": child["logical_jat_id"],
                                         "descriptor_sha256": HASH} for child in parent_plan["children"]},
            "parent_pin": {"room_id": room, "logical_jat_id": output, "descriptor_sha256": HASH},
        }
        claims.setdefault(output, "f" * 32)
        _record_bound(state, envelope["binding"], room, output, child=False)
    if len(operations) > MAX_OPERATIONS or len(updates) > MAX_CHILD_UPDATES or len(claims) > MAX_CLAIMS:
        raise ValueError("Catalog Save exceeds remaining operation, child update or capture claim capacity")
    if len(canonical(state)) > MAX_BYTES or len(canonical(projected)) > MAX_BYTES:
        raise ValueError("Catalog Save exceeds remaining encrypted authority capacity")


def validate_intent_transition(previous, candidate, intent_id):
    before, after = previous.get("save_intent"), candidate.get("save_intent")
    if previous["format"] == INTENT_FORMAT and candidate["format"] != INTENT_FORMAT:
        raise ValueError("Catalog Save authority cannot be downgraded")
    old_receipts = previous.get("completed_saves", {})
    new_receipts = candidate.get("completed_saves", {})
    expected_receipts = old_receipts if before is None or after is not None else {
        **old_receipts, before["operation_id"]: before["approval_sha256"],
    }
    if new_receipts != expected_receipts:
        raise ValueError("completed Catalog Save approvals are immutable")
    if before is None and after is None:
        return
    intent = before or after
    if intent["operation_id"] != intent_id:
        raise ValueError("Catalog Save intent requires its exact operation")
    if before is None or after is None:
        if any(previous[key] != candidate[key] for key in
               ("namespace", "binding", "request", "secret", "repository_id", "state", "capture_claims")):
            raise ValueError("Catalog Save intent transition cannot change history")
        if previous.get("retirements", []) != candidate.get("retirements", []):
            raise ValueError("Catalog Save intent transition cannot retire recovery points")
        if after is None:
            state = candidate["state"]
            op = state["operations"].get(intent_id)
            if intent["parent_output"] != intent["baseline"] and (
                    not op or op["phase"] != "committed"
                    or op["parent_pin"]["logical_jat_id"] != intent["parent_output"]):
                raise ValueError("Catalog Save intent cannot clear before verified publication")
            for row in intent["children"]:
                if row["writes_snapshot"]:
                    update = state["child_updates"].get(row["operation_id"])
                    if not update or update["phase"] != "committed" or update["pin"]["logical_jat_id"] != row["approved_output"]:
                        raise ValueError("Catalog Save intent cannot clear before its child publications")
        return
    for key in ("namespace", "binding", "request", "secret", "repository_id"):
        if previous[key] != candidate[key]:
            raise ValueError("Catalog Save authority identity is immutable")
    if before != after:
        raise ValueError("Catalog Save intent is immutable")
    if previous.get("retirements", []) != candidate.get("retirements", []):
        raise ValueError("retirement is blocked while Catalog Save is pending")
    allowed_updates = {row["operation_id"] for row in intent["children"] if row["writes_snapshot"]}
    for kind, allowed in (("operations", {intent_id}), ("child_updates", allowed_updates)):
        older, newer = (body["state"].get(kind, {}) for body in (previous, candidate))
        if any(older.get(key) != newer.get(key) for key in (older.keys() | newer.keys()) - allowed):
            raise ValueError("Catalog Save intent does not admit another operation")
    state = candidate["state"]
    previous_claims = previous["capture_claims"]
    new_claims = candidate["capture_claims"]
    if any(new_claims.get(name) != value for name, value in previous_claims.items()):
        raise ValueError("Catalog Save capture claims are immutable")
    allowed_claims = set()
    previous_state = previous["state"]
    parent_before = previous_state["operations"].get(intent_id)
    if parent_before and parent_before["phase"] == "parent-prepared":
        allowed_claims.add(intent["parent_output"])
    for row in intent["children"]:
        if not row["writes_snapshot"]:
            continue
        update = previous_state.get("child_updates", {}).get(row["operation_id"])
        if not update or update["phase"] != "reserved":
            continue
        output = row["approved_output"]
        allowed_claims.add(output)
        if output in previous_claims:
            modes = update["plan"].get("component_plan", {}).get("selection", {}).get("components", {})
            allowed_claims.update(derived_claim(previous["namespace"], output, name)
                                  for name in COMPONENTS if modes.get(name) == "refresh")
    added_claims = set(new_claims) - set(previous_claims)
    if added_claims - allowed_claims or len(added_claims) > 1 or (added_claims and state != previous_state):
        raise ValueError("Catalog Save intent does not admit another capture claim")
    for child in intent["children"]:
        update = state.get("child_updates", {}).get(child["operation_id"])
        if not child["writes_snapshot"] or update is None:
            continue
        plan = update["plan"]
        approval = plan_digest({"action": "save-child", "namespace": previous["namespace"], "binding": previous["binding"],
                                "operation_id": child["operation_id"], "plan": plan})
        if (approval != child["approval_sha256"] or plan["room_id"] != child["room_id"]
                or plan["logical_jat_id"] != child["approved_output"]):
            raise ValueError("child reservation differs from its Catalog Save intent")
    parent = state["operations"].get(intent_id)
    if parent is not None:
        plan = parent["plan"]
        if plan_digest(plan) != intent["parent_plan_sha256"]:
            raise ValueError("parent plan digest differs from its Catalog Save intent")
        if (plan["parent_room_id"], plan["expected_parent_logical_jat_id"], plan["parent_logical_jat_id"]) != (
                intent["room_id"], intent["baseline"], intent["parent_output"]):
            raise ValueError("parent reservation differs from its Catalog Save intent")
        source = next((op for op in state["operations"].values() if op["phase"] == "committed"
                       and op["parent_pin"]["room_id"] == intent["room_id"]
                       and op["parent_pin"]["logical_jat_id"] == intent["baseline"]), None)
        if source is None:
            raise ValueError("Catalog Save intent baseline is unavailable")
        pins = {slot["path"]: slot["logical_jat_id"] for slot in source["plan"]["children"]}
        rooms = {slot["path"]: slot["room_id"] for slot in source["plan"]["children"]}
        approved = {child["path"]: child["approved_output"] for child in intent["children"]}
        if (set(pins) != set(approved) | set(intent["retained_missing_children"])
                or {slot["path"]: slot["room_id"] for slot in plan["children"]} != rooms
                or {slot["path"]: slot["logical_jat_id"] for slot in plan["children"]} != {**pins, **approved}):
            raise ValueError("parent pins differ from its Catalog Save intent")
