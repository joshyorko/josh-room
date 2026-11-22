"""Reviewed, durable, two-tool native-history producer for ordinary Room Save.

This module owns no workspace Save orchestration or credential handoff. Callers
must keep prepare_batch entered through their exact workspace publication/result.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import dataclasses
import hashlib
import os
import re
import secrets
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from . import native_agent_discovery as discovery
from . import native_agent_reference as references
from . import native_agents as native
from .cancellation import CLICancelled, sigterm_cancellation
from .native_workspace_content import fingerprint
from .private_paths import verify_private_path

SELECTION_FORMAT = "josh-room.save-agent-selection.v1"
PLAN_FORMAT = "josh-room.save-agent-plan.v2"
ATTEMPT_FORMAT = "josh-room.save-agent-attempt.v1"
MAX_PLAN = 16 * 1024
MAX_LEDGER = 512 * 1024
_FAILURES = (OSError, ValueError, RuntimeError, KeyError, TypeError, BotoCoreError, ClientError,
             subprocess.SubprocessError, KeyboardInterrupt, CLICancelled)


@dataclasses.dataclass(frozen=True, slots=True)
class TargetScope:
    context: object
    workspace: Path
    capture_policy_sha256: str
    workspace_signature: str
    reviewed_context: object | None = None


class ProducerError(native.NativeAgentError):
    def __init__(self, code, *, receipt=None):
        super().__init__(code, committed=receipt is not None)
        self.receipt = receipt
        self.result = receipt


def _fail(code):
    raise ProducerError(code)


def _digest(value):
    return hashlib.sha256(references._canonical_json(value)).hexdigest()


def _selection(value):
    if (not isinstance(value, dict) or set(value) != {"format", "entries"}
            or value["format"] != SELECTION_FORMAT or not isinstance(value["entries"], list)
            or not 1 <= len(value["entries"]) <= 2):
        _fail("native-save-selection-invalid")
    tools = set()
    for item in value["entries"]:
        if (not isinstance(item, dict) or set(item) != {"tool", "profile_ref"}
                or not isinstance(item["tool"], str) or item["tool"] not in {"codex", "claude"} or item["tool"] in tools
                or not references._is_digest(item["profile_ref"])):
            _fail("native-save-selection-invalid")
        tools.add(item["tool"])
    return sorted(value["entries"], key=lambda item: item["tool"])


def _scope_facts(scope):
    from .composition import require_uncomposed_workspace
    from .room_store_operations import scan_workspace_for_status

    context = scope.context
    if (not sys.platform.startswith("linux") or getattr(context.dimension, "provider", None) not in {"minio", "s3"}
            or not isinstance(context.project_id, str) or not native._LABEL.fullmatch(context.project_id)
            or not isinstance(context.physical_binding, str) or not context.physical_binding
            or not references._is_digest(scope.capture_policy_sha256)
            or not references._is_digest(scope.workspace_signature)):
        _fail("native-save-scope-unavailable")
    workspace = native._canonical(Path(scope.workspace))
    require_uncomposed_workspace(workspace)
    scan = scan_workspace_for_status(workspace)
    if scan.capture_policy_sha256 != scope.capture_policy_sha256 or scan.signature != scope.workspace_signature:
        _fail("native-save-workspace-changed")
    content = fingerprint(workspace, expected_policy_sha256=scope.capture_policy_sha256)
    keyset = context.material.keyset
    if (type(keyset.key_generation) is not int or keyset.key_generation < 1
            or not references._is_digest(context.repository_info.repository_id)
            or keyset.binding != context.physical_binding):
        _fail("native-save-keyset-unavailable")
    private = {"dimension_id": context.dimension.dimension_id, "room_id": context.project_id,
               "workspace": str(workspace), "physical_binding": context.physical_binding,
               "encryption_domain_id": context.material.encryption_domain_id,
               "key_generation": keyset.key_generation, "recipients": references._recipients(context),
               "repository_id": context.repository_info.repository_id}
    return {"target_scope_sha256": _digest(private), "workspace_content_sha256": content,
            "capture_policy_sha256": scope.capture_policy_sha256, "workspace_signature": scope.workspace_signature}


def _resolve(scope, item):
    context = scope.context
    grant = discovery.resolve_profile(dimension=context.dimension.dimension_id, room=context.project_id,
        workspace=scope.workspace, profile_ref=item["profile_ref"], tool=item["tool"])
    actual, binding, admission = references._grant(context, grant["profile"], item["tool"], str(scope.workspace), "room-capture")
    if (actual != grant or grant["scope"] not in {"personal", "oss", "homelab"}
            or grant["encryption_domain_id"] != context.material.encryption_domain_id
            or grant["key_generation"] != context.material.keyset.key_generation):
        _fail("native-save-admission-changed")
    root = native._load(Path(grant["profile_root"]), binding)
    reference_root = native._canonical(Path(grant["local_reference_root"]))
    verify_private_path(reference_root, directory=True)
    if (reference_root.is_relative_to(scope.workspace) or reference_root.is_relative_to(root)
            or reference_root.is_relative_to(scope.context.private_dir)):
        _fail("native-save-attempt-root-unsafe")
    if native._json(native._read(root / native.POLICY, 4096))["recipients"] != references._recipients(context):
        _fail("native-save-recipient-mismatch")
    return {"item": dict(item), "grant": copy.deepcopy(grant), "binding": binding, "admission": admission,
            "root": root, "reference_root": reference_root}


@contextlib.contextmanager
def _capture(scope, selection):
    items = _selection(selection)
    initial = _scope_facts(scope)
    members = [_resolve(scope, item) for item in items]
    with contextlib.ExitStack() as stack:
        for root in sorted({member["root"] for member in members}):
            stack.enter_context(native._lock(root))
        paths = []
        for member in members:
            tool = member["item"]["tool"]
            member["inventory"] = native._inventory(member["root"], tool, str(scope.workspace))
            paths.extend((member["root"] / tool / relative, info) for relative, info in sorted(member["inventory"].items()))
        stack.enter_context(sigterm_cancellation())
        try:
            barrier = stack.enter_context(native.read_barrier(paths))
            for member in members:
                tool = member["item"]["tool"]
                entries, _ = native._entries(member["root"], member["binding"], barrier, member["inventory"], tool)
                decoded = {entry["path"]: base64.b64decode(entry["data"], validate=True) for entry in entries}
                references._sessions(decoded, tool)
                member["entries"] = entries
                member["content_sha256"] = references._content_digest(decoded, tool)

            def check():
                barrier.check()
                if _scope_facts(scope) != initial:
                    _fail("native-save-workspace-changed")
                for member in members:
                    current = _resolve(scope, member["item"])
                    if any(current[key] != member[key] for key in ("grant", "binding", "admission", "root", "reference_root")):
                        _fail("native-save-admission-changed")
                    if native._inventory(member["root"], member["item"]["tool"], str(scope.workspace)) != member["inventory"]:
                        _fail("native-save-content-changed")
                barrier.check()

            check()
            yield initial, members, check
        except native.QuiescenceError as error:
            _fail(str(error))


def _reviewed(scope):
    if scope.reviewed_context is None:
        return None, None, None
    context = scope.reviewed_context
    target = references._target(context)
    if (target["room_id"] != scope.context.project_id or target["dimension_id"] != scope.context.dimension.dimension_id
            or target["physical_binding"] != scope.context.physical_binding
            or target["encryption_domain_id"] != scope.context.material.encryption_domain_id
            or target["key_generation"] != scope.context.material.keyset.key_generation):
        _fail("native-save-reviewed-point-mismatch")
    references._same_history(context)
    document, etag = references._load(context)
    public = {"logical_jat_id": target["logical_jat_id"], "binding_sha256": _digest(target),
              "sidecar_sha256": _digest([document, etag])}
    return public, document, etag


def _plan(scope, facts, members, reserved=None):
    reviewed, document, _ = _reviewed(scope)
    existing_eligible = reviewed is not None
    if existing_eligible:
        target = references._target(scope.reviewed_context)
        existing_eligible = target["capture_policy_sha256"] == facts["capture_policy_sha256"]
        if document is not None and document["format"] == references.FORMAT:
            existing_eligible &= document["workspace_content_sha256"] == facts["workspace_content_sha256"]
        else:
            existing_eligible &= target["workspace_signature"] == facts["workspace_signature"]
        existing_eligible &= document is None or not document["captures_closed"]
    entries = []
    for member in members:
        item = member["item"]
        previous = None if reserved is None else next(entry for entry in reserved["entries"] if entry["tool"] == item["tool"])
        outcome = None
        if existing_eligible:
            matching = [] if document is None else [entry for entry in document["entries"]
                if (entry["profile"], entry["tool"]) == (member["grant"]["profile"], item["tool"])]
            existing_id = secrets.token_hex(32) if previous is None else (previous["existing_point"] or {}).get("capture_id")
            kind = "created"
            if matching:
                latest = matching[-1]
                guard = references._authority_guard(scope.reviewed_context, latest, "room-verify")
                guard()
                decoded, _ = references._decode_entry(scope.reviewed_context, latest)
                guard()
                if references._content_digest(decoded, item["tool"]) == member["content_sha256"]:
                    existing_id, kind = references._capture_id(latest), "reused"
            if not references._is_digest(existing_id):
                _fail("native-save-plan-stale")
            outcome = {"capture_id": existing_id, "outcome": kind}
        entries.append({**item, "content_sha256": member["content_sha256"],
                        "authority_sha256": member["admission"]["authority_sha256"],
                        "new_point_capture_id": secrets.token_hex(32) if previous is None else previous["new_point_capture_id"],
                        "existing_point": outcome})
    result = {"format": PLAN_FORMAT, "attempt_id": secrets.token_hex(32) if reserved is None else reserved["attempt_id"],
              "workspace_candidate_id": uuid.uuid4().hex if reserved is None else reserved["workspace_candidate_id"],
              **facts, "reviewed_point": reviewed, "entries": entries}
    result["digest"] = _digest(result)
    if len(references._canonical_json(result)) > MAX_PLAN:
        _fail("native-save-plan-limit")
    return result


def preview_batch(scope, selection):
    with _capture(scope, selection) as (facts, members, check):
        result = _plan(scope, facts, members)
        check()
        return result


def _validate_plan(plan, selection):
    fields = {"format", "attempt_id", "workspace_candidate_id", "target_scope_sha256", "workspace_content_sha256", "capture_policy_sha256",
              "workspace_signature", "reviewed_point", "entries", "digest"}
    try:
        if (not isinstance(plan, dict) or set(plan) != fields or plan["format"] != PLAN_FORMAT
                or len(references._canonical_json(plan)) > MAX_PLAN
                or any(not references._is_digest(plan[name]) for name in fields - {"format", "reviewed_point", "entries", "workspace_candidate_id"})
                or not isinstance(plan["workspace_candidate_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", plan["workspace_candidate_id"])
                or plan["digest"] != _digest({key: value for key, value in plan.items() if key != "digest"})
                or not isinstance(plan["entries"], list) or len(plan["entries"]) != len(_selection(selection))):
            _fail("native-save-plan-invalid")
        if plan["reviewed_point"] is not None:
            point = plan["reviewed_point"]
            if (not isinstance(point, dict) or set(point) != {"logical_jat_id", "binding_sha256", "sidecar_sha256"}
                    or not isinstance(point["logical_jat_id"], str) or not native._LABEL.fullmatch(point["logical_jat_id"])
                    or not all(references._is_digest(point[key]) for key in ("binding_sha256", "sidecar_sha256"))):
                _fail("native-save-plan-invalid")
        for entry, item in zip(plan["entries"], _selection(selection), strict=True):
            if (set(entry) != {"tool", "profile_ref", "content_sha256", "authority_sha256", "new_point_capture_id", "existing_point"}
                    or any(entry[key] != item[key] for key in item)
                    or not all(references._is_digest(entry[key]) for key in ("content_sha256", "authority_sha256", "new_point_capture_id"))):
                _fail("native-save-plan-invalid")
            outcome = entry["existing_point"]
            if outcome is not None and (not isinstance(outcome, dict) or set(outcome) != {"capture_id", "outcome"}
                    or not references._is_digest(outcome["capture_id"]) or outcome["outcome"] not in {"created", "reused"}
                    or plan["reviewed_point"] is None):
                _fail("native-save-plan-invalid")
    except ProducerError:
        raise
    except (ValueError, KeyError, TypeError, OverflowError):
        _fail("native-save-plan-invalid")


def _attempt_root(members, plan):
    root = members[0]["reference_root"] / ("save-attempt-" + plan["attempt_id"])
    return root


def _load_ledger(scope, root, plan):
    if not root.exists():
        return None
    verify_private_path(root, directory=True)
    path = root / "attempt.age"
    if not path.exists():
        _fail("native-save-attempt-incomplete")
    data = native._decrypt_to_memory(path, [scope.context.material.identity], MAX_LEDGER)
    ledger = native._json(data)
    if (not isinstance(ledger, dict) or ledger.get("format") != ATTEMPT_FORMAT
            or ledger.get("plan") != plan or set(ledger) != {"format", "plan", "phase", "payloads", "candidate", "logical_jat_id",
                "proposal", "pending_receipt", "receipt", "created_at", "uploaded", "upload_uncertain"}
            or ledger["phase"] not in {"preparing", "prepared", "workspace-candidate", "workspace-committed", "workspace-uncertain",
                "workspace-rejected", "workspace-cancelled", "uploading", "publishing", "agent-published", "complete"}
            or not isinstance(ledger["payloads"], dict) or set(ledger["payloads"]) - {entry["tool"] for entry in plan["entries"]}):
        _fail("native-save-attempt-invalid")
    tools = {entry["tool"] for entry in plan["entries"]}
    if (not isinstance(ledger["uploaded"], dict) or set(ledger["uploaded"]) - tools
            or any(type(value) is not int or not 0 <= value <= native.MAX_BYTES * 2 for value in ledger["uploaded"].values())
            or not isinstance(ledger["upload_uncertain"], list) or any(tool not in tools for tool in ledger["upload_uncertain"])
            or len(set(ledger["upload_uncertain"])) != len(ledger["upload_uncertain"])
            or ledger["pending_receipt"] is not None and (not isinstance(ledger["pending_receipt"], str)
                or not re.fullmatch(r"[0-9a-f]{32}\.json", ledger["pending_receipt"]))):
        _fail("native-save-attempt-invalid")
    return ledger


def _write_ledger(scope, root, ledger):
    plain = references._canonical_json(ledger)
    if len(plain) > MAX_LEDGER - 2048:
        _fail("native-save-attempt-limit")
    cipher = references._encrypt(plain, references._recipients(scope.context))
    temporary = root / ("attempt-" + uuid.uuid4().hex + ".age")
    native._write(temporary, cipher)
    try:
        target = root / "attempt.age"
        if target.exists():
            native._file(target, MAX_LEDGER)
        os.replace(temporary, target)
        native._fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _facts_match(plan, facts, members):
    if any(plan[name] != value for name, value in facts.items()):
        _fail("native-save-plan-stale")
    for approved, member in zip(plan["entries"], members, strict=True):
        if (any(approved[key] != member["item"][key] for key in member["item"])
                or approved["content_sha256"] != member["content_sha256"]
                or approved["authority_sha256"] != member["admission"]["authority_sha256"]):
            _fail("native-save-plan-stale")


def _candidate(scope, descriptor, object_ref, workspace_signature):
    from .local_store import ObjectRef
    from .logical_jat import LogicalJat

    if not isinstance(descriptor, LogicalJat) or not isinstance(object_ref, ObjectRef):
        _fail("native-save-candidate-invalid")
    body = descriptor.to_dict()
    context = scope.context
    if (body["room_id"] != context.project_id or body["dimension_id"] != context.dimension.dimension_id
            or body["encryption_domain_id"] != context.material.encryption_domain_id
            or body["capture_policy_sha256"] != scope.capture_policy_sha256
            or body["workspace"]["repository_id"] != context.repository_info.repository_id
            or workspace_signature != scope.workspace_signature):
        _fail("native-save-candidate-mismatch")
    reference = dataclasses.asdict(object_ref)
    references._validate_object(reference)
    return {"logical_jat_id": body["logical_jat_id"], "object": reference,
            "descriptor_sha256": _digest(body), "workspace_signature": workspace_signature}


class PreparedBatch:
    def __init__(self, scope, plan, members, check, root, ledger):
        self.scope, self.plan, self.members = scope, copy.deepcopy(plan), members
        self._native_check, self.root, self.ledger = check, root, ledger
        self._migration_guards = []

    def check(self):
        self._native_check()
        for guard in self._migration_guards:
            guard()

    def _persist(self):
        _write_ledger(self.scope, self.root, self.ledger)

    def before_workspace_publication(self, descriptor, object_ref, workspace_signature):
        self.check()
        candidate = _candidate(self.scope, descriptor, object_ref, workspace_signature)
        if candidate["logical_jat_id"] != self.plan["workspace_candidate_id"]:
            _fail("native-save-candidate-not-reserved")
        if self.ledger["candidate"] is not None and self.ledger["candidate"] != candidate:
            _fail("native-save-candidate-changed")
        if self.ledger["phase"] not in {"prepared", "workspace-candidate"}:
            _fail("native-save-workspace-retry-required")
        self.ledger.update(candidate=candidate, logical_jat_id=candidate["logical_jat_id"], phase="workspace-candidate")
        self._persist()

    def verify_workspace_result(self, exact_context):
        """Freshly corroborate this attempt's result without waiving marker checks at publish."""
        return self._exact(exact_context, require_marker=False)

    def workspace_outcome(self, phase, *, logical_jat_id=None):
        if phase not in {"committed", "uncertain", "rejected", "cancelled"}:
            _fail("native-save-workspace-phase-invalid")
        candidate = self.ledger["candidate"]
        approved = self.plan["reviewed_point"]
        expected = candidate["logical_jat_id"] if candidate is not None else (approved or {}).get("logical_jat_id")
        if logical_jat_id is not None and logical_jat_id != expected:
            _fail("native-save-result-point-mismatch")
        if phase == "committed" and (logical_jat_id is None or expected is None):
            _fail("native-save-exact-result-required")
        if self.ledger["phase"] == "complete":
            if phase != "committed" or logical_jat_id != self.ledger["logical_jat_id"]:
                _fail("native-save-workspace-phase-invalid")
            return
        selected = candidate["logical_jat_id"] if candidate is not None else logical_jat_id
        self.ledger.update(phase="workspace-" + phase, logical_jat_id=selected)
        self._persist()

    def _exact(self, context, *, require_marker, check_native=True):
        if check_native:
            self.check()
        context = references._fresh(context, self.scope.context.project_id, self.ledger["logical_jat_id"])
        target = references._target(context)
        candidate = self.ledger["candidate"]
        reviewed = self.plan["reviewed_point"]
        if candidate is not None:
            if (target["logical_jat_id"] != candidate["logical_jat_id"]
                    or target["descriptor_sha256"] != candidate["object"]["sha256"]
                    or target["descriptor_size"] != candidate["object"]["size"]
                    or target["workspace_signature"] != candidate["workspace_signature"]
                    or _digest(context.selected_descriptor.to_dict()) != candidate["descriptor_sha256"]):
                _fail("native-save-result-point-mismatch")
        elif reviewed is None or _digest(target) != reviewed["binding_sha256"]:
            _fail("native-save-result-point-mismatch")
        if (target["room_id"] != self.scope.context.project_id
                or target["dimension_id"] != self.scope.context.dimension.dimension_id
                or target["physical_binding"] != self.scope.context.physical_binding
                or target["encryption_domain_id"] != self.scope.context.material.encryption_domain_id
                or target["key_generation"] != self.scope.context.material.keyset.key_generation
                or target["capture_policy_sha256"] != self.scope.capture_policy_sha256
                or target["repository_id"] != self.scope.context.repository_info.repository_id
                or references._recipients(context) != references._recipients(self.scope.context)):
            _fail("native-save-result-scope-mismatch")
        if require_marker:
            references._workspace(context, str(self.scope.workspace), captured_baseline=False)
        return context

    def _outcomes(self, context):
        target = references._target(context)
        reviewed = self.plan["reviewed_point"]
        existing = reviewed is not None and _digest(target) == reviewed["binding_sha256"]
        outcomes = []
        for approved in self.plan["entries"]:
            outcome = approved["existing_point"] if existing else {
                "capture_id": approved["new_point_capture_id"], "outcome": "created"}
            if outcome is None:
                _fail("native-save-result-not-reviewed")
            outcomes.append(outcome)
        return outcomes

    def receipt(self, state, *, error=None, context=None):
        try:
            outcomes = self._outcomes(context) if context is not None else [None] * len(self.members)
        except native.NativeAgentError:
            outcomes = [None] * len(self.members)
        complete = state == "complete"
        uncertain = state in {"agent-uncertain", "workspace-uncertain"}
        entries = []
        for member, approved, outcome in zip(self.members, self.plan["entries"], outcomes, strict=True):
            tool = member["item"]["tool"]
            kind = outcome["outcome"] if complete and outcome is not None else ("uncertain" if uncertain else "prepared-unpublished")
            entries.append({**member["item"], "capture_id": None if outcome is None else outcome["capture_id"],
                "content_sha256": approved["content_sha256"], "state": kind,
                "data_uploaded": None if tool in self.ledger["upload_uncertain"] else self.ledger["uploaded"].get(tool, 0),
                "upload_uncertain": tool in self.ledger["upload_uncertain"]})
        result = {"ok": complete, "state": state, "attempt_id": self.plan["attempt_id"],
                  "logical_jat_id": self.ledger["logical_jat_id"], "history_only": True,
                  "native_resume_verified": False, "original_t3_identity_restored": False,
                  "workspace_committed": True if context is not None else None,
                  "reconciliation_required": uncertain, "entries": entries}
        if error is not None:
            result["error"] = error
        return result


