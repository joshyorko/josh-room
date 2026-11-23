"""Typed history-only Save orchestration; producer owns native bytes and custody."""

from __future__ import annotations

import base64
import ctypes
import dataclasses
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

SELECTION_FORMAT = "josh-room.save-agent-selection.v1"
PLAN_FORMAT = "josh-room.save-agent-plan.v2"
MAX_PLAN_BYTES = 16384
_HEX = re.compile(r"[0-9a-f]{64}\Z")
# Proof-backed Linux/MinIO history-only chooser; native resume is not implied.
SAVE_ADAPTER_QUALIFIED = True


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
        "workspace_candidate_id",
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
        - {
            "format",
            "workspace_candidate_id",
            "workspace_signature",
            "reviewed_point",
            "entries",
        }
    ):
        _fail()
    if not isinstance(value["workspace_candidate_id"], str) or not re.fullmatch(
        r"[0-9a-f]{32}", value["workspace_candidate_id"]
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
    if not SAVE_ADAPTER_QUALIFIED:
        raise ValueError("agent history Save integration is not enabled")
    if sys.platform != "linux" or dimension.provider != "minio":
        raise ValueError("agent Save requires qualified native Linux MinIO custody")
    from . import room_store_bridge as bridge
    from .composition import require_uncomposed_workspace
    from .native_agent_save_producer import TargetScope

    require_uncomposed_workspace(workspace)
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


def _workspace_receipt(context, *, workspace=None):
    descriptor = context.selected_descriptor.to_dict()
    record = context.selected_record
    result = {
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
    if workspace is not None:
        from .workspace_state import context_status

        marker = context_status(workspace)
        keys = (
            "dimension_id",
            "encryption_domain_id",
            "project_id",
            "snapshot_id",
            "workspace_signature",
            "signature_algorithm",
            "capture_policy_sha256",
        )
        result["marker_state"] = (
            "updated"
            if marker.get("path_matches") is True
            and all(marker.get(key) == result[key] for key in keys)
            else "unfinalized"
        )
    return result


def _marker_fd_record(fd):
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_size > 65536
    ):
        raise ValueError("agent Save retry marker is invalid")
    data = os.pread(fd, 65537, 0)
    after = os.fstat(fd)
    if (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or len(data) != info.st_size:
        raise ValueError("agent Save retry marker changed")
    return {
        "identity": [
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
        ],
        "data": base64.b64encode(data).decode("ascii"),
    }


def _marker_record(directory, name):
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    try:
        return _marker_fd_record(fd)
    finally:
        os.close(fd)


def _marker_move(source_fd, source, destination_fd, destination):
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None:
        raise ValueError("agent Save retry marker no-replace is unavailable")
    if rename(
        source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1
    ):
        raise OSError(ctypes.get_errno(), "agent Save retry marker no-replace refused")
    os.fsync(source_fd)
    os.fsync(destination_fd)


@contextmanager
def _marker_cleanup_directory(custody, entry, *, create=False):
    name = ".cleanup-" + entry
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=custody)
            os.fsync(custody)
        except FileExistsError:
            pass
    try:
        private = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=custody
        )
    except FileNotFoundError:
        yield None
        return
    identity = os.fstat(private)
    validated = False
    try:
        if (
            identity.st_uid != os.geteuid()
            or identity.st_mode & 0o077
            or identity.st_dev != os.fstat(custody).st_dev
            or set(os.listdir(private)) - {entry}
        ):
            raise ValueError("agent Save retry cleanup custody changed")
        validated = True
        yield private
    finally:
        try:
            attached = os.stat(name, dir_fd=custody, follow_symlinks=False)
            if validated and (attached.st_dev, attached.st_ino) == (
                identity.st_dev,
                identity.st_ino,
            ) and not os.listdir(private):
                os.rmdir(name, dir_fd=custody)
                os.fsync(custody)
        finally:
            os.close(private)


def _claimed_marker_record(custody, entry):
    with _marker_cleanup_directory(custody, entry) as private:
        return None if private is None else _marker_record(private, entry)


def _remove_claimed_marker_control(custody, entry, expected):
    """Replay a deterministic verified claim; never unlink its original name."""
    with _marker_cleanup_directory(custody, entry, create=True) as private:
        claimed = _marker_record(private, entry)
        held = os.open(
            entry,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=private if claimed is not None else custody,
        )
        try:
            if _marker_fd_record(held) != expected:
                raise ValueError("agent Save retry marker cleanup is uncertain")
            if claimed is None:
                _marker_move(custody, entry, private, entry)
            if (
                _marker_record(private, entry) != expected
                or _marker_fd_record(held) != expected
            ):
                raise ValueError("agent Save retry marker cleanup claim changed")
            os.unlink(entry, dir_fd=private)
            os.fsync(private)
        finally:
            os.close(held)


