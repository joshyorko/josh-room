"""Typed history-only Save orchestration; producer owns native bytes and custody."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path

SELECTION_FORMAT = "josh-room.save-agent-selection.v1"
PLAN_FORMAT = "josh-room.save-agent-plan.v1"
MAX_PLAN_BYTES = 16384
_HEX = re.compile(r"[0-9a-f]{64}\Z")
# Parent qualification must precede changing this narrow UI availability gate.
SAVE_ADAPTER_QUALIFIED = False


def _fail():
    raise ValueError("agent Save selection or reviewed plan is invalid")


def _digest(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _decode(value):
    if isinstance(value, str):
        if len(value.encode()) > MAX_PLAN_BYTES:
            _fail()

        def unique(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    _fail()
                result[key] = item
            return result

        value = json.loads(value, object_pairs_hook=unique)
    if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_PLAN_BYTES:
        _fail()
    return value


def selection_request(value):
    if value is None:
        return None
    value = _decode(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "entries"}
        or value["format"] != SELECTION_FORMAT
    ):
        _fail()
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
        _fail()
    tools = set()
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"tool", "profile_ref"}
            or not isinstance(entry["tool"], str)
            or entry["tool"] not in {"codex", "claude"}
            or entry["tool"] in tools
            or not _digest(entry["profile_ref"])
        ):
            _fail()
        tools.add(entry["tool"])
    return {
        "format": SELECTION_FORMAT,
        "entries": sorted(
            [dict(entry) for entry in entries], key=lambda item: item["tool"]
        ),
    }


def reviewed_plan(value, selection=None):
    if value is None:
        return None
    value = _decode(value)
    fields = {
        "format",
        "attempt_id",
        "target_scope_sha256",
        "workspace_content_sha256",
        "capture_policy_sha256",
        "workspace_signature",
        "reviewed_point",
        "entries",
        "digest",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["format"] != PLAN_FORMAT
    ):
        _fail()
    if any(
        not _digest(value[key])
        for key in fields
        - {"format", "workspace_signature", "reviewed_point", "entries"}
    ):
        _fail()
    signature = value["workspace_signature"]
    if not isinstance(signature, str) or not re.fullmatch(
        r"[A-Za-z0-9:._-]{1,256}", signature
    ):
        _fail()
    point = value["reviewed_point"]
    if point is not None and (
        not isinstance(point, dict)
        or set(point) != {"logical_jat_id", "binding_sha256", "sidecar_sha256"}
        or not isinstance(point["logical_jat_id"], str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", point["logical_jat_id"]
        )
        or not _digest(point["binding_sha256"])
        or not _digest(point["sidecar_sha256"])
    ):
        _fail()
    entries = value["entries"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 2:
        _fail()
    selectors = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {
                "tool",
                "profile_ref",
                "content_sha256",
                "authority_sha256",
                "new_point_capture_id",
                "existing_point",
            }
            or any(
                not _digest(entry[key])
                for key in (
                    "profile_ref",
                    "content_sha256",
                    "authority_sha256",
                    "new_point_capture_id",
                )
            )
        ):
            _fail()
        selectors.append({"tool": entry["tool"], "profile_ref": entry["profile_ref"]})
        existing = entry["existing_point"]
        if existing is not None and (
            point is None
            or not isinstance(existing, dict)
            or set(existing) != {"capture_id", "outcome"}
            or not _digest(existing["capture_id"])
            or not isinstance(existing["outcome"], str)
            or existing["outcome"] not in {"created", "reused"}
        ):
            _fail()
    normalized = selection_request({"format": SELECTION_FORMAT, "entries": selectors})
    if selection is not None and normalized != selection_request(selection):
        _fail()
    return json.loads(json.dumps(value))


def plan_digest(value):
    value = reviewed_plan(value)
    return hashlib.sha256(
        json.dumps([PLAN_FORMAT, value], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _with_reviewed(scope, reviewed_id):
    if reviewed_id is None:
        return scope
    from . import room_store_bridge as bridge

    context = scope.context
    record = context.catalog.resolve_snapshot(context.project_id, reviewed_id)
    descriptor = bridge._load_descriptor(
        context.backend,
        context.instance,
        context.material,
        context.project_id,
        record,
        context.private_dir,
    )
    return dataclasses.replace(
        scope,
        reviewed_context=dataclasses.replace(
            context,
            selected_record=record,
            selected_descriptor=descriptor,
        ),
    )


@contextmanager
def open_scope(
    instance,
    dimension,
    material,
    room,
    workspace,
    policy_sha256,
    signature,
    *,
    reviewed_id=None,
):
    from . import room_store_bridge as bridge
    from .composition import require_uncomposed_workspace
    from .native_agent_save_producer import TargetScope

    require_uncomposed_workspace(workspace)
    if sys.platform != "linux" or dimension.provider not in {"minio", "s3"}:
        raise ValueError("agent Save requires qualified native Linux MinIO/S3 custody")
    with bridge.open_existing_room_store(
        instance, dimension, material, project_id=None
    ) as opened:
        context = dataclasses.replace(opened, project_id=room)
        scope = TargetScope(context, Path(workspace), policy_sha256, signature)
        yield _with_reviewed(scope, reviewed_id)


def preview(
    instance, dimension, material, room, workspace, selection, output, reviewed_id
):
    from .native_agent_save_producer import preview_batch

    selection = selection_request(selection)
    with open_scope(
        instance,
        dimension,
        material,
        room,
        workspace,
        output["capture_policy_sha256"],
        output["workspace_signature"],
        reviewed_id=reviewed_id,
    ) as scope:
        return reviewed_plan(preview_batch(scope, selection), selection)


def partial(
    workspace_result,
    plan,
    *,
    receipt=None,
    state="failed",
    candidate_id=None,
    cancelled=False,
):
    tools = [
        {
            "tool": entry["tool"],
            "profile_ref": entry["profile_ref"],
            "capture_id": (
                entry["existing_point"]["capture_id"]
                if plan["reviewed_point"] is not None
                and candidate_id == plan["reviewed_point"]["logical_jat_id"]
                and entry["existing_point"] is not None
                else entry["new_point_capture_id"]
                if candidate_id
                else None
            ),
            "new_point_capture_id": entry["new_point_capture_id"],
            "content_sha256": entry["content_sha256"],
            "state": state,
            "data_uploaded": None,
        }
        for entry in plan["entries"]
    ]
    receipt = receipt or {
        "ok": False,
        "state": state,
        "attempt_id": plan["attempt_id"],
        "entries": tools,
        "history_only": True,
        "native_resume_verified": False,
    }
    return {
        "ok": False,
        "error": "agent-save-partial",
        "status": "agent-save-partial",
        "workspace_result": workspace_result,
        "agent_result": receipt,
        "attempt_id": plan["attempt_id"],
        "candidate_logical_jat_id": candidate_id,
        "cancelled": cancelled,
        "reconciliation_required": state == "uncertain"
        or receipt.get("reconciliation_required", False),
    }


def _workspace_receipt(context):
    descriptor = context.selected_descriptor.to_dict()
    record = context.selected_record
    return {
        "ok": True,
        "status": "already-saved",
        "dimension_id": descriptor["dimension_id"],
        "encryption_domain_id": descriptor["encryption_domain_id"],
        "project_id": descriptor["room_id"],
        "snapshot_id": descriptor["logical_jat_id"],
        "workspace_snapshot_id": descriptor["workspace"]["snapshot_id"],
        "workspace_signature": record["workspace_signature"],
        "signature_algorithm": "josh-room-stat-v1",
        "capture_policy_sha256": descriptor["capture_policy_sha256"],
        "data_added_bytes": 0,
        "has_external_components": any(
            descriptor.get("components", {}).get(name) is not None
            for name in ("homebrew_recovery", "hauler_content")
        ),
    }


def save(
    instance, dimension, material, room, workspace, selection, plan, workspace_save
):
    from . import room_store_bridge as bridge
    from .native_agent_save_producer import (
        prepare_batch,
        publish_batch,
        reconcile_attempt,
    )

    selection = selection_request(selection)
    plan = reviewed_plan(plan, selection)
    if plan is None:
        _fail()
    reviewed_id = (
        plan["reviewed_point"]["logical_jat_id"] if plan["reviewed_point"] else None
    )
    with open_scope(
        instance,
        dimension,
        material,
        room,
        workspace,
        plan["capture_policy_sha256"],
        plan["workspace_signature"],
    ) as scope:
        reconciliation = reconcile_attempt(scope, selection, plan)
        state = reconciliation["state"]
        workspace_result = None
        exact_id = (
            reconciliation.get("logical_jat_id")
            if reconciliation.get("workspace_committed") is True
            else None
        )
        if exact_id is not None:
            with bridge.open_existing_room_store(
                instance, dimension, material, project_id=room, snapshot_id=exact_id
            ) as exact:
                workspace_result = _workspace_receipt(exact)
        if state in {"workspace-uncertain", "agent-uncertain", "blocked"}:
            return partial(
                workspace_result,
                plan,
                receipt=reconciliation,
                state="uncertain",
                candidate_id=reconciliation.get("logical_jat_id"),
            )
        receipt = None
        try:
            # A durable committed candidate is authoritative independently of
            # the former reviewed parent, which may have since been removed.
            if exact_id is None:
                scope = _with_reviewed(scope, reviewed_id)
            with prepare_batch(scope, selection, plan) as attempt:
                attempt.check()
                if exact_id is None:
                    workspace_result = workspace_save(attempt)
                    exact_id = workspace_result.get("snapshot_id")
                if not exact_id or workspace_result.get("status") not in {
                    "saved",
                    "already-saved",
                }:
                    return partial(
                        workspace_result,
                        plan,
                        state="prepared-unpublished",
                        candidate_id=exact_id,
                    )
                with bridge.open_existing_room_store(
                    instance, dimension, material, project_id=room, snapshot_id=exact_id
                ) as exact:
                    attempt.workspace_outcome("committed", logical_jat_id=exact_id)
                    attempt.check()
                    receipt = publish_batch(exact, attempt)
                    if receipt.get("ok") is not True:
                        return partial(
                            workspace_result,
                            plan,
                            receipt=receipt,
                            state="uncertain",
                            candidate_id=exact_id,
                        )
                    result = {**workspace_result, "agent_result": receipt}
            return result
        except (Exception, KeyboardInterrupt) as error:
            from .cancellation import CLICancelled

            outcome = getattr(error, "result", None) or {}
            candidate = (
                outcome.get("candidate_logical_jat_id")
                or outcome.get("logical_jat_id")
                or exact_id
            )
            if (
                candidate is not None
                or workspace_result is not None
                or outcome.get("publication_state")
                in {
                    "uncertain",
                    "committed",
                    "committed-marker-stale",
                    "committed-verification-unknown",
                }
            ):
                return partial(
                    workspace_result,
                    plan,
                    receipt=getattr(error, "receipt", None) or receipt,
                    state="uncertain",
                    candidate_id=candidate,
                    cancelled=isinstance(error, CLICancelled),
                )
            raise


def status(instance, dimension, material, room, workspace):
    """Metadata-only narrow availability. Global producer capabilities stay false."""
    result = {
        "ok": True,
        "agent_save": {
            "contract_version": 1,
            "mode": "history-only",
            "available": False,
            "profiles": [],
        },
    }
    if (
        not SAVE_ADAPTER_QUALIFIED
        or sys.platform != "linux"
        or dimension.provider not in {"minio", "s3"}
    ):
        return result
    from .native_agent_discovery import resolve_profile, room_status
    from .native_agent_reference import _grant
    from .room_store_operations import scan_workspace_for_status
    from .workspace_policy import load_capture_policy

    policy = load_capture_policy(Path(workspace))
    scan = scan_workspace_for_status(Path(workspace))
    with open_scope(
        instance, dimension, material, room, workspace, policy.sha256, scan.signature
    ) as scope:
        for candidate in room_status(
            dimension=dimension.dimension_id, room=room, workspace=workspace
        )["profiles"]:
            if not candidate.get("room_reference_allowed"):
                continue
            for tool in candidate["tools"]:
                if tool not in {"codex", "claude"}:
                    continue
                try:
                    grant = resolve_profile(
                        dimension=dimension.dimension_id,
                        room=room,
                        workspace=workspace,
                        profile_ref=candidate["profile_ref"],
                        tool=tool,
                    )
                    _grant(
                        scope.context,
                        grant["profile"],
                        tool,
                        str(Path(workspace).resolve()),
                        "room-capture",
                    )
                except (OSError, RuntimeError, ValueError):
                    continue
                result["agent_save"]["profiles"].append(
                    {
                        "tool": tool,
                        "profile_ref": candidate["profile_ref"],
                        "scope": candidate["scope"],
                        "admitted": True,
                    }
                )
    result["agent_save"]["available"] = bool(result["agent_save"]["profiles"])
    return result