def _payloads(attempt):
    for member in attempt.members:
        tool = member["item"]["tool"]
        path = attempt.root / (tool + ".age")
        document = {"format": native._native_format(tool), "binding": member["binding"],
                    "recipients": references._recipients(attempt.scope.context), "provider": tool,
                    "mode": "history-only", "entries": member["entries"]}
        if tool not in attempt.ledger["payloads"]:
            if path.exists():
                # A preparing crash can leave only an encrypted payload. It is
                # reusable only after the full native decode/integrity check.
                cipher = native._read(path, native.MAX_BYTES * 2)
            else:
                cipher = references._encrypt(references._canonical_json(document), references._recipients(attempt.scope.context))
                attempt.check()
                native._write(path, cipher)
                native._fsync_directory(attempt.root)
            digest = hashlib.sha256(cipher).hexdigest()
            attempt.ledger["payloads"][tool] = {"key": "objects/sha256/" + digest, "sha256": digest, "size": len(cipher)}
        reference = attempt.ledger["payloads"][tool]
        references._validate_object(reference)
        cipher = native._read(path, native.MAX_BYTES * 2)
        if hashlib.sha256(cipher).hexdigest() != reference["sha256"] or len(cipher) != reference["size"]:
            _fail("native-save-payload-changed")
        decoded, recipients = native._decode(path, [attempt.scope.context.material.identity], member["binding"], tool)
        if (recipients != references._recipients(attempt.scope.context)
                or references._content_digest(decoded, tool) != member["content_sha256"]
                or native._json(native._decrypt_to_memory(path, [attempt.scope.context.material.identity]))["format"] != native._native_format(tool)):
            _fail("native-save-payload-changed")
        attempt.check()
        attempt._persist()


