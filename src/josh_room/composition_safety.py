"""Read-only candidate v4 contracts. No admission, publish, repair or cleanup API.

Inputs must be authenticated by a future caller. Offline evidence validation is
not remote authority, migration fencing, or proof of payload availability.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from .composition import validate_paths
from .logical_jat import LogicalJat
from .restic_store import RepositoryInfo, SnapshotInventoryItem
from .room_store_references import build_reference_index, compare_restic_inventory
from .workspace_onboarding import _directory

MAX_BYTES = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_PHASES = ("reserved", "children-recorded", "parent-prepared", "committed")


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise ValueError("invalid composition safety fields")


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.casefold() == "latest":
        raise ValueError("invalid exact composition identity")


def _digest(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("invalid composition digest")


def plan_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


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


@dataclass(frozen=True)
class CompositionSafetyState:
    # Store canonical bytes, rather than retaining mutable caller-owned maps.
    _canonical: bytes

    def to_dict(self):
        return json.loads(self._canonical)

    @classmethod
    def from_dict(cls, body):
        _object(body, "format_version contract scope revision dimension_catalog operations")
        if type(body["format_version"]) is not int or body["format_version"] != 4 or body["contract"] != "composition-safety-v1":
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
            _object(plan, "source_path_sha256 capture_policy_sha256 parent_room_id parent_logical_jat_id expected_parent_logical_jat_id capture_mode children")
            for field in ("source_path_sha256", "capture_policy_sha256"):
                _digest(plan[field])
            for field in ("parent_room_id", "parent_logical_jat_id"):
                _identifier(plan[field])
            baseline = plan["expected_parent_logical_jat_id"]
            if baseline is not None:
                _identifier(baseline)
                if catalog.resolve_snapshot(plan["parent_room_id"], baseline).get("payload_kind") != "room-store-v1":
                    raise ValueError("invalid parent baseline")
            if plan["capture_mode"] != "source-only" or operation["plan_sha256"] != plan_digest(plan):
                raise ValueError("composition plan or approved capture mode differs")
            children = plan["children"]
            if not isinstance(children, list) or not 1 <= len(children) <= 32:
                raise ValueError("invalid composition child slots")
            for row in children:
                _object(row, "path room_id logical_jat_id")
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
            involved = rooms | {plan["parent_room_id"]}
            for previous_rooms, previous_phase, previous_parent in used:
                if involved & previous_rooms and (phase != "committed" or previous_phase != "committed"):
                    raise ValueError("overlapping incomplete composition operations")
                if previous_parent == (plan["parent_room_id"], plan["parent_logical_jat_id"]):
                    raise ValueError("historical parent identity is duplicated")
            used.append((involved, phase, (plan["parent_room_id"], plan["parent_logical_jat_id"])))
            pins = operation["child_pins"]
            slots = {row["path"]: row for row in children}
            if not isinstance(pins, dict) or not pins.keys() <= slots.keys() or (phase != "reserved" and pins.keys() != slots.keys()):
                raise ValueError("incomplete or unknown composition child pins")
            for path, pin in pins.items():
                _pin(pin, catalog, slots[path]["room_id"], slots[path]["logical_jat_id"])
            for field in ("manifest_sha256", "parent_policy_sha256"):
                if phase in ("parent-prepared", "committed"):
                    _digest(operation[field])
                elif operation[field] is not None:
                    raise ValueError("composition metadata appeared before preparation")
            if phase == "committed":
                _pin(operation["parent_pin"], catalog, plan["parent_room_id"], plan["parent_logical_jat_id"])
            elif operation["parent_pin"] is not None:
                raise ValueError("uncommitted parent pin")
        if parent_rooms & child_rooms:
            raise ValueError("nested composition is unsupported")
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if len(canonical) > MAX_BYTES:
            raise ValueError("composition state exceeds size limit")
        return cls(canonical)


def reconcile(state, descriptors, repository, inventory, *, expected_scope):
    """Validate supplied evidence, report only. Never repair or infer admission."""
    if not isinstance(state, CompositionSafetyState):
        raise ValueError("validated composition state is required")
    state = CompositionSafetyState.from_dict(state.to_dict())
    body = state.to_dict()
    scope = body["scope"]
    if not isinstance(repository, RepositoryInfo) or scope != expected_scope or repository.repository_id != scope["repository_id"]:
        raise ValueError("composition evidence belongs to another authority")
    catalog = Catalog.from_body(body["dimension_catalog"], scope["dimension_id"], scope["encryption_domain_id"])
    index = build_reference_index(catalog, descriptors)
    report = compare_restic_inventory(index, repository, inventory)
    for operation in body["operations"].values():
        if operation["phase"] == "committed":
            plan = operation["plan"]
            parent = descriptors[(plan["parent_room_id"], plan["parent_logical_jat_id"])].to_dict()
            if parent.get("capture_mode", "full-repository") != "source-only" or parent["capture_policy_sha256"] != operation["parent_policy_sha256"]:
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
        state = CompositionSafetyState.from_dict(evidence["state"])
        descriptors = {}
        for value in evidence["descriptors"]:
            descriptor = LogicalJat.from_dict(value)
            key = (value["room_id"], value["logical_jat_id"])
            if key in descriptors:
                raise ValueError("duplicate descriptor evidence")
            descriptors[key] = descriptor
        result = reconcile(state, descriptors, RepositoryInfo(**evidence["repository"]),
                           [SnapshotInventoryItem(**row) for row in evidence["inventory"]],
                           expected_scope=evidence["expected_scope"])
        result["evidence_trust"] = "offline-untrusted-input"
        return result
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError("invalid composition reconciliation evidence") from None
