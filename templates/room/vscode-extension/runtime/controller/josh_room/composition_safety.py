"""Versioned namespace state and offline composition evidence validation.

Callers must authenticate inputs. Validation alone does not establish physical
isolation, immutable payload availability or legacy migration admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .catalog import Catalog, _validate_identifier, _validate_logical_identifier
from .composition import validate_paths
from .composition_component_contract import (
    validate_component_choices,
    validate_component_plan,
    verify_artifact_identities,
)
from .logical_jat import LogicalJat
from .restic_store import RepositoryInfo, SnapshotInventoryItem
from .room_store_references import build_reference_index, compare_restic_inventory
from .workspace_onboarding import _directory

MAX_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z")
_PHASES = ("reserved", "children-recorded", "parent-prepared", "committed")
_CONTRACTS = ("composition-safety-v1", "composition-safety-v2", "composition-safety-v3", "composition-safety-v4", "composition-safety-v5")
_MODES = ("source-only", "full-repository")


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("invalid composition safety fields")


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.casefold() == "latest":
        raise ValueError("invalid exact composition identity")


def _digest(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("invalid composition digest")


def _timestamp(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("invalid composition timestamp")


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _versioned_plan(body, plan):
    if not isinstance(plan, dict):
        raise ValueError("invalid composition plan")  # noqa: TRY004 - invalid decoded ledger data
    if "plan_version" not in plan:
        return False
    version = plan["plan_version"]
    if type(version) is not int or version not in (3, 4, 5) or body["contract"] not in _CONTRACTS[version - 1:]:
        raise ValueError("unsupported composition plan version")
    return True


def _snapshot_slot(body, room, snapshot):
    for update in body.get("child_updates", {}).values():
        plan = update["plan"]
        if update["phase"] == "committed" and (plan["room_id"], plan["logical_jat_id"]) == (room, snapshot):
            return plan
    for op in body["operations"].values():
        plan = op["plan"]
        if op["phase"] == "committed" and (plan["parent_room_id"], plan["parent_logical_jat_id"]) == (room, snapshot):
            return {"capture_mode": plan["capture_mode"], "capture_policy_sha256": op["parent_policy_sha256"]}
        if plan["expected_parent_logical_jat_id"] is None or body["contract"] in _CONTRACTS[:2]:
            for slot in plan["children"]:
                if slot["path"] in op["child_pins"] and (slot["room_id"], slot["logical_jat_id"]) == (room, snapshot):
                    return slot
    raise ValueError("snapshot lacks a reserved capture contract")


def _snapshot_contract(body, room, snapshot):
    slot = _snapshot_slot(body, room, snapshot)
    return slot.get("capture_mode", "source-only"), slot.get("capture_policy_sha256")


def verify_snapshot_artifacts(state, descriptor):
    value = descriptor.to_dict()
    slot = _snapshot_slot(state.to_dict(), value["room_id"], value["logical_jat_id"])
    if "component_plan" not in slot:
        validate_component_choices(slot, value["components"], {})
    verify_artifact_identities(slot, value["components"])


def snapshot_capture_contract(state, room, snapshot):
    return _snapshot_contract(state.to_dict(), room, snapshot)


def _pin(pin, catalog, room_id, logical_id):
    _object(pin, "room_id logical_jat_id descriptor_sha256")
    _identifier(logical_id)
    _digest(pin["descriptor_sha256"])
    if pin["room_id"] != room_id or pin["logical_jat_id"] != logical_id:
        raise ValueError("composition pin differs from its reserved slot")
    try:
        record = catalog.resolve_snapshot(room_id, logical_id)
    except (KeyError, ValueError):
        raise ValueError("composition pin record is missing") from None
    if record.get("payload_kind") != "room-store-v1" or record["ciphertext_sha256"] != pin["descriptor_sha256"]:
        raise ValueError("composition pin record is missing or mismatched")
    return record


@dataclass(frozen=True)
class CompositionSafetyState:
    # Store canonical bytes, rather than retaining mutable caller-owned maps.
    _canonical: bytes

    def to_dict(self):
        return json.loads(self._canonical)

    @classmethod
    def from_dict(cls, body):
        version2 = isinstance(body, dict) and body.get('contract') in _CONTRACTS[1:]
        _object(body, "format_version contract scope revision dimension_catalog operations" + (' child_updates' if version2 else ''))
        if type(body["format_version"]) is not int or body["format_version"] != 4 or body["contract"] not in _CONTRACTS:
            raise ValueError("unsupported composition safety version")
        scope = body["scope"]
        _object(scope, "provider connection_id dimension_id encryption_domain_id repository_id catalog_key")
        if scope["provider"] not in ("r2", "minio", "s3") or scope["catalog_key"] != "catalog.jroom.age":
            raise ValueError("unsupported composition catalog authority")
        for field in ("connection_id", "dimension_id"):
            _identifier(scope[field])
        _digest(scope["repository_id"])
        # Require the serialized domain before binding; never inject a missing one.
        inner = body["dimension_catalog"]
        if not isinstance(inner, dict) or inner.get("format_version") != 3 or inner.get("encryption_domain_id") != scope["encryption_domain_id"]:
            raise ValueError("composition requires an explicit v3 domain binding")
        catalog = Catalog.from_body(inner, scope["dimension_id"], scope["encryption_domain_id"])
        legacy_fields = {"snapshot_id", "object_key", "ciphertext_sha256", "ciphertext_size", "created_at", "workspace_fingerprint"}
        for project in inner["projects"].values():
            for snapshot_id, record in project["snapshots"].items():
                _timestamp(record["created_at"])
                if record["snapshot_id"] != snapshot_id:
                    raise ValueError("composition snapshot differs from its catalog slot")
                if "payload_kind" not in record:
                    if not legacy_fields <= record.keys() or record.keys() - legacy_fields - {"origin_project_id"}:
                        raise ValueError("legacy composition evidence has unknown or missing fields")
                    if "origin_project_id" in record:
                        _validate_identifier("legacy origin project", record["origin_project_id"])
                elif "origin_room_id" in record:
                    _validate_logical_identifier("logical origin room", record["origin_room_id"])
        revision = body["revision"]
        if type(revision) is not int or revision < inner["revision"]:
            raise ValueError("invalid composition revision")
        operations = body["operations"]
        if not isinstance(operations, dict) or not 1 <= len(operations) <= 32:
            raise ValueError("invalid composition operation inventory")
        used = []
        parent_rooms, child_rooms = set(), set()
        for operation_id, operation in operations.items():
            _identifier(operation_id)
            _object(operation, "plan plan_sha256 phase child_pins parent_pin manifest_sha256 parent_policy_sha256")
            plan = operation["plan"]
            versioned = _versioned_plan(body, plan)
            _object(plan, "source_path_sha256 capture_policy_sha256 parent_room_id parent_logical_jat_id expected_parent_logical_jat_id capture_mode children" + (" plan_version" if versioned else ""))
            for field in ("source_path_sha256", "capture_policy_sha256"):
                _digest(plan[field])
            for field in ("parent_room_id", "parent_logical_jat_id"):
                _identifier(plan[field])
            baseline = plan["expected_parent_logical_jat_id"]
            if baseline is not None:
                _identifier(baseline)
                if baseline == plan["parent_logical_jat_id"]:
                    raise ValueError("parent baseline must differ from its reserved output")
                try:
                    baseline_record = catalog.resolve_snapshot(plan["parent_room_id"], baseline)
                except (KeyError, ValueError):
                    raise ValueError("composition parent baseline is missing") from None
                if baseline_record.get("payload_kind") != "room-store-v1":
                    raise ValueError("invalid parent baseline")
            preserves_parent = plan.get("plan_version") == 5 or (
                body["contract"] == "composition-safety-v5" and baseline is not None and not versioned
            )
            if (plan["capture_mode"] not in (_MODES if preserves_parent else ("source-only",))
                    or operation["plan_sha256"] != plan_digest(plan)):
                raise ValueError("composition plan or approved capture mode differs")
            children = plan["children"]
            if not isinstance(children, list) or not (0 if preserves_parent else 1) <= len(children) <= 32:
                raise ValueError("invalid composition child slots")
            for row in children:
                fields = "path room_id logical_jat_id"
                if versioned:
                    fields += " capture_mode capture_policy_sha256 approved_paths_sha256 source_records_sha256"
                if plan.get("plan_version") in (4, 5) and isinstance(row, dict) and "component_plan" in row:
                    validate_component_plan(row["component_plan"])
                    fields += " component_plan"
                _object(row, fields)
                if versioned:
                    if row["capture_mode"] not in _MODES:
                        raise ValueError("unsupported child capture mode")
                    for field in ("capture_policy_sha256", "approved_paths_sha256", "source_records_sha256"):
                        _digest(row[field])
                _identifier(row["room_id"])
                _identifier(row["logical_jat_id"])
            validate_paths(row["path"] for row in children)
            rooms = {row["room_id"] for row in children}
            if len(rooms) != len(children) or plan["parent_room_id"] in rooms:
                raise ValueError("duplicate or self-referencing composition slots")
            parent_rooms.add(plan["parent_room_id"])
            child_rooms.update(rooms)
            phase = operation["phase"]
            if phase not in _PHASES:
                raise ValueError("unsupported composition phase")
            parent_records = inner["projects"].get(plan["parent_room_id"], {}).get("snapshots", {})
            if phase != "committed" and plan["parent_logical_jat_id"] in parent_records:
                raise ValueError("parent output appeared before atomic commit")
            parent_latest = inner["projects"].get(plan["parent_room_id"], {}).get("latest")
            if phase != "committed" and baseline != parent_latest:
                raise ValueError("composition baseline differs from current parent")
            involved = rooms | {plan["parent_room_id"]}
            for previous_rooms, previous_phase, previous_parent in used:
                if involved & previous_rooms and phase != "committed" and previous_phase != "committed":
                    raise ValueError("overlapping incomplete composition operations")
                if previous_parent == (plan["parent_room_id"], plan["parent_logical_jat_id"]):
                    raise ValueError("historical parent identity is duplicated")
            used.append((involved, phase, (plan["parent_room_id"], plan["parent_logical_jat_id"])))
            pins = operation["child_pins"]
            slots = {row["path"]: row for row in children}
            if not isinstance(pins, dict) or not pins.keys() <= slots.keys() or (phase != "reserved" and pins.keys() != slots.keys()):
                raise ValueError("incomplete or unknown composition child pins")
            for path, pin in pins.items():
                slot = slots[path]
                record = _pin(pin, catalog, slot["room_id"], slot["logical_jat_id"])
                if versioned and baseline is None:
                    validate_component_choices(slot, record["component_refs"], {})
            for path, slot in slots.items():
                records = inner["projects"].get(slot["room_id"], {}).get("snapshots", {})
                if path not in pins and slot["logical_jat_id"] in records:
                    raise ValueError("child output appeared without its atomic pin association")
            for field in ("manifest_sha256", "parent_policy_sha256"):
                if phase in ("parent-prepared", "committed"):
                    _digest(operation[field])
                elif operation[field] is not None:
                    raise ValueError("composition metadata appeared before preparation")
            if phase == "committed":
                record = _pin(operation["parent_pin"], catalog, plan["parent_room_id"], plan["parent_logical_jat_id"])
                validate_component_choices({}, record["component_refs"], {})
            elif operation["parent_pin"] is not None:
                raise ValueError("uncommitted parent pin")
        if parent_rooms & child_rooms:
            raise ValueError("nested composition is unsupported")
        if version2:
            updates = body['child_updates']
            if not isinstance(updates, dict) or len(updates) > 128:
                raise ValueError('invalid child update inventory')
            pending = set()
            outputs = set()
            for key, update in updates.items():
                _identifier(key)
                _object(update, 'plan plan_sha256 phase pin')
                plan = update['plan']
                fields = 'room_id logical_jat_id expected_logical_jat_id source_path_sha256 source_records_sha256 source_identity source_signature capture_policy_sha256 content_sha256 capture_mode'
                versioned = _versioned_plan(body, plan)
                if versioned:
                    fields += ' plan_version'
                    if 'content_digest_algorithm' not in plan:
                        raise ValueError('unsupported child content digest algorithm')
                if plan.get("plan_version") == 5:
                    raise ValueError("unsupported child update version")
                if plan.get("plan_version") == 4:
                    fields += " component_plan"
                    validate_component_plan(plan.get("component_plan"))
                if isinstance(plan, dict) and 'content_digest_algorithm' in plan:
                    fields += ' content_digest_algorithm'
                    if plan['content_digest_algorithm'] != 'sha256-framed-v2':
                        raise ValueError('unsupported child content digest algorithm')
                _object(plan, fields)
                for field in ('room_id', 'logical_jat_id', 'expected_logical_jat_id'):
                    _identifier(plan[field])
                for field in ('source_path_sha256', 'source_records_sha256', 'source_signature', 'capture_policy_sha256', 'content_sha256'):
                    _digest(plan[field])
                identity = plan['source_identity']
                if not isinstance(identity, list) or len(identity) != 2 or any(type(n) is not int or n < 0 for n in identity):
                    raise ValueError('invalid child source identity')
                if plan['capture_mode'] not in (_MODES if versioned else ('source-only',)) or update['plan_sha256'] != plan_digest(plan):
                    raise ValueError('invalid child update approval')
                room, output, baseline = (plan[k] for k in ('room_id', 'logical_jat_id', 'expected_logical_jat_id'))
                if room not in child_rooms or output == baseline or (room, output) in outputs:
                    raise ValueError('invalid or duplicated child update slot')
                outputs.add((room, output))
                baseline_record = catalog.resolve_snapshot(room, baseline)
                if update['phase'] == 'reserved':
                    if update['pin'] is not None or output in inner['projects'][room]['snapshots'] or inner['projects'][room]['latest'] != baseline:
                        raise ValueError('uncommitted child output or stale baseline')
                    if room in pending or any(room in rooms and phase != 'committed' for rooms, phase, _ in used):
                        raise ValueError('overlapping incomplete child updates')
                    pending.add(room)
                elif update['phase'] == 'committed':
                    record = _pin(update['pin'], catalog, room, output)
                    validate_component_choices(plan, record["component_refs"], baseline_record["component_refs"])
                else:
                    raise ValueError('invalid child update phase')
            for update in updates.values():
                plan = update['plan']
                if 'plan_version' in plan:
                    room, baseline = plan['room_id'], plan['expected_logical_jat_id']
                    mode, _ = _snapshot_contract(body, room, baseline)
                    baseline_record = catalog.resolve_snapshot(room, baseline)
                    if mode != plan['capture_mode'] or baseline_record['capture_policy_sha256'] != plan['capture_policy_sha256']:
                        raise ValueError('child update differs from baseline capture contract')
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if len(canonical) > MAX_BYTES:
            raise ValueError("composition state exceeds size limit")
        return cls(canonical)


def reconcile(state, descriptors, repository, inventory, *, expected_scope):
    """Validate supplied evidence, report only. Never repair or infer admission."""
    if not isinstance(state, CompositionSafetyState):
        raise TypeError("validated composition state is required")
    state = CompositionSafetyState.from_dict(state.to_dict())
    body = state.to_dict()
    scope = body["scope"]
    if not isinstance(repository, RepositoryInfo) or scope != expected_scope or repository.repository_id != scope["repository_id"]:
        raise ValueError("composition evidence belongs to another authority")
    catalog = Catalog.from_body(body["dimension_catalog"], scope["dimension_id"], scope["encryption_domain_id"])
    index = build_reference_index(catalog, descriptors)
    object_evidence = {}
    for room_id, project in catalog.body["projects"].items():
        for snapshot_id, record in project["snapshots"].items():
            kind = record.get("payload_kind", "legacy-jat-hauler-v1")
            evidence = (kind, record["ciphertext_size"],
                        descriptors[(room_id, snapshot_id)].sha256 if kind == "room-store-v1" else None)
            if object_evidence.setdefault(record["object_key"], evidence) != evidence:
                raise ValueError("composition ciphertext evidence is inconsistent")
    report = compare_restic_inventory(index, repository, inventory)
    pins = [pin for op in body["operations"].values() for pin in op["child_pins"].values()]
    pins.extend(update["pin"] for update in body.get("child_updates", {}).values() if update["phase"] == "committed")
    for pin in pins:
        key = pin["room_id"], pin["logical_jat_id"]
        child = descriptors[key].to_dict()
        verify_artifact_identities(_snapshot_slot(body, *key), child["components"])
        mode, digest = _snapshot_contract(body, *key)
        if child.get("capture_mode", "full-repository") != mode or (digest and child["capture_policy_sha256"] != digest):
            raise ValueError("child capture contract differs from reservation")
    for operation in body["operations"].values():
        if operation["phase"] == "committed":
            plan = operation["plan"]
            parent = descriptors[(plan["parent_room_id"], plan["parent_logical_jat_id"])].to_dict()
            if "parent_logical_jat_id" in parent or "parent_snapshot_id" in parent["workspace"]:
                baseline = plan["expected_parent_logical_jat_id"]
                if baseline is None or parent.get("parent_logical_jat_id") != baseline:
                    raise ValueError("parent lineage differs from reserved baseline")
                prior = descriptors[(plan["parent_room_id"], baseline)].to_dict()
                if parent["workspace"].get("parent_snapshot_id") != prior["workspace"]["snapshot_id"]:
                    raise ValueError("parent workspace lineage differs from reserved baseline")
            if parent.get("capture_mode", "full-repository") != plan["capture_mode"] or parent["capture_policy_sha256"] != operation["parent_policy_sha256"]:
                raise ValueError("parent capture contract differs from reserved metadata")
            if any(parent["components"].values()):
                raise ValueError("composed parent optional components are not admitted")
    return {"ok": not report.missing_from_restic, "status": "read-only-reconciliation",
            "can_apply": False, "can_resume": False, "writes_performed": False,
            "catalog_revision": body["revision"], "missing_snapshot_count": len(report.missing_from_restic),
            "operations": [{"operation_id": key, "phase": op["phase"],
                            "validated_child_records": len(op["child_pins"]),
                            "next_action": "blocked-until-destructive-writers-fenced"}
                           for key, op in sorted(body["operations"].items())],
            "blockers": ["destructive-writers-not-fenced", "single-catalog-authority-unproven",
                         "saved-parent-metadata-not-verified", "writable-resume-not-implemented"]}


def inspect_evidence_file(path: Path):
    """Offline evidence only: arbitrary local JSON never confers provider authority."""
    if ".." in path.parts:
        raise ValueError("composition evidence path contains parent traversal")
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("safe composition evidence inspection is unavailable")
    directory = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
                raise ValueError("unsafe composition evidence file")
            raw = stream.read(MAX_BYTES + 1)
    finally:
        os.close(directory)
    if len(raw) > MAX_BYTES:
        raise ValueError("composition evidence exceeds size limit")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate composition evidence field")
            result[key] = value
        return result
    try:
        evidence = json.loads(raw, object_pairs_hook=unique)
        _object(evidence, "state descriptors repository inventory expected_scope")
        if not isinstance(evidence["descriptors"], list) or not isinstance(evidence["inventory"], list):
            raise TypeError("composition descriptor and inventory evidence must be arrays")
        state = CompositionSafetyState.from_dict(evidence["state"])
        descriptors = {}
        for value in evidence["descriptors"]:
            descriptor = LogicalJat.from_dict(value)
            key = (value["room_id"], value["logical_jat_id"])
            if key in descriptors:
                raise ValueError("duplicate descriptor evidence")
            descriptors[key] = descriptor
        inventory = []
        for row in evidence["inventory"]:
            _object(row, "snapshot_id tree_id time parent_snapshot_id")
            _digest(row["snapshot_id"])
            _digest(row["tree_id"])
            if row["parent_snapshot_id"] is not None:
                _digest(row["parent_snapshot_id"])
            _timestamp(row["time"])
            inventory.append(SnapshotInventoryItem(**row))
        result = reconcile(state, descriptors, RepositoryInfo(**evidence["repository"]),
                           inventory,
                           expected_scope=evidence["expected_scope"])
        result["evidence_trust"] = "offline-untrusted-input"
        return result
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("invalid composition reconciliation evidence") from None