@contextlib.contextmanager
def prepare_batch(scope, selection, plan):
    _validate_plan(plan, selection)
    with _capture(scope, selection) as (facts, members, check):
        _facts_match(plan, facts, members)
        root = _attempt_root(members, plan)
        ledger = _load_ledger(scope, root, plan)
        if ledger is None:
            if _plan(scope, facts, members, reserved=plan) != plan:
                _fail("native-save-plan-stale")
            root.mkdir(mode=0o700)
            native._fsync_directory(root.parent)
            ledger = {"format": ATTEMPT_FORMAT, "plan": copy.deepcopy(plan), "phase": "preparing",
                "payloads": {}, "candidate": None, "logical_jat_id": None, "proposal": None,
                "pending_receipt": None, "receipt": None, "created_at": datetime.now(UTC).isoformat(),
                "uploaded": {}, "upload_uncertain": []}
            _write_ledger(scope, root, ledger)
        elif ledger["proposal"] is None and ledger["phase"] != "complete":
            if ledger["candidate"] is not None:
                # The callback's real B, not the old reviewed A, is the only
                # retry target once B's catalog commitment is corroborated.
                recorded = PreparedBatch(scope, plan, members, check, root, ledger)
                recorded._exact(scope.context, require_marker=False)
            elif _plan(scope, facts, members, reserved=plan) != plan:
                _fail("native-save-plan-stale")
        attempt = PreparedBatch(scope, plan, members, check, root, ledger)
        _payloads(attempt)
        if ledger["phase"] == "preparing":
            ledger["phase"] = "prepared"
            attempt._persist()
        attempt.check()
        try:
            yield attempt
        except (KeyboardInterrupt, CLICancelled):
            # Do not replace a more informative pre-CAS/publishing phase. The
            # last fsynced phase is the conservative crash recovery boundary.
            attempt._persist()
            raise


