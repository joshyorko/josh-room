"""Encrypted, exact-recovery-point native history references outside logical JATs."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from . import native_agent_admission as authority
from . import native_agent_discovery as discovery
from . import native_agents as native
from .cancellation import CLICancelled, terminate_owned_process
from .crypto import _managed_executable
from .r2 import R2PublicationError

FORMAT = "josh-room.agent-references.v1"
EXPORT_FORMAT = "josh-room.agent-reference-export.v1"
MAX_CONTROL = 64 * 1024
MAX_EXPORT = native.MAX_BYTES * 2
_TARGET_FIELDS = {
    "dimension_id", "physical_binding", "encryption_domain_id", "key_generation", "room_id",
    "logical_jat_id", "descriptor_sha256", "descriptor_size", "repository_id",
    "workspace_snapshot_id", "workspace_tree_id", "capture_policy_sha256", "workspace_signature",
}
_ENTRY_FIELDS = {"profile", "tool", "scope", "created_at", "archive_format", "object", "archive_binding", "origin"}


class NativeReferenceError(native.NativeAgentError):
    def __init__(self, code, *, sidecar_published=None, orphaned=None):
        super().__init__(code, committed=True)
        self.sidecar_published = sidecar_published
        self.orphaned = True if sidecar_published is False and orphaned is None else orphaned


def _fail(code, *, committed=False):
    raise native.NativeAgentError(code, committed=committed)


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _recipients(context):
    return native._recipients([context.material.recipient, *context.material.keyset.recovery_recipients])


def _target(context):
    from .room_store_lifecycle import _selected_logical_record

    record, descriptor = _selected_logical_record(context)
    body = descriptor.to_dict()
    physical = context.physical_binding
    if not isinstance(physical, str) or not physical:
        _fail("native-reference-physical-binding-required")
    return {
        "dimension_id": body["dimension_id"], "physical_binding": physical,
        "encryption_domain_id": body["encryption_domain_id"],
        "key_generation": context.material.keyset.key_generation,
        "room_id": body["room_id"], "logical_jat_id": body["logical_jat_id"],
        "descriptor_sha256": record["ciphertext_sha256"], "descriptor_size": record["ciphertext_size"],
        "repository_id": body["workspace"]["repository_id"],
        "workspace_snapshot_id": body["workspace"]["snapshot_id"], "workspace_tree_id": body["workspace"]["tree_id"],
        "capture_policy_sha256": body["capture_policy_sha256"], "workspace_signature": record["workspace_signature"],
    }


def _key(target):
    stable = {key: value for key, value in target.items() if key != "key_generation"}
    return "control/agent-references/v1/" + hashlib.sha256(_canonical_json(stable)).hexdigest() + ".age"


def _validate_target(value):
    if not isinstance(value, dict) or set(value) != _TARGET_FIELDS:
        _fail("native-reference-format-invalid")
    for name in ("dimension_id", "room_id", "logical_jat_id"):
        if not isinstance(value[name], str) or not authority._LABEL.fullmatch(value[name]):
            _fail("native-reference-format-invalid")
    for name in ("descriptor_sha256", "repository_id", "workspace_snapshot_id", "workspace_tree_id", "capture_policy_sha256", "workspace_signature"):
        if not isinstance(value[name], str) or len(value[name]) != 64 or any(character not in "0123456789abcdef" for character in value[name]):
            _fail("native-reference-format-invalid")
    if (not isinstance(value["physical_binding"], str) or not value["physical_binding"]
            or len(value["physical_binding"]) > 4096
            or type(value["key_generation"]) is not int or value["key_generation"] < 1
            or type(value["descriptor_size"]) is not int or not 1 <= value["descriptor_size"] <= MAX_CONTROL):
        _fail("native-reference-format-invalid")
    from .encryption_domain import validate_encryption_domain_id

    validate_encryption_domain_id(value["encryption_domain_id"])


def _validate_object(value):
    if not isinstance(value, dict) or set(value) != {"key", "sha256", "size"}:
        _fail("native-reference-object-invalid")
    digest = value["sha256"]
    if (not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            or value["key"] != "objects/sha256/" + digest
            or type(value["size"]) is not int or not 1 <= value["size"] <= native.MAX_BYTES * 2):
        _fail("native-reference-object-invalid")


def _validate(document, target):
    try:
        return _validate_document(document, target)
    except native.NativeAgentError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError):
        _fail("native-reference-format-invalid")


def _validate_document(document, target):
    if (not isinstance(document, dict) or set(document) != {"format", "binding", "revision", "captures_closed", "entries"}
            or document["format"] != FORMAT or type(document["revision"]) is not int or document["revision"] < 1
            or type(document["captures_closed"]) is not bool or not isinstance(document["entries"], list)
            or not 1 <= len(document["entries"]) <= 64):
        _fail("native-reference-format-invalid")
    _validate_target(document["binding"])
    if document["binding"] != target:
        _fail("native-reference-historical-binding-mismatch")
    identities = set()
    for entry in document["entries"]:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            _fail("native-reference-format-invalid")
        if (not isinstance(entry["profile"], str) or not authority._LABEL.fullmatch(entry["profile"])
                or entry["tool"] not in {"codex", "claude"} or entry["scope"] not in {"personal", "oss", "homelab"}
                or entry["archive_format"] != native._native_format(entry["tool"])):
            _fail("native-reference-format-invalid")
        stamp = datetime.fromisoformat(entry["created_at"])
        if stamp.utcoffset() is None:
            _fail("native-reference-format-invalid")
        identity = (entry["profile"], entry["tool"])
        if identity in identities:
            _fail("native-reference-identity-ambiguous")
        identities.add(identity)
        _validate_object(entry["object"])
        _validate_target(entry["origin"])
        binding = entry["archive_binding"]
        if not isinstance(binding, dict) or set(binding) != {"dimension", "room", "profile", "workspace"}:
            _fail("native-reference-format-invalid")
        if (binding["dimension"] != entry["origin"]["dimension_id"] or binding["room"] != entry["origin"]["room_id"]
                or binding["profile"] != entry["profile"] or not isinstance(binding["workspace"], str)
                or not Path(binding["workspace"]).is_absolute() or ".." in Path(binding["workspace"]).parts
                or "\0" in binding["workspace"] or len(binding["workspace"]) > 4096):
            _fail("native-reference-format-invalid")
    return document


def _encrypt(data, recipients):
    command = [str(_managed_executable("age"))]
    for recipient in native._recipients(recipients):
        command.extend(["-r", recipient])
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        output, _ = process.communicate(data, timeout=60)
        if process.returncode:
            _fail("native-reference-encryption-failed")
        return output
    finally:
        if process.poll() is None:
            terminate_owned_process(process)


@contextlib.contextmanager
def _cipher_file(context, data, *, directory=None):
    directory = Path(directory) if directory else context.private_dir
    path = directory / ("native-reference-" + uuid.uuid4().hex + ".age")
    native._write(path, data)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def _load(context):
    if getattr(context.dimension, "provider", None) == "r2":
        return None, None
    target = _target(context)
    body, etag = context.backend.read_control(_key(target), MAX_CONTROL)
    if body is None:
        if etag is not None:
            _fail("native-reference-control-invalid")
        return None, None
    if not isinstance(body, bytes) or not isinstance(etag, str) or not etag:
        _fail("native-reference-control-invalid")
    with _cipher_file(context, body) as source:
        data = native._decrypt_to_memory(source, [context.material.identity], MAX_CONTROL)
    return _validate(native._json(data), target), etag


def _publish(context, document, etag, *, before_commit=None):
    _validate(document, _target(context))
    plain = _canonical_json(document)
    if len(plain) > MAX_CONTROL - 2048:
        _fail("native-reference-limit")
    cipher = _encrypt(plain, _recipients(context))
    if len(cipher) > MAX_CONTROL:
        _fail("native-reference-limit")
    if before_commit is not None:
        before_commit()
    try:
        if etag is None:
            context.backend.create_control(_key(document["binding"]), cipher)
        else:
            context.backend.replace_control(_key(document["binding"]), cipher, etag)
    except (R2PublicationError, BotoCoreError, ClientError, OSError, ValueError) as error:
        _fail("native-reference-publication-uncertain" if getattr(error, "published", None) is not False else "native-reference-conflict",
              committed=getattr(error, "published", None) is not False)


def _fresh(context, room_id=None, logical_id=None):
    from .room_store_bridge import _load_descriptor, _read_catalog

    catalog, etag = _read_catalog(context.backend, context.instance, context.dimension, context.material)
    room_id = room_id or context.project_id
    logical_id = logical_id or context.selected_record["snapshot_id"]
    record = catalog.resolve_snapshot(room_id, logical_id)
    descriptor = _load_descriptor(context.backend, context.instance, context.material, room_id, record, context.private_dir)
    return dataclasses.replace(context, catalog=catalog, catalog_etag=etag, project_id=room_id,
                               selected_record=record, selected_descriptor=descriptor)


def _same_history(context):
    if _target(_fresh(context)) != _target(context):
        _fail("native-reference-historical-binding-mismatch")


def _workspace(context, workspace, *, captured_baseline=True):
    from .room_store_operations import scan_workspace_for_status
    from .workspace_state import context_status

    marker = context_status(Path(workspace))
    target = _target(context)
    expected = {"dimension_id": target["dimension_id"], "encryption_domain_id": target["encryption_domain_id"],
                "project_id": target["room_id"], "snapshot_id": target["logical_jat_id"],
                "signature_algorithm": "josh-room-stat-v1",
                "capture_policy_sha256": target["capture_policy_sha256"]}
    if captured_baseline:
        expected["workspace_signature"] = target["workspace_signature"]
    if marker.get("path_matches") is not True or any(marker.get(key) != value for key, value in expected.items()):
        _fail("native-reference-workspace-binding-mismatch")
    scan = scan_workspace_for_status(Path(workspace))
    expected_signature = target["workspace_signature"] if captured_baseline else marker.get("workspace_signature")
    if scan.signature != expected_signature or scan.capture_policy_sha256 != target["capture_policy_sha256"]:
        _fail("native-reference-workspace-changed")


def _grant(context, profile, tool, workspace, operation, *, destination=None):
    matches = [grant for _, grant, tools in discovery._matching_grants(context.dimension.dimension_id, context.project_id, Path(workspace))
               if grant["profile"] == profile and tool in tools]
    if len(matches) != 1:
        _fail("native-admission-not-authorized")
    grant = matches[0]
    binding = {"dimension": context.dimension.dimension_id, "room": context.project_id, "profile": profile, "workspace": workspace}
    receipt = native._admit(binding, _recipients(context), root=Path(grant["profile_root"]), operation=operation,
                            destination=destination, tool=tool)
    expected = authority.authority_digest(authority.FORMAT_V3, tool, grant, context.physical_binding,
                                          context.material.keyset.key_generation)
    if receipt.get("authority_sha256") != expected:
        _fail("native-reference-authority-context-mismatch")
    return grant, binding, receipt


def _authority_guard(context, entry, operation):
    arguments = (context, entry["profile"], entry["tool"], entry["archive_binding"]["workspace"], operation)
    grant, _, receipt = _grant(*arguments)
    if grant["scope"] != entry["scope"]:
        _fail("native-reference-scope-mismatch")

    def check():
        current, _, admitted = _grant(*arguments)
        if current != grant or admitted != receipt:
            _fail("native-admission-changed")

    return check


def _sessions(decoded, tool):
    sessions = []
    identities = set()
    for path, data in sorted(decoded.items()):
        identity = native._session_id(path, data, tool)
        if identity in identities:
            _fail("native-session-identity-ambiguous")
        identities.add(identity)
        sessions.append({"provider_session_id": identity, "tool": tool, "cwd_matches": True})
    return sessions


def _entry(context, document, profile, tool):
    entries = [entry for entry in document["entries"] if (entry["profile"], entry["tool"]) == (profile, tool)]
    if len(entries) != 1:
        _fail("native-reference-unavailable")
    entry = entries[0]
    grant, _, _ = _grant(context, profile, tool, entry["archive_binding"]["workspace"], "room-verify")
    if grant["scope"] != entry["scope"]:
        _fail("native-reference-scope-mismatch")
    return entry


def _blob(context, entry):
    reference = entry["object"]
    try:
        data = context.backend.get_bytes(reference["key"], expected_digest=reference["sha256"], expected_size=reference["size"])
    except (BotoCoreError, ClientError, OSError, ValueError, RuntimeError):
        _fail("native-reference-object-unavailable")
    if len(data) != reference["size"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
        _fail("native-reference-object-invalid")
    return data


def _put_blob(context, cipher):
    digest = hashlib.sha256(cipher).hexdigest()
    reference = context.backend.put_bytes("objects/sha256/" + digest, cipher)
    value = {"key": reference.key, "sha256": reference.sha256, "size": reference.size}
    _validate_object(value)
    if value != {"key": "objects/sha256/" + digest, "sha256": digest, "size": len(cipher)}:
        _fail("native-reference-object-invalid")
    _blob(context, {"object": value})
    return value


def _scope_digest(context):
    return hashlib.sha256(_canonical_json([context.physical_binding, context.material.encryption_domain_id,
                                          context.repository_info.repository_id])).hexdigest()


def _pending(context, objects, operation):
    directory = context.instance / "agent-reference-receipts"
    directory.mkdir(mode=0o700, exist_ok=True)
    from .private_paths import verify_private_path

    verify_private_path(directory, directory=True)
    identity = uuid.uuid4().hex
    path = directory / (identity + ".json")
    native._write(path, _canonical_json({"format": "josh-room.agent-reference-pending.v1", "id": identity,
        "scope_sha256": _scope_digest(context), "operation": operation, "objects": objects}))
    native._fsync_directory(directory)
    return path


def _pending_objects(context):
    directory = context.instance / "agent-reference-receipts"
    if not directory.exists():
        return set(), []
    from .private_paths import verify_private_path

    verify_private_path(directory, directory=True)
    paths = sorted(directory.iterdir())
    if len(paths) > 1024:
        _fail("native-reference-receipt-limit")
    objects = set()
    receipts = []
    for path in paths:
        document = native._json(native._read(path, MAX_CONTROL))
        if (not isinstance(document, dict) or set(document) != {"format", "id", "scope_sha256", "operation", "objects"}
                or document["format"] != "josh-room.agent-reference-pending.v1"
                or path.name != document["id"] + ".json" or not isinstance(document["objects"], list)
                or len(document["objects"]) > 64):
            _fail("native-reference-receipt-invalid")
        if document["scope_sha256"] != _scope_digest(context):
            continue
        for reference in document["objects"]:
            _validate_object(reference)
            objects.add(reference["key"])
        receipts.append(document["id"])
    return objects, receipts


def _decode_entry(context, entry, data=None):
    with _cipher_file(context, _blob(context, entry) if data is None else data) as source:
        decoded, recipients = native._decode(source, [context.material.identity], entry["archive_binding"], entry["tool"])
        plain = native._json(native._decrypt_to_memory(source, [context.material.identity]))
    if recipients != _recipients(context):
        _fail("native-reference-recipient-mismatch")
    _sessions(decoded, entry["tool"])
    return decoded, plain


def inspect_reference(context):
    document, _ = _load(context)
    if document is None:
        return None
    return {"state": "history-reference", "history_only": True, "native_resume_verified": False,
            "original_t3_identity_restored": False, "captures_closed": document["captures_closed"],
            "entries": [{"profile": entry["profile"], "tool": entry["tool"], "scope": entry["scope"],
                         "capture_closed": document["captures_closed"]} for entry in document["entries"]]}


def capture_reference(context, *, grant, tool):
    workspace = grant["workspace"]
    actual, binding, admitted = _grant(context, grant["profile"], tool, workspace, "room-capture")
    if actual != grant:
        _fail("native-profile-selector-stale-or-unavailable")
    _workspace(context, workspace)
    _same_history(context)
    document, etag = _load(context)
    if document is not None and (document["captures_closed"] or any((entry["profile"], entry["tool"]) == (binding["profile"], tool) for entry in document["entries"])):
        _fail("native-reference-capture-closed-or-collision")
    root = Path(grant["profile_root"])
    output = Path(grant["local_reference_root"]) / ("room-capture-" + uuid.uuid4().hex + ".age")
    published = False
    publication_confirmed = False
    uploaded = False
    pending = None
    try:
        native.capture(root, output=output, recipients=_recipients(context), tool=tool, **binding)
        decoded, recipients = native._decode(output, [context.material.identity], binding, tool)
        if recipients != _recipients(context) or not decoded:
            _fail("native-reference-object-invalid")
        _sessions(decoded, tool)
        cipher = native._read(output, native.MAX_BYTES * 2)
        if _grant(context, binding["profile"], tool, workspace, "room-capture") != (grant, binding, admitted):
            _fail("native-admission-changed")
        _workspace(context, workspace)
        _same_history(context)
        digest = hashlib.sha256(cipher).hexdigest()
        pending = _pending(context, [{"key": "objects/sha256/" + digest, "sha256": digest, "size": len(cipher)}], "capture")
        uploaded = True
        reference = _put_blob(context, cipher)
        entry = {"profile": binding["profile"], "tool": tool, "scope": grant["scope"],
                 "created_at": datetime.now(UTC).isoformat(), "archive_format": native._native_format(tool),
                 "object": reference,
                 "archive_binding": binding, "origin": _target(context)}
        _blob(context, entry)
        _workspace(context, workspace)
        if _grant(context, binding["profile"], tool, workspace, "room-capture")[2] != admitted:
            _fail("native-admission-changed")
        _same_history(context)
        document = {"format": FORMAT, "binding": _target(context), "revision": 1 if document is None else document["revision"] + 1,
                    "captures_closed": False, "entries": ([] if document is None else document["entries"]) + [entry]}
        published = True
        def commit_guard():
            if _grant(context, binding["profile"], tool, workspace, "room-capture") != (grant, binding, admitted):
                _fail("native-admission-changed")
            _workspace(context, workspace)
            _same_history(context)

        _publish(context, document, etag, before_commit=commit_guard)
        publication_confirmed = True
        if _grant(context, binding["profile"], tool, workspace, "room-capture") != (grant, binding, admitted):
            _fail("native-admission-changed")
        _same_history(context)
        pending.unlink()
        return {"ok": True, **inspect_reference(context)}
    except (KeyError, ValueError, RuntimeError, OSError, BotoCoreError, ClientError, KeyboardInterrupt, CLICancelled) as error:
        if published or uploaded:
            if isinstance(error, native.NativeAgentError) and str(error) == "native-reference-conflict":
                raise NativeReferenceError("native-reference-conflict", sidecar_published=False) from None
            raise NativeReferenceError("native-reference-publication-uncertain",
                                       sidecar_published=True if publication_confirmed else None) from None
        raise
    finally:
        try:
            output.unlink(missing_ok=True)
        except (OSError, KeyboardInterrupt, CLICancelled):
            if uploaded or published:
                _fail("native-reference-publication-uncertain", committed=True)
            raise


def verify_reference(context, *, grant, tool, export_path=None):
    document = _load_export(context, export_path)[0] if export_path else _load(context)[0]
    if document is None:
        _fail("native-reference-unavailable")
    entry = _entry(context, document, grant["profile"], tool)
    if grant["workspace"] != entry["archive_binding"]["workspace"]:
        _fail("path-mismatch")
    blobs = _load_export(context, export_path)[1] if export_path else None
    current, _, _ = _grant(context, grant["profile"], tool, grant["workspace"], "room-verify")
    if current != grant:
        _fail("native-profile-selector-stale-or-unavailable")
    guard = _authority_guard(context, entry, "room-verify")
    guard()
    decoded, _ = _decode_entry(context, entry, None if blobs is None else blobs[entry["object"]["key"]])
    sessions = _sessions(decoded, tool)
    guard()
    _same_history(context)
    return {"ok": True, "state": "history-verified", "history_only": True, "native_resume_verified": False,
            "original_t3_identity_restored": False, "sessions": sessions}


def restore_reference(context, *, grant, tool, session_id, export_path=None):
    document, blobs = _load_export(context, export_path) if export_path else (_load(context)[0], None)
    if document is None:
        _fail("native-reference-unavailable")
    entry = _entry(context, document, grant["profile"], tool)
    workspace = entry["archive_binding"]["workspace"]
    if grant["workspace"] != workspace:
        _fail("path-mismatch")
    _workspace(context, workspace, captured_baseline=False)
    actual, binding, _ = _grant(context, entry["profile"], tool, workspace, "room-verify")
    if actual != grant:
        _fail("native-profile-selector-stale-or-unavailable")
    cipher = _blob(context, entry) if blobs is None else blobs[entry["object"]["key"]]
    committed = False
    try:
        with _cipher_file(context, cipher, directory=grant["local_reference_root"]) as source:
            _, _, admitted = _grant(context, entry["profile"], tool, workspace, "room-materialize", destination=source)
            try:
                result = native._materialize_bound_session(Path(grant["profile_root"]), source=source,
                    identity_paths=[context.material.identity], archive_binding=entry["archive_binding"],
                    session_id=session_id, tool=tool, **binding)
            except (KeyboardInterrupt, CLICancelled):
                committed = True
                raise
            committed = True
        if _grant(context, entry["profile"], tool, workspace, "room-materialize", destination=source)[2] != admitted:
            _fail("native-admission-changed")
        _same_history(context)
    except (ValueError, RuntimeError, OSError, KeyboardInterrupt, CLICancelled) as error:
        if committed or getattr(error, "committed", False):
            _fail("native-reference-materialization-uncertain", committed=True)
        raise
    return {**result, "history_only": True, "original_t3_identity_restored": False}


def preflight_copy(source_context, destination_context, destination_room_id):
    document, _ = _load(source_context)
    if document is None:
        return
    destination = dataclasses.replace(destination_context, project_id=destination_room_id)
    for entry in document["entries"]:
        for context in (source_context, destination):
            grant, _, _ = _grant(context, entry["profile"], entry["tool"], entry["archive_binding"]["workspace"], "room-copy")
            if grant["scope"] != entry["scope"]:
                _fail("native-reference-scope-mismatch")
        _decode_entry(source_context, entry)


def copy_reference(source_context, destination_context, destination_room_id, destination_logical_jat_id):
    document, _ = _load(source_context)
    if document is None:
        return None
    preflight_copy(source_context, destination_context, destination_room_id)
    destination = _fresh(destination_context, destination_room_id, destination_logical_jat_id)
    if _load(destination)[0] is not None:
        _fail("native-reference-capture-closed-or-collision")
    entries = []
    uploaded = 0
    pending = None
    guards = [_authority_guard(context, entry, "room-copy")
              for entry in document["entries"] for context in (source_context, destination)]

    def guard():
        for check in guards:
            check()

    affected = False
    try:
        for entry in document["entries"]:
            guard()
            _, plain = _decode_entry(source_context, entry)
            plain["recipients"] = _recipients(destination)
            cipher = _encrypt(_canonical_json(plain), _recipients(destination))
            guard()
            digest = hashlib.sha256(cipher).hexdigest()
            references = [value["object"] for value in entries] + [
                {"key": "objects/sha256/" + digest, "sha256": digest, "size": len(cipher)}]
            new_pending = _pending(destination, references, "copy")
            if pending:
                pending.unlink()
            pending = new_pending
            guard()
            affected = True
            reference = _put_blob(destination, cipher)
            entries.append({**entry, "object": reference})
            uploaded += len(cipher)
            guard()
        _same_history(source_context)
        guard()
        _publish(destination, {"format": FORMAT, "binding": _target(destination), "revision": 1,
                             "captures_closed": document["captures_closed"], "entries": entries}, None, before_commit=guard)
        guard()
        _same_history(destination)
        pending.unlink()
    except (KeyError, ValueError, RuntimeError, OSError, BotoCoreError, ClientError, KeyboardInterrupt, CLICancelled):
        if affected:
            _fail("native-reference-copy-uncertain", committed=True)
        raise
    return {"state": "history-reference", "history_only": True, "native_resume_verified": False, "data_uploaded": uploaded}


def preflight_export(context, output):
    document, _ = _load(context)
    if document is None:
        return
    destination = Path(str(output) + ".agents.age")
    if destination.exists() or destination.is_symlink():
        _fail("native-reference-export-collision")
    for entry in document["entries"]:
        _grant(context, entry["profile"], entry["tool"], entry["archive_binding"]["workspace"], "room-export")
        _decode_entry(context, entry)


def export_reference(context, output):
    document, _ = _load(context)
    if document is None:
        return None
    preflight_export(context, output)
    guards = [_authority_guard(context, entry, "room-export") for entry in document["entries"]]

    def guard():
        for check in guards:
            check()

    blobs = {}
    for entry in document["entries"]:
        guard()
        blobs[entry["object"]["key"]] = base64.b64encode(_blob(context, entry)).decode()
    data = _canonical_json({"format": EXPORT_FORMAT, "reference": document, "objects": blobs})
    if len(data) > MAX_EXPORT - 2048:
        _fail("native-reference-export-limit")
    cipher = _encrypt(data, _recipients(context))
    if len(cipher) > MAX_EXPORT:
        _fail("native-reference-export-limit")
    destination = native._canonical(Path(str(output) + ".agents.age"), exists=False)
    affected = False
    try:
        with _cipher_file(context, cipher, directory=destination.parent) as temporary:
            _same_history(context)
            guard()
            affected = True
            os.link(temporary, destination)
            native._fsync_directory(destination.parent)
            guard()
            _same_history(context)
    except (KeyError, ValueError, RuntimeError, OSError, BotoCoreError, ClientError, KeyboardInterrupt, CLICancelled):
        if affected:
            _fail("native-reference-export-uncertain", committed=True)
        raise
    return {"state": "encrypted-agent-companion", "history_only": True, "native_resume_verified": False,
            "output": str(destination), "sha256": hashlib.sha256(cipher).hexdigest(), "size": len(cipher)}


def _load_export(context, source):
    plain = native._decrypt_to_memory(Path(source), [context.material.identity], MAX_EXPORT)
    bundle = native._json(plain)
    if not isinstance(bundle, dict) or set(bundle) != {"format", "reference", "objects"} or bundle["format"] != EXPORT_FORMAT or not isinstance(bundle["objects"], dict):
        _fail("native-reference-export-invalid")
    document = _validate(bundle["reference"], _target(context))
    if set(bundle["objects"]) != {entry["object"]["key"] for entry in document["entries"]}:
        _fail("native-reference-export-invalid")
    blobs = {}
    for entry in document["entries"]:
        reference = entry["object"]
        data = base64.b64decode(bundle["objects"][reference["key"]], validate=True)
        if len(data) != reference["size"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
            _fail("native-reference-export-invalid")
        blobs[reference["key"]] = data
    return document, blobs


def _record_context(context, room_id, logical_id, descriptors):
    record = context.catalog.resolve_snapshot(room_id, logical_id)
    if descriptors is None:
        from .room_store_bridge import _load_descriptor

        descriptor = _load_descriptor(context.backend, context.instance, context.material, room_id, record, context.private_dir)
    else:
        descriptor = descriptors[(room_id, logical_id)]
    return dataclasses.replace(context, project_id=room_id, selected_record=record, selected_descriptor=descriptor)


def reconcile_references(context, descriptors=None, candidates=()):
    pending, receipt_ids = _pending_objects(context)
    candidates = set(candidates) | pending
    reachable = set()
    points = 0
    for room_id, room in context.catalog.body["projects"].items():
        for logical_id, record in room["snapshots"].items():
            if record.get("payload_kind") != "room-store-v1":
                continue
            selected = _record_context(context, room_id, logical_id, descriptors)
            document, _ = _load(selected)
            if document is None:
                continue
            points += 1
            for entry in document["entries"]:
                _blob(selected, entry)
                reachable.add(entry["object"]["key"])
    if not points and not candidates:
        return None
    return {"state": "history-reference-reachability", "history_only": True, "native_resume_verified": False,
            "recovery_points": points, "reachable_objects": sorted(reachable),
            "unreachable_candidates": sorted(set(candidates) - reachable), "pending_receipts": receipt_ids,
            "automatic_cleanup": False}


def close_for_records(context, identities, descriptors):
    receipts = []
    for identity in sorted(identities, key=lambda value: (value.room_id, value.logical_jat_id)):
        if context.catalog.resolve_snapshot(identity.room_id, identity.logical_jat_id).get("payload_kind") != "room-store-v1":
            continue
        selected = _record_context(context, identity.room_id, identity.logical_jat_id, descriptors)
        document, etag = _load(selected)
        if document is None:
            continue
        guards = [_authority_guard(selected, entry, "room-remove") for entry in document["entries"]]
        if not document["captures_closed"]:
            document = {**document, "revision": document["revision"] + 1, "captures_closed": True}
            for guard in guards:
                guard()
            try:
                def before_close(checks=tuple(guards), selected_context=selected):
                    for check in checks:
                        check()
                    _same_history(selected_context)

                _publish(selected, document, etag, before_commit=before_close)
                for guard in guards:
                    guard()
                _same_history(selected)
            except (ValueError, RuntimeError, OSError, BotoCoreError, ClientError, KeyboardInterrupt, CLICancelled):
                _fail("native-reference-closure-uncertain", committed=True)
        receipts.append({"room_id": identity.room_id, "logical_jat_id": identity.logical_jat_id,
                         "control_key": _key(document["binding"]),
                         "target_sha256": hashlib.sha256(_canonical_json(document["binding"])).hexdigest(),
                         "objects": [entry["object"] for entry in document["entries"]]})
    return tuple(receipts)


def complete_removed_references(context, receipts, *, descriptors=None):
    if not receipts:
        return None
    candidates = {}
    guards_by_key = {}
    catalog_objects = {record["object_key"] for room in context.catalog.body["projects"].values()
                       for record in room["snapshots"].values()}
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {"room_id", "logical_jat_id", "control_key", "target_sha256", "objects"}:
            _fail("native-reference-removal-receipt-invalid")
        from .encryption_domain import is_control_key

        if not isinstance(receipt["control_key"], str) or not is_control_key(receipt["control_key"]) or not receipt["control_key"].startswith("control/agent-references/v1/"):
            _fail("native-reference-removal-receipt-invalid")
        cipher, _etag = context.backend.read_control(receipt["control_key"], MAX_CONTROL)
        if not isinstance(cipher, bytes):
            _fail("native-reference-removal-receipt-invalid")
        with _cipher_file(context, cipher) as source:
            document = native._json(native._decrypt_to_memory(source, [context.material.identity], MAX_CONTROL))
        _validate(document, document.get("binding"))
        target = document["binding"]
        if (document["captures_closed"] is not True or _key(target) != receipt["control_key"]
                or hashlib.sha256(_canonical_json(target)).hexdigest() != receipt["target_sha256"]
                or target["room_id"] != receipt["room_id"] or target["logical_jat_id"] != receipt["logical_jat_id"]
                or target["dimension_id"] != context.dimension.dimension_id
                or target["encryption_domain_id"] != context.material.encryption_domain_id
                or target["physical_binding"] != context.physical_binding
                or target["repository_id"] != context.repository_info.repository_id
                or [entry["object"] for entry in document["entries"]] != receipt["objects"]):
            _fail("native-reference-removal-receipt-invalid")
        if (receipt["room_id"] in context.catalog.body["projects"]
                and receipt["logical_jat_id"] in context.catalog.body["projects"][receipt["room_id"]]["snapshots"]):
            _fail("native-reference-removal-not-committed")
        selected = dataclasses.replace(context, project_id=receipt["room_id"])
        for entry in document["entries"]:
            guard = _authority_guard(selected, entry, "room-remove")
            guards_by_key.setdefault(entry["object"]["key"], []).append(guard)
        for reference in receipt["objects"]:
            _validate_object(reference)
            if reference["key"] in catalog_objects:
                _fail("native-reference-object-still-referenced")
            candidates[reference["key"]] = reference
    report = reconcile_references(context, descriptors, candidates)
    pending = []
    removed = []
    for key in sorted(set(report["unreachable_candidates"]) & candidates.keys()):
        for guard in guards_by_key[key]:
            guard()
        try:
            context.backend.delete_object(key)
            removed.append(key)
            for guard in guards_by_key[key]:
                guard()
        except (KeyboardInterrupt, CLICancelled):
            _fail("native-reference-cleanup-uncertain", committed=True)
        except (R2PublicationError, BotoCoreError, ClientError, OSError, ValueError, RuntimeError):
            pending.append(key)
    return {"state": "cleanup-pending" if pending else "references-removed", "history_only": True,
            "native_resume_verified": False, "removed_objects": removed, "pending_objects": pending,
            "captures_closed": True, "reconciliation_required": bool(pending)}
