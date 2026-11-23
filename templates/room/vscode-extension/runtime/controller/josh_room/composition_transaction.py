"""Monotonic metadata transitions for isolated namespace capture and fixtures.

Physical authority and immutable payload verification belong to the lifecycle;
these transitions alone do not admit legacy repository migration.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .catalog import Catalog
from .composition import validate_manifest
from .composition_safety import CompositionSafetyState, _identifier, plan_digest


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def reserve(catalog, scope, operation_id, plan, *, previous=None):
    """Allocate all immutable slots before capture; never migrate a provider.

    The returned candidate must be durably CASed before any synthetic payload
    operation. Reusing an operation ID with any changed plan is refused.
    """
    _identifier(operation_id)
    if previous is None:
        body = {"format_version": 4, "contract": "composition-safety-v1",
                "scope": copy.deepcopy(scope), "revision": catalog.body["revision"],
                "dimension_catalog": copy.deepcopy(catalog.body), "operations": {}}
    else:
        body = previous.to_dict()
        if body["scope"] != scope or body["dimension_catalog"] != catalog.body:
            raise ValueError("reservation authority or catalog changed")
    operation = {"plan": copy.deepcopy(plan), "plan_sha256": plan_digest(plan),
                 "phase": "reserved", "child_pins": {}, "parent_pin": None,
                 "manifest_sha256": None, "parent_policy_sha256": None}
    existing = body["operations"].get(operation_id)
    if existing is not None:
        if existing["plan"] != plan:
            raise ValueError("operation ID already binds another plan")
        return CompositionSafetyState.from_dict(body)
    body["operations"][operation_id] = operation
    body["revision"] += 1
    return CompositionSafetyState.from_dict(body)


def _operation(state, operation_id, approved_plan_sha256):
    body = CompositionSafetyState.from_dict(state.to_dict()).to_dict()
    operation = body["operations"].get(operation_id)
    if operation is None or operation["plan_sha256"] != approved_plan_sha256:
        raise ValueError("operation or approved plan differs")
    return body, operation


def manifest_for(state, operation_id, approved_plan_sha256):
    """Exact reserved pins, independent of every Room's current latest value."""
    body, operation = _operation(state, operation_id, approved_plan_sha256)
    slots = operation["plan"]["children"]
    if len(operation["child_pins"]) != len(slots):
        raise ValueError("children are not all recorded")
    scope = body["scope"]
    return validate_manifest({"format": "room-composition-v1", "children": [
        {"path": slot["path"], "provider": scope["provider"],
         "connection_id": scope["connection_id"], "dimension_id": scope["dimension_id"],
         "room_id": slot["room_id"], "snapshot_id": slot["logical_jat_id"],
         "hydration": "explicit"} for slot in slots]})


def record_child(state, operation_id, approved_plan_sha256, path, *,
                 descriptor, object_ref, workspace_signature, display_name):
    """Add a child record and its retention association in ONE candidate CAS."""
    body, operation = _operation(state, operation_id, approved_plan_sha256)
    slot = next((row for row in operation["plan"]["children"] if row["path"] == path), None)
    if slot is None:
        raise ValueError("child slot is not reserved")
    return _record(body, operation, slot, descriptor, object_ref,
                   workspace_signature, display_name, child_path=path)