def _base_document(attempt, context):
    document, etag = references._load(context)
    reviewed = attempt.plan["reviewed_point"]
    if reviewed is not None and _digest(references._target(context)) == reviewed["binding_sha256"]:
        if _digest([document, etag]) != reviewed["sidecar_sha256"]:
            _fail("native-save-sidecar-changed")
    elif document is not None or etag is not None:
        _fail("native-save-sidecar-changed")
    if document is not None and document["captures_closed"]:
        _fail("native-save-captures-closed")
    if references._workspace_content(context, str(attempt.scope.workspace), document) != attempt.plan["workspace_content_sha256"]:
        _fail("native-save-workspace-changed")
    return document, etag


def _proposal(attempt, context, document, outcomes):
    entries = [] if document is None else copy.deepcopy(document["entries"])
    if document is not None and document["format"] == references.FORMAT_V1:
        for entry in entries:
            guard = references._authority_guard(context, entry, "room-verify")
            guard()
            decoded, _ = references._decode_entry(context, entry)
            guard()
            attempt._migration_guards.append(guard)
            entry.update(capture_id=references._capture_id(entry), content_sha256=references._content_digest(decoded, entry["tool"]))
    for member, outcome in zip(attempt.members, outcomes, strict=True):
        if outcome["outcome"] == "reused":
            continue
        tool = member["item"]["tool"]
        entries.append({"profile": member["grant"]["profile"], "tool": tool, "scope": member["grant"]["scope"],
            "created_at": attempt.ledger["created_at"], "archive_format": native._native_format(tool),
            "object": attempt.ledger["payloads"][tool], "archive_binding": member["binding"],
            "origin": references._target(context), "capture_id": outcome["capture_id"],
            "content_sha256": member["content_sha256"]})
    result = {"format": references.FORMAT, "binding": references._target(context),
              "revision": 1 if document is None else document["revision"] + 1, "captures_closed": False,
              "workspace_content_sha256": attempt.plan["workspace_content_sha256"], "entries": entries}
    if len(entries) > 64 or len(references._canonical_json(result)) > references.MAX_CONTROL - 2048:
        _fail("native-reference-limit")
    references._validate(result, references._target(context))
    return result