def _commit_retry_marker(workspace, plan, marker, previous, check):
    """Claim/verify an expected marker, never replace a competing binding."""
    from .workspace_state import MARKER_NAME, _validate_workspace_marker

    _validate_workspace_marker(marker)

    parent = os.open(workspace.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root = custody = None
    name = ".josh-room-marker-retry-" + plan["attempt_id"]
    try:
        root = os.open(
            workspace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        root_info = os.fstat(root)
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        custody = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        info = os.fstat(custody)
        if (
            info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_dev != root_info.st_dev
        ):
            raise ValueError("agent Save retry marker custody is unavailable")
        target = (json.dumps(marker, sort_keys=True) + "\n").encode()
        intent = {
            "format": "josh-room.save-marker-retry.v1",
            "attempt_id": plan["attempt_id"],
            "plan_sha256": plan_digest(plan),
            "target": base64.b64encode(target).decode("ascii"),
            "old": None,
        }
        # Replay only the three reserved cleanup namespaces. In particular the
        # intent may itself have been claimed before a process interruption.
        for entry in ("old-marker", "new-marker", "intent.json"):
            _claimed_marker_record(custody, entry)
        existing = _claimed_marker_record(custody, "intent.json") or _marker_record(
            custody, "intent.json"
        )
        if existing is None:
            if os.listdir(custody):
                raise ValueError("agent Save retry marker custody is not empty")
            live = _marker_record(root, MARKER_NAME)
            if live is not None and base64.b64decode(live["data"]) == target:
                check()
                attached = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (attached.st_dev, attached.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("agent Save retry custody binding changed")
                os.rmdir(name, dir_fd=parent)
                os.fsync(parent)
                return
            intent["old"] = _marker_record(root, MARKER_NAME)
            if intent["old"] is None and plan["reviewed_point"] is not None:
                raise ValueError("agent Save retry expected marker is unavailable")
            from .workspace_state import context_status

            if context_status(workspace) != previous:
                raise ValueError("agent Save retry marker changed")
            fd = os.open(
                "intent.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=custody,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(json.dumps(intent, sort_keys=True).encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(custody)
        else:
            loaded = json.loads(base64.b64decode(existing["data"]))
            if (
                not isinstance(loaded, dict)
                or set(loaded) != set(intent)
                or any(loaded[key] != intent[key] for key in set(intent) - {"old"})
            ):
                raise ValueError("agent Save retry marker custody changed")
            intent = loaded
        old = intent["old"]
        if old is not None:
            if (
                not isinstance(old, dict)
                or set(old) != {"identity", "data"}
                or not isinstance(old["identity"], list)
                or len(old["identity"]) != 5
                or any(type(value) is not int for value in old["identity"])
            ):
                raise ValueError("agent Save retry custody baseline is invalid")
            baseline = _validate_workspace_marker(
                json.loads(base64.b64decode(old["data"], validate=True))
            )
            reviewed = plan["reviewed_point"]
            if (
                reviewed is None
                or baseline.get("format_version") != 3
                or baseline.get("snapshot_id") != reviewed["logical_jat_id"]
                or any(
                    baseline.get(key) != marker[key]
                    for key in (
                        "dimension_id",
                        "encryption_domain_id",
                        "project_id",
                        "signature_algorithm",
                        "capture_policy_sha256",
                        "workspace_path_sha256",
                    )
                )
            ):
                raise ValueError("agent Save retry custody baseline changed")
        elif plan["reviewed_point"] is not None:
            raise ValueError("agent Save retry expected marker is unavailable")
        current = _marker_record(root, MARKER_NAME)
        complete = current is not None and base64.b64decode(current["data"]) == target
        if not complete:
            prepared = _marker_record(custody, "new-marker")
            if prepared is None:
                fd = os.open(
                    "new-marker",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=custody,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(target)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(custody)
            elif base64.b64decode(prepared["data"]) != target:
                raise ValueError("agent Save retry prepared marker changed")
            claimed = _marker_record(custody, "old-marker")
            if claimed is None and old is not None:
                if current != old:
                    raise ValueError("agent Save retry marker changed")
                held = os.open(
                    MARKER_NAME,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=root,
                )
                try:
                    if _marker_fd_record(held) != old:
                        raise ValueError("agent Save retry marker changed")
                    _marker_move(root, MARKER_NAME, custody, "old-marker")
                    claimed = _marker_record(custody, "old-marker")
                    if _marker_fd_record(held) != old:
                        raise ValueError("agent Save retry held marker changed")
                finally:
                    os.close(held)
            if claimed != old:
                raise ValueError("agent Save retry claimed marker changed")
            check()
            attached = os.stat(workspace, follow_symlinks=False)
            if (attached.st_dev, attached.st_ino) != (
                root_info.st_dev,
                root_info.st_ino,
            ):
                raise ValueError("agent Save retry workspace binding changed")
            _marker_move(custody, "new-marker", root, MARKER_NAME)
            complete = True
        check()
        if base64.b64decode(_marker_record(root, MARKER_NAME)["data"]) != target:
            raise ValueError("agent Save retry marker finalization is uncertain")
        # The private attempt-bound directory is never recursively removed.
        for entry in ("old-marker", "new-marker", "intent.json"):
            record = _claimed_marker_record(custody, entry) or _marker_record(
                custody, entry
            )
            if record is None:
                continue
            if (
                (entry == "old-marker" and record != old)
                or (
                    entry == "new-marker" and base64.b64decode(record["data"]) != target
                )
                or (
                    entry == "intent.json"
                    and base64.b64decode(record["data"])
                    != json.dumps(intent, sort_keys=True).encode()
                )
            ):
                raise ValueError("agent Save retry marker cleanup is uncertain")
            _remove_claimed_marker_control(custody, entry, record)
        os.fsync(custody)
        attached = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (attached.st_dev, attached.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("agent Save retry custody binding changed")
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)
    finally:
        for descriptor in (custody, root, parent):
            if descriptor is not None:
                os.close(descriptor)


def _finalize_retry_marker(scope, exact, attempt, plan):
    """Finalize only the approved candidate's local marker after a lost CAS reply."""
    from .native_workspace_content import fingerprint
    from .room_store_operations import scan_workspace_for_status
    from .workspace_state import canonical_workspace_path_sha256, context_status

    exact = attempt.verify_workspace_result(exact)
    body = exact.selected_descriptor.to_dict()
    expected = {
        "dimension_id": body["dimension_id"],
        "encryption_domain_id": body["encryption_domain_id"],
        "project_id": body["room_id"],
        "snapshot_id": body["logical_jat_id"],
        "signature_algorithm": "josh-room-stat-v1",
        "workspace_signature": exact.selected_record["workspace_signature"],
        "capture_policy_sha256": body["capture_policy_sha256"],
    }
    if (
        expected["workspace_signature"] != plan["workspace_signature"]
        or expected["capture_policy_sha256"] != plan["capture_policy_sha256"]
    ):
        raise ValueError("agent Save retry marker target changed")

    def unchanged():
        attempt.check()
        scan = scan_workspace_for_status(scope.workspace)
        if (
            scan.signature != plan["workspace_signature"]
            or scan.capture_policy_sha256 != plan["capture_policy_sha256"]
            or fingerprint(
                scope.workspace, expected_policy_sha256=plan["capture_policy_sha256"]
            )
            != plan["workspace_content_sha256"]
        ):
            raise ValueError("agent Save retry workspace changed")

    unchanged()
    previous = context_status(scope.workspace)
    already_exact = previous.get("path_matches") is True and all(
        previous.get(key) == value for key, value in expected.items()
    )
    if already_exact and not os.path.lexists(
        scope.workspace.parent / (".josh-room-marker-retry-" + plan["attempt_id"])
    ):
        return exact
    reviewed = plan["reviewed_point"]
    if already_exact or previous.get("state") == "unlinked":
        # A missing old baseline is allowed only when the private, attempt-bound
        # custody intent proves an earlier claim, or this was a new Room.
        pass
    elif (
        reviewed is not None
        and previous.get("path_matches") is True
        and previous.get("snapshot_id") == reviewed["logical_jat_id"]
        and not previous.get("composition_namespace")
        and all(
            previous.get(key) == expected[key]
            for key in (
                "dimension_id",
                "encryption_domain_id",
                "project_id",
                "signature_algorithm",
                "capture_policy_sha256",
            )
        )
    ):
        pass
    else:
        raise ValueError("agent Save retry marker was rebound or is unavailable")
    unchanged()
    if context_status(scope.workspace) != previous:
        raise ValueError("agent Save retry marker changed")
    marker = {
        "format_version": 3,
        **expected,
        "display_name": exact.catalog.body["projects"][body["room_id"]]["display_name"],
        "workspace_path_sha256": canonical_workspace_path_sha256(scope.workspace),
    }
    _commit_retry_marker(scope.workspace, plan, marker, previous, unchanged)
    unchanged()
    current = context_status(scope.workspace)
    if current.get("path_matches") is not True or any(
        current.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("agent Save retry marker finalization is uncertain")
    return attempt.verify_workspace_result(exact)


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
                workspace_result = _workspace_receipt(exact, workspace=scope.workspace)
        if state in {"workspace-uncertain", "agent-uncertain", "blocked"}:
            return partial(
                workspace_result,
                plan,
                receipt=reconciliation,
                state="uncertain",
                candidate_id=reconciliation.get("logical_jat_id"),
            )
        receipt = None
        retry_committed = exact_id is not None
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
                    if retry_committed:
                        exact = _finalize_retry_marker(scope, exact, attempt, plan)
                        workspace_result = {
                            **workspace_result,
                            "marker_state": "updated",
                        }
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


def _profile_status(dimension, room, workspace):
    """Offline profile admission; never resolve encryption material or storage."""
    result = {
        "ok": True,
        "agent_save": {
            "contract_version": 1,
            "mode": "history-only",
            "available": False,
            "profiles": [],
        },
    }
    if not SAVE_ADAPTER_QUALIFIED or sys.platform != "linux" or dimension.provider != "minio":
        result["agent_save"]["reason"] = (
            "native-save-disabled" if not SAVE_ADAPTER_QUALIFIED else
            "native-save-unsupported-platform" if sys.platform != "linux" else
            "native-save-unsupported-provider"
        )
        return result, []
    from .native_agent_discovery import room_status

    discovery = room_status(dimension=dimension.dimension_id, room=room, workspace=workspace)
    profiles = discovery.get("profiles") or []
    if not profiles:
        reason = discovery.get("reason")
        result["agent_save"]["reason"] = reason if reason in {
            "native-room-authority-unavailable", "native-room-profile-unavailable"
        } else "native-save-admission-unavailable"
        return result, []
    eligible = [
        candidate for candidate in profiles
        if candidate.get("room_reference_allowed")
        and candidate.get("scope") in {"personal", "oss", "homelab"}
        and any(tool in {"codex", "claude"} for tool in candidate.get("tools", ()))
    ]
    if not eligible:
        if any(candidate.get("scope") == "work" for candidate in profiles):
            result["agent_save"]["reason"] = "native-save-work-scope-unqualified"
        elif any(
            not candidate.get("room_reference_allowed")
            or candidate.get("scope") not in {"personal", "oss", "homelab"}
            for candidate in profiles
        ):
            result["agent_save"]["reason"] = "native-save-room-reference-not-authorized"
        else:
            result["agent_save"]["reason"] = "native-save-profile-admission-failed"
    return result, eligible


def admission_preflight(dimension, room, workspace):
    result, eligible = _profile_status(dimension, room, workspace)
    return result if not eligible else None


def status(instance, dimension, material, room, workspace):
    """Metadata-only narrow availability. Global producer capabilities stay false."""
    result, eligible = _profile_status(dimension, room, workspace)
    if not eligible:
        return result
    from .native_agent_discovery import resolve_profile
    from .native_agent_reference import _grant
    from .room_store_operations import scan_workspace_for_status
    from .workspace_policy import load_capture_policy

    agent_save = result["agent_save"]
    denials = {}
    policy = load_capture_policy(Path(workspace))
    scan = scan_workspace_for_status(Path(workspace))
    with open_scope(
        instance, dimension, material, room, workspace, policy.sha256, scan.signature
    ) as scope:
        for candidate in eligible:
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
                    admitted_grant, _binding, _admission = _grant(
                        scope.context,
                        grant["profile"],
                        tool,
                        str(Path(workspace).resolve()),
                        "room-capture",
                    )
                    if admitted_grant != grant:
                        denials[tool] = "native-save-profile-changed"
                        continue
                except (OSError, RuntimeError, ValueError):
                    denials.setdefault(tool, "native-save-profile-admission-failed")
                    continue
                agent_save["profiles"].append(
                    {
                        "tool": tool,
                        "profile_ref": candidate["profile_ref"],
                        "scope": candidate["scope"],
                        "admitted": True,
                    }
                )
    for profile in agent_save["profiles"]:
        denials.pop(profile["tool"], None)
    if denials:
        agent_save["denials"] = denials
    agent_save["available"] = bool(agent_save["profiles"])
    if not agent_save["available"]:
        agent_save["reason"] = (
            "native-save-profile-changed" if "native-save-profile-changed" in denials.values()
            else "native-save-profile-admission-failed"
        )
    return result