def _record(body, operation, slot, descriptor, object_ref, signature, name, *, child_path=None):
    value = descriptor.to_dict()
    if value["room_id"] != slot["room_id"] or value["logical_jat_id"] != slot["logical_jat_id"]:
        raise ValueError("descriptor differs from reserved slot")
    if value['workspace']['repository_id'] != body['scope']['repository_id']:
        raise ValueError('descriptor repository differs from reservation')
    if child_path is None:
        if operation['parent_pin'] is None and operation['phase'] != 'parent-prepared':
            raise ValueError('parent is not prepared')
        baseline = operation['plan']['expected_parent_logical_jat_id']
        if (value.get('parent_logical_jat_id') is not None or 'parent_snapshot_id' in value['workspace']):
            if baseline is None or value.get('parent_logical_jat_id') != baseline:
                raise ValueError('parent lineage differs from reservation')
            prior = body['dimension_catalog']['projects'][slot['room_id']]['snapshots'][baseline]
            if value['workspace'].get('parent_snapshot_id') != prior['workspace_snapshot_id']:
                raise ValueError('parent workspace lineage differs from reservation')
        if (value.get("capture_mode") != "source-only"
                or value["capture_policy_sha256"] != operation["parent_policy_sha256"]
                or any(value["components"].values())):
            raise ValueError("parent capture differs from preparation")
    catalog = Catalog.from_body(body["dimension_catalog"])
    candidate = catalog.add_logical_snapshot(slot["room_id"], name, descriptor, object_ref, signature)
    record = candidate.resolve_snapshot(slot["room_id"], slot["logical_jat_id"])
    pin = {"room_id": slot["room_id"], "logical_jat_id": slot["logical_jat_id"],
           "descriptor_sha256": object_ref.sha256}
    existing_pin = operation["child_pins"].get(child_path) if child_path is not None else operation["parent_pin"]
    if existing_pin is not None:
        if existing_pin != pin or catalog.resolve_snapshot(slot["room_id"], slot["logical_jat_id"]) != record:
            raise ValueError("immutable recorded output differs")
        return CompositionSafetyState.from_dict(body)
    if child_path is not None:
        if operation["phase"] != "reserved":
            raise ValueError("child cannot be recorded in this phase")
        operation["child_pins"][child_path] = pin
        if len(operation["child_pins"]) == len(operation["plan"]["children"]):
            operation["phase"] = "children-recorded"
    else:
        if operation["phase"] != "parent-prepared":
            raise ValueError("parent is not prepared")
        operation["parent_pin"] = pin
        operation["phase"] = "committed"
    body["dimension_catalog"] = candidate.body
    body["revision"] += 1
    return CompositionSafetyState.from_dict(body)


def prepare_parent(state, operation_id, approved_plan_sha256, *, policy_sha256):
    """Bind intended bytes only; this does NOT prove an immutable tree contains them."""
    manifest = manifest_for(state, operation_id, approved_plan_sha256)
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    body, operation = _operation(state, operation_id, approved_plan_sha256)
    if operation["phase"] in ("parent-prepared", "committed"):
        if operation["manifest_sha256"] != digest or operation["parent_policy_sha256"] != policy_sha256:
            raise ValueError("prepared metadata differs")
        return CompositionSafetyState.from_dict(body)
    if operation["phase"] != "children-recorded":
        raise ValueError("children are not all recorded")
    operation.update(phase="parent-prepared", manifest_sha256=digest, parent_policy_sha256=policy_sha256)
    body["revision"] += 1
    return CompositionSafetyState.from_dict(body)


def record_parent(state, operation_id, approved_plan_sha256, *, descriptor,
                  object_ref, workspace_signature, display_name):
    """Record synthetic parent output and committed phase atomically.

    Production callers additionally need authenticated immutable-tree checks,
    reachable payloads and a qualified exclusive authority; none is inferred here.
    """
    body, operation = _operation(state, operation_id, approved_plan_sha256)
    plan = operation["plan"]
    slot = {"room_id": plan["parent_room_id"], "logical_jat_id": plan["parent_logical_jat_id"]}
    return _record(body, operation, slot, descriptor, object_ref, workspace_signature, display_name)


def persist_candidate(store, expected_state, expected_etag, candidate):
    """Synthetic CAS + exact readback; reconcile uncertain writes without retry.

    Store read() returns a paired (body-or-None, ETag). compare_and_swap accepts
    that ETag and a body. A conflicting/ambiguous outcome never authorizes another
    write. Process loss (BaseException) propagates; reopening/repeating reads the
    exact desired candidate before doing anything else.
    """
    desired = CompositionSafetyState.from_dict(candidate.to_dict()).to_dict()
    expected = None if expected_state is None else expected_state.to_dict()
    _require_extension(expected, desired)
    observed, etag = store.read()
    if canonical(observed) == canonical(desired):
        return candidate, etag
    if canonical(observed) != canonical(expected) or etag != expected_etag:
        raise ValueError("composition CAS baseline changed")
    try:
        store.compare_and_swap(expected_etag, desired)
    except (OSError, RuntimeError, ValueError):
        observed, etag = store.read()
        if canonical(observed) != canonical(desired):
            raise ValueError("composition CAS outcome unconfirmed; reconcile before retry") from None
        return candidate, etag
    observed, etag = store.read()
    if canonical(observed) != canonical(desired):
        raise ValueError("composition CAS readback differs; reconcile before retry")
    return candidate, etag