def _verify_outcomes(attempt, context, document, outcomes, *, expected=None):
    if document is None:
        return False
    for member, outcome in zip(attempt.members, outcomes, strict=True):
        matching = [entry for entry in document["entries"] if references._capture_id(entry) == outcome["capture_id"]]
        if not matching:
            return False
        if len(matching) != 1:
            _fail("native-save-capture-id-conflict")
        entry = matching[0]
        if ((entry["profile"], entry["tool"], entry["scope"]) !=
                (member["grant"]["profile"], member["item"]["tool"], member["grant"]["scope"])):
            _fail("native-save-capture-id-conflict")
        if expected is not None:
            proposed = [item for item in expected["entries"] if references._capture_id(item) == outcome["capture_id"]]
            if len(proposed) != 1 or entry != proposed[0]:
                _fail("native-save-capture-id-conflict")
        guard = references._authority_guard(context, entry, "room-verify")
        guard()
        decoded, _ = references._decode_entry(context, entry)
        guard()
        if references._content_digest(decoded, entry["tool"]) != member["content_sha256"]:
            _fail("native-save-content-mismatch")
    return True


def _finish(attempt, context, outcomes, *, fence):
    def guard():
        attempt._exact(context, require_marker=True)

    if fence:
        references._fence_publication(context, guard)
    guard()
    references._same_history(context)
    document, _ = references._load(context)
    expected = attempt.ledger["proposal"]
    if not _verify_outcomes(attempt, context, document, outcomes, expected=None if expected is None else expected["document"]):
        _fail("native-save-published-history-unavailable")
    objects = _created_objects(attempt, outcomes)
    pending = attempt.ledger["pending_receipt"]
    if pending is not None:
        if pending != _pending_name(attempt, context, objects):
            _fail("native-save-pending-receipt-mismatch")
        _cleanup_pending_receipt(context, pending, objects)
    result = attempt.receipt("complete", context=context)
    attempt.ledger.update(phase="complete", receipt=result)
    attempt._persist()
    return result