def _require_extension(previous, candidate):
    """A CAS must never erase historical records, pins or operation bindings."""
    for key, operation in candidate['operations'].items():
        if operation['phase'] in ('parent-prepared', 'committed'):
            expected_manifest = manifest_for(CompositionSafetyState.from_dict(candidate), key, operation['plan_sha256'])
            if operation['manifest_sha256'] != hashlib.sha256(canonical(expected_manifest)).hexdigest():
                raise ValueError('prepared manifest differs from reserved pins')
    if previous is None:
        operations = list(candidate['operations'].values())
        if len(operations) != 1 or operations[0]['phase'] != 'reserved' or operations[0]['child_pins']:
            raise ValueError('initial CAS requires an unfulfilled reservation')
        return
    if previous == candidate:
        return
    if candidate['scope'] != previous['scope'] or candidate['revision'] != previous['revision'] + 1:
        raise ValueError('candidate authority or revision differs')
    for room, project in previous['dimension_catalog']['projects'].items():
        snapshots = candidate['dimension_catalog']['projects'].get(room, {}).get('snapshots', {})
        if any(snapshots.get(key) != record for key, record in project['snapshots'].items()):
            raise ValueError('historical catalog output cannot be changed or removed')
    phases = ('reserved', 'children-recorded', 'parent-prepared', 'committed')
    changed = 0
    for key, old in previous['operations'].items():
        new = candidate['operations'].get(key)
        if new is None or old['plan'] != new['plan']:
            raise ValueError('historical operation binding cannot be changed or removed')
        if any(new['child_pins'].get(path) != pin for path, pin in old['child_pins'].items()):
            raise ValueError('historical child pin cannot be changed or removed')
        if old['phase'] in ('parent-prepared', 'committed') and any(
            old[field] != new[field] for field in ('manifest_sha256', 'parent_policy_sha256')
        ):
            raise ValueError('prepared metadata cannot change')
        if old['phase'] == 'committed' and old != new:
            raise ValueError('committed operation is immutable')
        if not 0 <= phases.index(new['phase']) - phases.index(old['phase']) <= 1:
            raise ValueError('candidate cannot skip or reverse phases')
        changed += old != new
    old_updates = previous.get('child_updates', {})
    new_updates = candidate.get('child_updates', {})
    if previous['contract'] != candidate['contract'] and not (
        previous['contract'] == 'composition-safety-v1' and candidate['contract'] == 'composition-safety-v2'
    ):
        raise ValueError('composition contract cannot be downgraded')
    for key, old in old_updates.items():
        new = new_updates.get(key)
        if new is None or old['plan'] != new['plan'] or old['plan_sha256'] != new['plan_sha256']:
            raise ValueError('child update binding cannot change')
        if old['phase'] == 'committed' and old != new:
            raise ValueError('committed child update is immutable')
        if old != new and (old['phase'], new['phase']) != ('reserved', 'committed'):
            raise ValueError('invalid child update transition')
        changed += old != new
    new_update_keys = new_updates.keys() - old_updates.keys()
    if any(new_updates[key]['phase'] != 'reserved' for key in new_update_keys):
        raise ValueError('new child update must be reserved')
    added = candidate['operations'].keys() - previous['operations'].keys()
    if changed + len(added) + len(new_update_keys) != 1 or any(candidate['operations'][key]['phase'] != 'reserved' for key in added):
        raise ValueError('one operation transition is required per CAS')