def _created_objects(attempt, outcomes):
    return [attempt.ledger["payloads"][member["item"]["tool"]]
            for member, outcome in zip(attempt.members, outcomes, strict=True) if outcome["outcome"] == "created"]


def _pending_name(attempt, context, objects):
    return _digest(["josh-room.save-agent-pending.v1", attempt.plan["attempt_id"],
                    references._target(context), objects])[:32] + ".json"


def _verify_pending_receipt(context, path, objects):
    verify_private_path(path.parent, directory=True)
    before = native._file(path, references.MAX_CONTROL)
    document = native._json(native._read(path, references.MAX_CONTROL))
    expected = {"format": "josh-room.agent-reference-pending.v1", "id": path.name[:-5],
                "scope_sha256": references._scope_digest(context), "operation": "capture", "objects": objects}
    if document != expected:
        _fail("native-save-pending-receipt-mismatch")
    after = native._file(path, references.MAX_CONTROL)
    if any(getattr(before, field) != getattr(after, field)
           for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        _fail("native-save-pending-claim-changed")
    return after


def _cleanup_pending_receipt(context, pending, objects):
    directory = context.instance / "agent-reference-receipts"
    verify_private_path(directory, directory=True)
    original = directory / pending
    custody = directory / ("cleanup-" + pending[:-5])
    if not custody.exists():
        if not original.exists() and not original.is_symlink():
            return
        custody.mkdir(mode=0o700)
        native._fsync_directory(directory)
    verify_private_path(custody, directory=True)
    claimed = custody / pending
    if not claimed.exists() and original.exists():
        before = _verify_pending_receipt(context, original, objects)
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            _fail("native-save-pending-claim-unavailable")
        if rename(-100, os.fsencode(original), -100, os.fsencode(claimed), 1):
            _fail("native-save-pending-claim-uncertain")
        native._fsync_directory(custody)
        native._fsync_directory(directory)
        after = _verify_pending_receipt(context, claimed, objects)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            _fail("native-save-pending-claim-changed")
    if claimed.exists():
        # Only this entered attempt owns the private claim. Never unlink the
        # original pathname after validation: another publisher may reuse it.
        _verify_pending_receipt(context, claimed, objects)
        claimed.unlink()
        native._fsync_directory(custody)
    custody.rmdir()
    native._fsync_directory(directory)
    if original.exists() or original.is_symlink():
        _fail("native-save-pending-entry-replaced")


def _pending_for_attempt(attempt, context, objects):
    previous = attempt.ledger["pending_receipt"]
    if previous != _pending_name(attempt, context, objects):
        _fail("native-save-pending-receipt-mismatch")
    path = context.instance / "agent-reference-receipts" / previous
    if path.exists():
        _verify_pending_receipt(context, path, objects)
        return path
    # The deterministic name was already fsynced in the encrypted journal.
    # Exclusive creation never overwrites unrelated or mismatched tracking.
    return references._pending(context, objects, "capture", receipt_id=previous[:-5])


def publish_batch(exact_context, attempt):
    context = None
    try:
        if attempt.ledger["phase"] not in {"workspace-committed", "uploading", "publishing", "agent-published", "complete"}:
            _fail("native-save-workspace-not-committed")
        context = attempt._exact(exact_context, require_marker=True)
        outcomes = attempt._outcomes(context)
        document, etag = references._load(context)
        proposal = attempt.ledger["proposal"]
        if proposal is not None and _verify_outcomes(attempt, context, document, outcomes, expected=proposal["document"]):
            return _finish(attempt, context, outcomes, fence=attempt.ledger["phase"] != "complete"
                           and any(outcome["outcome"] == "created" for outcome in outcomes))
        if attempt.ledger["phase"] == "complete":
            _fail("native-save-published-history-unavailable")
        document, etag = _base_document(attempt, context)
        if all(outcome["outcome"] == "reused" for outcome in outcomes):
            if not _verify_outcomes(attempt, context, document, outcomes):
                _fail("native-save-reviewed-capture-unavailable")
            attempt.check()
            if references._load(context) != (document, etag):
                _fail("native-save-sidecar-changed")
            attempt.ledger["proposal"] = {"document": document, "base_sha256": _digest([document, etag])}
            return _finish(attempt, context, outcomes, fence=False)
        updated = _proposal(attempt, context, document, outcomes)
        if proposal is not None and proposal != {"document": updated, "base_sha256": _digest([document, etag])}:
            _fail("native-save-sidecar-changed")
        attempt.ledger["proposal"] = {"document": updated, "base_sha256": _digest([document, etag])}
        objects = _created_objects(attempt, outcomes)
        reserved_receipt = _pending_name(attempt, context, objects)
        if attempt.ledger["pending_receipt"] not in {None, reserved_receipt}:
            _fail("native-save-pending-receipt-mismatch")
        attempt.ledger.update(phase="uploading", pending_receipt=reserved_receipt)
        attempt._persist()
        _pending_for_attempt(attempt, context, objects)
        for member, outcome in zip(attempt.members, outcomes, strict=True):
            if outcome["outcome"] == "reused":
                continue
            tool = member["item"]["tool"]
            reference = attempt.ledger["payloads"][tool]
            attempt.check()
            if tool not in attempt.ledger["upload_uncertain"]:
                attempt.ledger["upload_uncertain"].append(tool)
            attempt._persist()
            cipher = native._read(attempt.root / (tool + ".age"), native.MAX_BYTES * 2)
            if references._put_blob(context, cipher) != reference:
                _fail("native-save-payload-changed")
            attempt.ledger["uploaded"][tool] = reference["size"]
            attempt.ledger["upload_uncertain"].remove(tool)
            attempt._persist()
        attempt.ledger["phase"] = "publishing"
        attempt._persist()

        def guard():
            attempt.check()
            attempt._exact(context, require_marker=True)
            if references._load(context) != (document, etag):
                _fail("native-save-sidecar-changed")

        references._publish(context, updated, etag, before_commit=guard)
        attempt.ledger["phase"] = "agent-published"
        attempt._persist()
        return _finish(attempt, context, outcomes, fence=True)
    except _FAILURES as error:
        uncertain = attempt.ledger["phase"] in {"uploading", "publishing", "agent-published", "complete"}
        code = str(error) if isinstance(error, native.NativeAgentError) else "native-save-operation-failed"
        return attempt.receipt("agent-uncertain" if uncertain else "blocked", error=code, context=context)


def _reconcile_active(scope, selection, plan):
    _validate_plan(plan, selection)
    with _capture(scope, selection) as (facts, members, check):
        _facts_match(plan, facts, members)
        root = _attempt_root(members, plan)
        ledger = _load_ledger(scope, root, plan)
        if ledger is None:
            return {"ok": False, "state": "absent", "attempt_id": plan["attempt_id"],
                    "logical_jat_id": None, "workspace_committed": False, "reconciliation_required": False}
        attempt = PreparedBatch(scope, plan, members, check, root, ledger)
        if ledger["logical_jat_id"] is None:
            return attempt.receipt("prepared" if ledger["phase"] in {"preparing", "prepared"} else "workspace-uncertain")
        try:
            context = attempt._exact(scope.context, require_marker=False)
        except KeyError:
            return attempt.receipt("workspace-uncertain" if ledger["phase"] not in {"workspace-rejected", "workspace-cancelled"} else "blocked")
        except _FAILURES:
            return attempt.receipt("workspace-uncertain")
        outcomes = attempt._outcomes(context)
        document, _ = references._load(context)
        proposal = ledger["proposal"]
        if proposal is not None and _verify_outcomes(attempt, context, document, outcomes, expected=proposal["document"]):
            try:
                attempt._exact(context, require_marker=True)
                return _finish(attempt, context, outcomes, fence=ledger["phase"] != "complete"
                               and any(outcome["outcome"] == "created" for outcome in outcomes))
            except _FAILURES:
                return attempt.receipt("agent-uncertain", context=context)
        if ledger["phase"] == "complete":
            return attempt.receipt("blocked", error="native-save-published-history-unavailable", context=context)
        return attempt.receipt("workspace-committed", context=context)


def _metadata_attempt(scope, selection, plan):
    item = _selection(selection)[0]
    grant = discovery.resolve_profile(dimension=scope.context.dimension.dimension_id, room=scope.context.project_id,
        workspace=scope.workspace, profile_ref=item["profile_ref"], tool=item["tool"])
    if grant["scope"] not in {"personal", "oss", "homelab"}:
        _fail("native-save-admission-changed")
    root = native._canonical(Path(grant["local_reference_root"]))
    verify_private_path(root, directory=True)
    if (root.is_relative_to(scope.workspace) or root.is_relative_to(Path(grant["profile_root"]))
            or root.is_relative_to(scope.context.private_dir)):
        _fail("native-save-attempt-root-unsafe")
    root = root / ("save-attempt-" + plan["attempt_id"])
    return root, _load_ledger(scope, root, plan)


def _reserved_workspace_context(scope, plan):
    """Read-only Room-authority proof when native grant/ledger access is gone."""
    facts = _scope_facts(scope)
    if any(plan[name] != value for name, value in facts.items()):
        _fail("native-save-plan-stale")
    context = references._fresh(scope.context, scope.context.project_id, plan["workspace_candidate_id"])
    target = references._target(context)
    expected = {"logical_jat_id": plan["workspace_candidate_id"], "room_id": scope.context.project_id,
                "dimension_id": scope.context.dimension.dimension_id, "physical_binding": scope.context.physical_binding,
                "encryption_domain_id": scope.context.material.encryption_domain_id,
                "key_generation": scope.context.material.keyset.key_generation,
                "repository_id": scope.context.repository_info.repository_id,
                "capture_policy_sha256": plan["capture_policy_sha256"], "workspace_signature": plan["workspace_signature"]}
    if any(target[name] != value for name, value in expected.items()):
        _fail("native-save-result-scope-mismatch")
    return context


def reconcile_attempt(scope, selection, plan):
    """Corroborate a durable exact candidate before re-reading native bytes.

    A blocked receipt is never permission to repeat workspace Save. Even when
    fresh native admission/content is unavailable, independently verified
    workspace commitment is retained without exposing native history or paths.
    """
    _validate_plan(plan, selection)
    ledger = None
    context = None
    try:
        root, ledger = _metadata_attempt(scope, selection, plan)
        if ledger is not None and ledger["logical_jat_id"] is not None:
            metadata = PreparedBatch(scope, plan, [], lambda: None, root, ledger)
            try:
                context = metadata._exact(scope.context, require_marker=False, check_native=False)
            except _FAILURES:
                context = None
        elif ledger is None:
            try:
                context = _reserved_workspace_context(scope, plan)
            except _FAILURES:
                context = None
            if context is not None:
                _fail("native-save-attempt-unavailable")
        return _reconcile_active(scope, selection, plan)
    except _FAILURES as error:
        code = str(error) if isinstance(error, native.NativeAgentError) else "native-save-reconciliation-failed"
        if context is None and ledger is None:
            try:
                context = _reserved_workspace_context(scope, plan)
            except _FAILURES:
                context = None
        logical_id = plan["workspace_candidate_id"] if ledger is None else ledger["logical_jat_id"]
        entries = []
        for item in plan["entries"]:
            existing = item["existing_point"]
            capture_id = (item["new_point_capture_id"] if ledger is None or ledger["candidate"] is not None
                          else None if existing is None else existing["capture_id"])
            entries.append({"tool": item["tool"], "profile_ref": item["profile_ref"], "capture_id": capture_id,
                "content_sha256": item["content_sha256"], "state": "prepared-unpublished",
                "data_uploaded": (None if ledger is None or item["tool"] in ledger["upload_uncertain"]
                                  else ledger["uploaded"].get(item["tool"], 0)),
                "upload_uncertain": ledger is None or item["tool"] in ledger["upload_uncertain"]})
        return {"ok": False, "state": "blocked", "attempt_id": plan["attempt_id"], "logical_jat_id": logical_id,
                "workspace_committed": True if context is not None and ledger is not None else None,
                "candidate_point_verified": context is not None, "history_only": True,
                "native_resume_verified": False, "original_t3_identity_restored": False,
                "reconciliation_required": ledger is None or ledger["phase"] not in {"preparing", "prepared", "complete"},
                "error": code, "entries": entries}
