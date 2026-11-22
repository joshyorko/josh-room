"""Native reference behavior with real age and synthetic storage/host authority."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import native_agent_reference as reference
from josh_room import native_agents as native
from josh_room.cancellation import CLICancelled
from josh_room.catalog import Catalog
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat
from josh_room.r2 import R2Conflict, R2PublicationError
from josh_room.room_store_bridge import ExistingRoomStoreContext
from josh_room.room_store_operations import scan_workspace_for_status
from josh_room.room_store_references import LogicalJatIdentity
from josh_room.workspace_state import write_stat_workspace_marker

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native-history reference contract")
DOMAIN = "00000000-0000-4000-8000-000000000001"
SESSION = "11111111-1111-4111-8111-111111111111"
STAMP = "2026-11-22T15:00:00Z"


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.controls = {}
        self.writes = []
        self.publication_fault = None
        self.catalog_etag = "catalog-1"

    def put_bytes(self, key, body):
        digest = hashlib.sha256(body).hexdigest()
        assert key == "objects/sha256/" + digest
        self.objects[key] = body
        self.writes.append(("object", key))
        return ObjectRef(key, digest, len(body))

    def get_bytes(self, key, expected_digest=None, expected_size=None):
        try:
            body = self.objects[key]
        except KeyError:
            raise FileNotFoundError("synthetic missing encrypted object") from None
        if expected_digest is not None and hashlib.sha256(body).hexdigest() != expected_digest:
            raise ValueError("ciphertext digest mismatch")
        if expected_size is not None and len(body) != expected_size:
            raise ValueError("ciphertext size mismatch")
        return body

    def read_control(self, key, max_bytes):
        body = self.controls.get(key)
        if body is None:
            return None, None
        assert len(body) <= max_bytes
        return body, hashlib.sha256(body).hexdigest()

    def create_control(self, key, body):
        if key in self.controls or self.publication_fault == "conflict":
            raise R2Conflict("synthetic conflict")
        return self._publish(key, body)

    def replace_control(self, key, body, expected_etag):
        if self.read_control(key, len(body) + 65536)[1] != expected_etag:
            raise R2Conflict("synthetic conflict")
        return self._publish(key, body)

    def _publish(self, key, body):
        self.controls[key] = body
        self.writes.append(("control", key))
        if self.publication_fault == "lost-response":
            raise R2PublicationError("synthetic lost response", published=True)
        return hashlib.sha256(body).hexdigest()

    def delete_object(self, key):
        self.objects.pop(key, None)
        self.writes.append(("delete", key))

    def conditional_catalog_put(self, body, expected_etag):
        if expected_etag != self.catalog_etag:
            raise R2Conflict("synthetic catalog conflict")
        self.catalog_body = body
        self.catalog_etag = hashlib.sha256(body).hexdigest()
        self.writes.append(("catalog", self.catalog_etag))
        return self.catalog_etag


def descriptor(room, logical_id, scan):
    return LogicalJat.from_dict({
        "format_version": 1,
        "payload_kind": "room-store-v1",
        "logical_jat_id": logical_id,
        "dimension_id": "archive",
        "encryption_domain_id": DOMAIN,
        "room_id": room,
        "created_at": STAMP,
        "capture_policy_sha256": scan.capture_policy_sha256,
        "workspace": {
            "engine": "restic", "repository_id": "a" * 64, "repository_format": 2,
            "snapshot_id": "b" * 64, "tree_id": "c" * 64, "source_path": ".",
            "logical_bytes": scan.logical_bytes, "data_added": 0, "data_added_packed": 0,
        },
        "components": {"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
        "source": {},
        "producer": {
            "josh_room_version": "0.1.0", "restic_version": "0.19.1",
            "source_platform": "linux-x64", "restore_platforms": ["linux-x64"],
        },
    })


def add_record(catalog, item, scan):
    encoded = item.to_json().encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return catalog.add_logical_snapshot(item.to_dict()["room_id"], "Synthetic Room", item,
        ObjectRef("objects/sha256/" + digest, digest, len(encoded)), scan.signature)


@pytest.fixture
def room(tmp_path, monkeypatch):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age and age-keygen are required")
    tmp_path.chmod(0o700)
    identities, recipients = [], []
    for index in range(2):
        identity = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        identity.chmod(0o600)
        result = subprocess.run(["age-keygen", "-y", str(identity)], check=True, capture_output=True, text=True)
        identities.append(identity)
        recipients.append(result.stdout.strip())
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "hello.txt").write_text("synthetic workspace\n")
    scan = scan_workspace_for_status(workspace)
    item = descriptor("demo", "history-first", scan)
    catalog = add_record(Catalog.empty("archive", DOMAIN), item, scan)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    store = MemoryStore()
    material = SimpleNamespace(identity=identities[0], recipient=recipients[0], encryption_domain_id=DOMAIN,
                               keyset=SimpleNamespace(key_generation=1, recovery_recipients=(recipients[1],)))
    context = ExistingRoomStoreContext(
        instance=private, backend=store, catalog=catalog, catalog_etag="catalog-1", store=None,
        private_dir=private, material=material, dimension=SimpleNamespace(dimension_id="archive"),
        project_id="demo", repository_info=SimpleNamespace(repository_id="a" * 64),
        selected_record=catalog.resolve_snapshot("demo", "history-first"), selected_descriptor=item,
        physical_binding="minio:http://127.0.0.1:19000:synthetic",
    )
    grants = {}
    contexts = {("demo", "history-first"): context}
    receipt = {"admitted": True, "authority_sha256": "d" * 64}
    monkeypatch.setattr(native, "_admit", lambda *args, **kwargs: receipt.copy())

    def configure(selected, name):
        profile = tmp_path / name
        references = tmp_path / (name + "-references")
        references.mkdir(mode=0o700)
        binding = {"dimension": selected.dimension.dimension_id, "room": selected.project_id, "profile": "personal", "workspace": str(workspace)}
        selected_recipients = [selected.material.recipient, *selected.material.keyset.recovery_recipients]
        native.configure(profile, recipients=selected_recipients, **binding)
        grant = {**binding, "profile_root": str(profile), "local_reference_root": str(references), "scope": "personal"}
        grants[selected.project_id] = grant
        return grant

    grant = configure(context, "profile")

    def admit(selected, profile, tool, actual_workspace, operation, *, destination=None):
        assert tool in {"codex", "claude"}
        assert actual_workspace == str(workspace)
        selected_grant = grants[selected.project_id]
        assert selected_grant["profile"] == profile
        if destination is not None:
            assert Path(destination).is_relative_to(Path(selected_grant["local_reference_root"]))
        binding = {"dimension": selected.dimension.dimension_id, "room": selected.project_id, "profile": profile, "workspace": actual_workspace}
        return selected_grant, binding, receipt.copy()

    monkeypatch.setattr(reference, "_grant", admit)
    def fresh(selected, room_id=None, logical_id=None):
        current = contexts[(room_id or selected.project_id, logical_id or selected.selected_record["snapshot_id"])]
        return dataclasses.replace(current, catalog_etag=current.backend.catalog_etag)

    monkeypatch.setattr(reference, "_fresh", fresh)
    relative = Path("sessions/2026/11/22") / f"rollout-2026-11-22T15-00-00-{SESSION}.jsonl"
    native_file = Path(grant["profile_root"]) / "codex" / relative
    native_file.parent.mkdir(mode=0o700, parents=True)
    for parent in native_file.parents:
        if parent.is_relative_to(Path(grant["profile_root"])):
            parent.chmod(0o700)
    rows = [
        {"timestamp": STAMP, "type": "session_meta", "payload": {
            "id": SESSION, "timestamp": STAMP, "cwd": str(workspace), "originator": "codex_cli_rs",
            "cli_version": "0.171.0", "source": "cli",
        }},
        {"timestamp": STAMP, "type": "event_msg", "payload": {
            "type": "user_message", "message": "synthetic retained question", "images": None, "local_images": [],
        }},
        {"timestamp": STAMP, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "synthetic retained answer"}],
        }},
    ]
    payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    native_file.write_bytes(payload)
    native_file.chmod(0o600)
    fixture = SimpleNamespace(context=context, store=store, grant=grant, workspace=workspace, scan=scan,
                              identities=identities, recipients=recipients, native_file=native_file, payload=payload,
                              relative=relative, configure=configure, grants=grants, contexts=contexts, root=tmp_path,
                              receipt=receipt)
    mark(fixture, context)
    return fixture


def mark(room, context):
    write_stat_workspace_marker(room.workspace, dimension_id=context.dimension.dimension_id, project_id=context.project_id,
        snapshot_id=context.selected_record["snapshot_id"], encryption_domain_id=context.material.encryption_domain_id, display_name="Synthetic Room",
        workspace_signature=room.scan.signature, signature_algorithm=room.scan.signature_algorithm,
        capture_policy_sha256=room.scan.capture_policy_sha256)


def capture(room):
    return reference.capture_reference(room.context, grant=room.grant, tool="codex")


def documents(room, context=None):
    return reference._load(context or room.context)[0]


def test_absent_references_preserve_opt_out_without_remote_writes(room):
    assert reference.inspect_reference(room.context) is None
    assert reference.export_reference(room.context, room.root / "snapshot.haul") is None
    assert reference.copy_reference(room.context, room.context, "other", "other-point") is None
    assert reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor}) is None
    assert reference.close_for_records(room.context, {LogicalJatIdentity("demo", "history-first")},
        {("demo", "history-first"): room.context.selected_descriptor}) == ()
    assert reference.complete_removed_references(room.context, ()) is None
    assert room.store.writes == []


def test_unqualified_r2_keeps_existing_operations_outside_native_sidecar_namespace(room, monkeypatch):
    selected = dataclasses.replace(room.context, dimension=SimpleNamespace(dimension_id="demo", provider="r2"))
    monkeypatch.setattr(room.store, "read_control", lambda *args: pytest.fail("R2 native sidecars are unqualified"))
    assert reference.inspect_reference(selected) is None
    reference.preflight_export(selected, room.root / "portable.haul")
    assert room.store.writes == []


def test_malformed_encrypted_metadata_never_echoes_private_values(room):
    capture(room)
    document = documents(room)
    document["entries"][0]["created_at"] = "/synthetic/private/metadata-value"
    with pytest.raises(native.NativeAgentError, match="native-reference-format-invalid") as caught:
        reference._validate(document, reference._target(room.context))
    assert "/synthetic/private/metadata-value" not in str(caught.value)


def test_cleanup_never_deletes_a_live_catalog_object_from_misbound_reference(room):
    capture(room)
    other = descriptor("retained-room", "retained-point", room.scan)
    catalog = add_record(room.context.catalog, other, room.scan)
    record = catalog.resolve_snapshot("retained-room", "retained-point")
    retained = other.to_json().encode()
    room.store.objects[record["object_key"]] = retained
    document, etag = reference._load(room.context)
    document["entries"][0]["object"] = {"key": record["object_key"], "sha256": record["ciphertext_sha256"],
                                         "size": record["ciphertext_size"]}
    document["revision"] += 1
    reference._publish(room.context, document, etag)
    selected = dataclasses.replace(room.context, catalog=catalog)
    receipts = reference.close_for_records(selected, [LogicalJatIdentity("demo", "history-first")],
        {("demo", "history-first"): room.context.selected_descriptor})
    remaining = catalog.remove_project("demo")[0]
    fresh = dataclasses.replace(selected, catalog=remaining, project_id=None, selected_record=None, selected_descriptor=None)
    with pytest.raises(native.NativeAgentError, match="object-still-referenced"):
        reference.complete_removed_references(fresh, receipts, descriptors={("retained-room", "retained-point"): other})
    assert room.store.objects[record["object_key"]] == retained


def test_context_cannot_reuse_same_named_admission_for_another_physical_bucket(room, monkeypatch):
    monkeypatch.undo()
    from josh_room import native_agent_admission as authority

    monkeypatch.setattr(reference.discovery, "_matching_grants", lambda *args: [("opaque", room.grant, ["codex"])])
    receipt = {"authority_sha256": authority.authority_digest(authority.FORMAT_V3, "codex", room.grant,
                                                               room.context.physical_binding, 1)}
    monkeypatch.setattr(native, "_admit", lambda *args, **kwargs: receipt)
    assert reference._grant(room.context, "personal", "codex", str(room.workspace), "room-capture")[0] == room.grant
    other = dataclasses.replace(room.context, physical_binding="minio:https://other.example.invalid:another-bucket")
    with pytest.raises(native.NativeAgentError, match="authority-context-mismatch"):
        reference._grant(other, "personal", "codex", str(room.workspace), "room-capture")
    assert room.store.writes == []


def test_capture_is_encrypted_and_verify_restores_original_identity_without_overwrite(room):
    result = capture(room)
    assert result["state"] == "history-reference" and result["history_only"] is True
    assert result["native_resume_verified"] is False
    assert str(room.workspace) not in json.dumps(result)
    assert "synthetic retained" not in json.dumps(result)
    for body in [*room.store.controls.values(), *room.store.objects.values()]:
        assert body.startswith(b"age-encryption.org/v1")
        assert b"synthetic retained" not in body and str(room.workspace).encode() not in body
    verified = reference.verify_reference(room.context, grant=room.grant, tool="codex")
    assert verified["sessions"] == [{"provider_session_id": SESSION, "tool": "codex", "cwd_matches": True}]
    room.native_file.unlink()
    restored = reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION)
    assert restored["state"] == "history-materialized" and restored["native_resume_verified"] is False
    assert room.native_file.read_bytes() == room.payload
    assert reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION)["recovered_commit"] is True
    room.native_file.write_bytes(room.payload + b"\n")
    with pytest.raises(native.NativeAgentError, match="target-mismatch"):
        reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION)


@pytest.mark.parametrize("change", ["point", "path", "contents"])
def test_capture_refuses_stale_moved_or_changed_workspace_before_upload(room, change):
    if change == "contents":
        (room.workspace / "hello.txt").write_text("changed today\n")
    else:
        marker = room.workspace / ".josh-room.json"
        body = json.loads(marker.read_bytes())
        body["snapshot_id" if change == "point" else "workspace_path_sha256"] = "older-point" if change == "point" else "0" * 64
        marker.write_text(json.dumps(body))
    with pytest.raises(native.NativeAgentError, match="native-reference-workspace"):
        capture(room)
    assert room.store.writes == []


@pytest.mark.parametrize("fault", ["conflict", "lost-response"])
def test_capture_cas_failure_distinguishes_sidecar_outcome_from_uploaded_cipher(room, fault):
    room.store.publication_fault = fault
    with pytest.raises(native.NativeAgentError) as caught:
        capture(room)
    assert caught.value.committed is True
    assert caught.value.sidecar_published is (False if fault == "conflict" else None)
    assert caught.value.orphaned is (True if fault == "conflict" else None)
    assert str(caught.value) == ("native-reference-conflict" if fault == "conflict" else "native-reference-publication-uncertain")
    assert bool(room.store.controls) is (fault == "lost-response")
    assert not list(Path(room.grant["local_reference_root"]).iterdir())


@pytest.mark.parametrize("change", ["missing", "corrupt"])
def test_verified_reference_refuses_missing_or_corrupt_native_ciphertext(room, change):
    capture(room)
    key = documents(room)["entries"][0]["object"]["key"]
    if change == "missing":
        del room.store.objects[key]
    else:
        room.store.objects[key] += b"tampered"
    with pytest.raises(native.NativeAgentError, match="unavailable|invalid"):
        reference.verify_reference(room.context, grant=room.grant, tool="codex")


@pytest.mark.parametrize("field,value", [
    ("logical_jat_id", "different-point"), ("encryption_domain_id", "00000000-0000-4000-8000-000000000002"),
    ("key_generation", 2), ("descriptor_sha256", "f" * 64),
])
def test_sidecar_replay_binding_is_refused(room, field, value):
    capture(room)
    body = documents(room)
    body["binding"][field] = value
    key = reference._key(reference._target(room.context))
    room.store.controls[key] = reference._encrypt(reference._canonical_json(body), room.recipients)
    with pytest.raises(native.NativeAgentError, match="historical-binding-mismatch"):
        reference.inspect_reference(room.context)


def test_encrypted_companion_is_standalone_verified_and_collision_safe(room):
    capture(room)
    result = reference.export_reference(room.context, room.root / "snapshot.haul")
    companion = Path(result["output"])
    assert companion.name == "snapshot.haul.agents.age"
    assert companion.read_bytes().startswith(b"age-encryption.org/v1")
    assert b"synthetic retained" not in companion.read_bytes()
    room.store.objects.clear()
    room.native_file.unlink()
    verified = reference.verify_reference(room.context, grant=room.grant, tool="codex", export_path=companion)
    assert verified["sessions"][0]["provider_session_id"] == SESSION
    reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION, export_path=companion)
    assert room.native_file.read_bytes() == room.payload
    with pytest.raises(native.NativeAgentError, match="export-collision"):
        reference.export_reference(room.context, room.root / "snapshot.haul")


def test_companion_rejects_inner_ciphertext_tampering(room):
    capture(room)
    result = reference.export_reference(room.context, room.root / "snapshot.haul")
    companion = Path(result["output"])
    bundle = native._json(native._decrypt_to_memory(companion, room.identities[:1], reference.MAX_EXPORT))
    first = next(iter(bundle["objects"]))
    bundle["objects"][first] = base64.b64encode(b"corrupted encrypted archive").decode()
    companion.write_bytes(reference._encrypt(reference._canonical_json(bundle), room.recipients))
    with pytest.raises(native.NativeAgentError, match="export-invalid"):
        reference.verify_reference(room.context, grant=room.grant, tool="codex", export_path=companion)


def test_copy_has_independent_ciphertext_origin_and_survives_source_removal(room):
    capture(room)
    source_entry = documents(room)["entries"][0]
    copied_descriptor = descriptor("copy", "copy-point", room.scan)
    copied_catalog = add_record(room.context.catalog, copied_descriptor, room.scan)
    copied = dataclasses.replace(room.context, catalog=copied_catalog, project_id="copy",
        selected_record=copied_catalog.resolve_snapshot("copy", "copy-point"), selected_descriptor=copied_descriptor)
    room.contexts[("copy", "copy-point")] = copied
    copied_grant = room.configure(copied, "copy-profile")
    result = reference.copy_reference(room.context, copied, "copy", "copy-point")
    copied_entry = documents(room, copied)["entries"][0]
    assert result["data_uploaded"] == copied_entry["object"]["size"] > 0
    assert copied_entry["object"]["key"] != source_entry["object"]["key"]
    assert copied_entry["origin"] == source_entry["origin"]
    assert copied_entry["archive_binding"] == source_entry["archive_binding"]
    closed = reference.close_for_records(room.context, {LogicalJatIdentity("demo", "history-first")},
        {("demo", "history-first"): room.context.selected_descriptor})
    body = json.loads(json.dumps(copied_catalog.body))
    del body["projects"]["demo"]
    fresh_catalog = Catalog.from_body(body)
    copied = dataclasses.replace(copied, catalog=fresh_catalog)
    room.contexts[("copy", "copy-point")] = copied
    cleanup = reference.complete_removed_references(copied, closed,
        descriptors={("copy", "copy-point"): copied_descriptor})
    assert cleanup["removed_objects"] == [source_entry["object"]["key"]]
    assert copied_entry["object"]["key"] in room.store.objects
    mark(room, copied)
    restored = reference.restore_reference(copied, grant=copied_grant, tool="codex", session_id=SESSION)
    assert restored["state"] == "history-materialized"
    assert (Path(copied_grant["profile_root"]) / "codex" / room.relative).read_bytes() == room.payload
    report = reference.reconcile_references(copied, {("copy", "copy-point"): copied_descriptor},
        candidates=[source_entry["object"]["key"]])
    assert report["reachable_objects"] == [copied_entry["object"]["key"]]
    assert report["unreachable_candidates"] == [source_entry["object"]["key"]]


def test_closed_reference_retains_readability_but_refuses_new_capture(room):
    capture(room)
    receipts = reference.close_for_records(room.context, {LogicalJatIdentity("demo", "history-first")},
        {("demo", "history-first"): room.context.selected_descriptor})
    assert receipts and str(room.workspace) not in json.dumps(receipts)
    assert reference.inspect_reference(room.context)["captures_closed"] is True
    assert reference.verify_reference(room.context, grant=room.grant, tool="codex")["sessions"][0]["provider_session_id"] == SESSION
    before = list(room.store.writes)
    with pytest.raises(native.NativeAgentError, match="capture-closed-or-collision"):
        capture(room)
    assert room.store.writes == before
    with pytest.raises(native.NativeAgentError, match="removal-not-committed"):
        reference.complete_removed_references(room.context, receipts,
            descriptors={("demo", "history-first"): room.context.selected_descriptor})


def test_claude_and_codex_have_separate_durable_native_formats(room):
    from josh_room.claude_native_history import project_key

    common = {"isSidechain": False, "userType": "external", "cwd": str(room.workspace),
              "sessionId": SESSION, "version": "2.1.310", "gitBranch": "synthetic", "timestamp": STAMP}
    user_id = "22222222-2222-4222-8222-222222222222"
    rows = [
        {**common, "parentUuid": None, "type": "user", "uuid": user_id,
         "message": {"role": "user", "content": "synthetic Claude question"}},
        {**common, "parentUuid": user_id, "type": "assistant", "uuid": "33333333-3333-4333-8333-333333333333",
         "message": {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic",
                     "content": [{"type": "text", "text": "synthetic Claude answer"}], "stop_reason": "end_turn",
                     "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    source = Path(room.grant["profile_root"]) / "claude/projects" / project_key(str(room.workspace)) / f"{SESSION}.jsonl"
    source.parent.mkdir(mode=0o700, parents=True)
    for parent in source.parents:
        if parent.is_relative_to(Path(room.grant["profile_root"])):
            parent.chmod(0o700)
    payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    source.write_bytes(payload)
    source.chmod(0o600)
    capture(room)
    reference.capture_reference(room.context, grant=room.grant, tool="claude")
    document = documents(room)
    assert {entry["archive_format"] for entry in document["entries"]} == {native.FORMAT, native.CLAUDE_FORMAT}
    assert reference.verify_reference(room.context, grant=room.grant, tool="claude")["sessions"] == [
        {"provider_session_id": SESSION, "tool": "claude", "cwd_matches": True}]
    cold_grant = room.configure(room.context, "claude-cold-profile")
    restored = reference.restore_reference(room.context, grant=cold_grant, tool="claude", session_id=SESSION)
    cold_file = Path(cold_grant["profile_root"]) / source.relative_to(Path(room.grant["profile_root"]))
    assert restored["native_resume_verified"] is False and cold_file.read_bytes() == payload
    assert source.read_bytes() == payload


def test_companion_refuses_another_historical_point_without_materializing(room):
    capture(room)
    output = reference.export_reference(room.context, room.root / "snapshot.haul")
    other_descriptor = descriptor("demo", "history-other", room.scan)
    other_catalog = add_record(room.context.catalog, other_descriptor, room.scan)
    other = dataclasses.replace(room.context, catalog=other_catalog,
        selected_record=other_catalog.resolve_snapshot("demo", "history-other"), selected_descriptor=other_descriptor)
    before = room.native_file.read_bytes()
    with pytest.raises(native.NativeAgentError, match="historical-binding-mismatch"):
        reference.restore_reference(other, grant=room.grant, tool="codex", session_id=SESSION, export_path=Path(output["output"]))
    assert room.native_file.read_bytes() == before


def test_rejected_capture_keeps_encrypted_orphan_for_explicit_reconciliation(room):
    room.store.publication_fault = "conflict"
    with pytest.raises(native.NativeAgentError, match="conflict") as caught:
        capture(room)
    assert caught.value.committed is True and caught.value.sidecar_published is False
    report = reference.reconcile_references(room.context, {("demo", "history-first"): room.context.selected_descriptor})
    assert report["reachable_objects"] == []
    assert report["unreachable_candidates"] == sorted(room.store.objects)
    assert report["automatic_cleanup"] is False
    assert not any(operation == "delete" for operation, _ in room.store.writes)


def test_deleted_point_after_publication_reports_uncertainty_and_unreachable_history(room, monkeypatch):
    def refreshed(_context, *args):
        if room.store.controls:
            raise KeyError("synthetic point removed")
        return room.context

    monkeypatch.setattr(reference, "_fresh", refreshed)
    with pytest.raises(native.NativeAgentError, match="publication-uncertain") as caught:
        capture(room)
    assert caught.value.committed is True
    assert caught.value.sidecar_published is True
    assert room.store.controls
    removed = dataclasses.replace(room.context, catalog=Catalog.empty("archive", DOMAIN))
    report = reference.reconcile_references(removed, {})
    assert report["reachable_objects"] == []
    assert report["unreachable_candidates"] == sorted(room.store.objects)


def test_first_capture_finishes_between_absent_close_and_catalog_removal(room, monkeypatch):
    from josh_room.room_store_lifecycle import (
        RoomStoreLifecycleError,
        remove_logical_catalog_records,
    )

    conditional_put = room.store.conditional_catalog_put
    raced = False

    def remove_after_capture(body, expected_etag):
        nonlocal raced
        if not raced:
            raced = True
            # Removal has already read ABSENT. Capture finishes, including its
            # historical postcheck and pending receipt deletion, before removal CAS.
            assert room.store.controls == {}
            assert capture(room)["ok"] is True
            assert reference._pending_objects(room.context) == (set(), [])
        return conditional_put(body, expected_etag)

    monkeypatch.setattr(room.store, "conditional_catalog_put", remove_after_capture)
    identities = {LogicalJatIdentity("demo", "history-first")}
    descriptors = {("demo", "history-first"): room.context.selected_descriptor}
    with pytest.raises(RoomStoreLifecycleError, match="catalog removal was not confirmed") as caught:
        remove_logical_catalog_records(room.context, identities, descriptors=descriptors)
    assert caught.value.result["publication_state"] == "rejected"
    assert not any(operation == "delete" for operation, _ in room.store.writes)
    objects = set(room.store.objects)
    report = reference.reconcile_references(room.context, descriptors)
    assert set(report["reachable_objects"]) == objects
    assert report["unreachable_candidates"] == []

    # A refreshed explicit retry sees the completed reference, closes it and
    # carries its authenticated candidates through normal catalog removal.
    current = reference._fresh(room.context)
    removal = remove_logical_catalog_records(current, identities, descriptors=descriptors)
    assert {item["key"] for item in removal.agent_reference_receipts[0]["objects"]} == objects
    removed = dataclasses.replace(current, catalog=Catalog.empty("archive", DOMAIN))
    result = reference.complete_removed_references(removed, removal.agent_reference_receipts, descriptors={})
    assert set(result["removed_objects"]) == objects
    assert result["pending_objects"] == []
    assert room.store.objects == {}


def test_capture_catalog_fence_preserves_catalog_contents_and_revision(room):
    before = room.context.catalog.body
    etag = room.store.catalog_etag
    capture(room)
    assert room.store.catalog_etag != etag
    with reference._cipher_file(room.context, room.store.catalog_body) as source:
        catalog = json.loads(native._decrypt_to_memory(source, [room.identities[0]]))
    assert catalog == before
    assert reference._pending_objects(room.context) == (set(), [])


@pytest.mark.parametrize("fault", ["rejected", "lost-response", "cancelled", "interrupted"])
def test_catalog_publication_fence_fault_keeps_known_native_candidates(room, monkeypatch, fault):
    conditional_put = room.store.conditional_catalog_put

    def failed_fence(body, expected_etag):
        if fault == "rejected":
            raise R2Conflict("synthetic deletion won")
        conditional_put(body, expected_etag)
        if fault == "lost-response":
            raise R2PublicationError("synthetic lost fence response", published=True)
        if fault == "cancelled":
            raise CLICancelled
        raise KeyboardInterrupt

    monkeypatch.setattr(room.store, "conditional_catalog_put", failed_fence)
    with pytest.raises(reference.NativeReferenceError, match="publication-uncertain") as caught:
        capture(room)
    assert caught.value.committed is True
    assert caught.value.sidecar_published is True
    removed = dataclasses.replace(room.context, catalog=Catalog.empty("archive", DOMAIN))
    report = reference.reconcile_references(removed, {})
    assert report["unreachable_candidates"] == sorted(room.store.objects)
    assert report["pending_receipts"]
    assert not any(operation == "delete" for operation, _ in room.store.writes)


def test_removal_wins_before_first_capture_fence_and_candidates_stay_tracked(room, monkeypatch):
    from josh_room.room_store_lifecycle import remove_logical_catalog_records

    conditional_put = room.store.conditional_catalog_put
    removal = None
    raced = False

    def delete_before_fence(body, expected_etag):
        nonlocal raced, removal
        if not raced:
            raced = True
            removal = remove_logical_catalog_records(room.context, {LogicalJatIdentity("demo", "history-first")},
                descriptors={("demo", "history-first"): room.context.selected_descriptor})
        return conditional_put(body, expected_etag)

    monkeypatch.setattr(room.store, "conditional_catalog_put", delete_before_fence)
    with pytest.raises(reference.NativeReferenceError, match="publication-uncertain") as caught:
        capture(room)
    assert caught.value.committed is True and caught.value.sidecar_published is True
    assert removal.agent_reference_receipts[0]["objects"]
    removed = dataclasses.replace(room.context, catalog=Catalog.empty("archive", DOMAIN))
    report = reference.reconcile_references(removed, {})
    objects = set(room.store.objects)
    assert set(report["unreachable_candidates"]) == objects
    assert report["pending_receipts"]
    cleanup = reference.complete_removed_references(removed, removal.agent_reference_receipts, descriptors={})
    assert set(cleanup["removed_objects"]) == objects


@pytest.mark.parametrize("fault", ["rejected", "lost-response", "cancelled"])
def test_copy_fence_fault_keeps_destination_candidates(room, monkeypatch, fault):
    capture(room)
    body = descriptor("copy", "copy-point", room.scan)
    catalog = add_record(room.context.catalog, body, room.scan)
    copied = dataclasses.replace(room.context, catalog=catalog, project_id="copy",
        selected_record=catalog.resolve_snapshot("copy", "copy-point"), selected_descriptor=body)
    room.contexts[("copy", "copy-point")] = copied
    room.configure(copied, "copy-profile")
    conditional_put = room.store.conditional_catalog_put

    def failed_fence(data, expected_etag):
        if fault == "rejected":
            raise R2Conflict("synthetic deletion won")
        conditional_put(data, expected_etag)
        if fault == "lost-response":
            raise R2PublicationError("synthetic lost copy response", published=True)
        raise CLICancelled

    monkeypatch.setattr(room.store, "conditional_catalog_put", failed_fence)
    with pytest.raises(native.NativeAgentError, match="copy-uncertain") as caught:
        reference.copy_reference(room.context, copied, "copy", "copy-point")
    assert caught.value.committed is True
    pending, receipts = reference._pending_objects(copied)
    assert pending == {entry["object"]["key"] for entry in documents(room, copied)["entries"]}
    assert receipts
    assert pending.isdisjoint(entry["object"]["key"] for entry in documents(room)["entries"])


def test_candidate_delete_failure_remains_pending_and_does_not_claim_removed(room):
    capture(room)
    receipts = reference.close_for_records(room.context, {LogicalJatIdentity("demo", "history-first")},
        {("demo", "history-first"): room.context.selected_descriptor})
    removed = dataclasses.replace(room.context, catalog=Catalog.empty("archive", DOMAIN))

    def refused(_key):
        raise OSError("synthetic unavailable storage")

    room.store.delete_object = refused
    result = reference.complete_removed_references(removed, receipts, descriptors={})
    assert result["state"] == "cleanup-pending" and result["reconciliation_required"] is True
    assert result["removed_objects"] == []
    assert result["pending_objects"] == sorted(room.store.objects)


def test_capture_refuses_ambiguous_provider_identity_before_upload(room):
    duplicate = room.native_file.with_name(room.native_file.stem + "_44444444-4444-4444-8444-444444444444.jsonl")
    duplicate.write_bytes(room.payload)
    duplicate.chmod(0o600)
    with pytest.raises(native.NativeAgentError, match="identity-ambiguous"):
        capture(room)
    assert room.store.writes == []


def test_cross_dimension_copy_uses_new_keys_and_keeps_original_native_identity(room):
    capture(room)
    original = documents(room)["entries"][0]
    keys, recipients = [], []
    for index in range(2):
        path = room.root / f"destination-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(path)], check=True, capture_output=True)
        path.chmod(0o600)
        keys.append(path)
        recipients.append(subprocess.run(["age-keygen", "-y", str(path)], check=True,
                                        capture_output=True, text=True).stdout.strip())
    new_domain = "00000000-0000-4000-8000-000000000002"
    body = descriptor("cross-copy", "cross-copy-point", room.scan).to_dict()
    body["dimension_id"] = "other-archive"
    body["encryption_domain_id"] = new_domain
    body["workspace"]["repository_id"] = "f" * 64
    destination_descriptor = LogicalJat.from_dict(body)
    catalog = add_record(Catalog.empty("other-archive", new_domain), destination_descriptor, room.scan)
    store = MemoryStore()
    material = SimpleNamespace(identity=keys[0], recipient=recipients[0], encryption_domain_id=new_domain,
        keyset=SimpleNamespace(key_generation=1, recovery_recipients=(recipients[1],)))
    destination = dataclasses.replace(room.context, backend=store, catalog=catalog, project_id="cross-copy",
        selected_record=catalog.resolve_snapshot("cross-copy", "cross-copy-point"), selected_descriptor=destination_descriptor,
        dimension=SimpleNamespace(dimension_id="other-archive"), material=material,
        repository_info=SimpleNamespace(repository_id="f" * 64), physical_binding="minio:http://127.0.0.1:19001:other")
    room.contexts[("cross-copy", "cross-copy-point")] = destination
    grant = room.configure(destination, "cross-profile")
    reference.copy_reference(room.context, destination, "cross-copy", "cross-copy-point")
    copied = documents(room, destination)
    entry = copied["entries"][0]
    assert copied["binding"]["encryption_domain_id"] == new_domain
    assert entry["origin"] == original["origin"]
    assert entry["archive_binding"] == original["archive_binding"]
    assert entry["object"]["key"] not in room.store.objects
    assert entry["object"]["key"] in store.objects
    mark(room, destination)
    result = reference.restore_reference(destination, grant=grant, tool="codex", session_id=SESSION)
    assert result["native_resume_verified"] is False
    assert (Path(grant["profile_root"]) / "codex" / room.relative).read_bytes() == room.payload


def test_copy_refuses_destination_authority_before_native_upload(room, monkeypatch):
    capture(room)
    body = descriptor("blocked-copy", "blocked-point", room.scan)
    catalog = add_record(room.context.catalog, body, room.scan)
    destination_store = MemoryStore()
    destination = dataclasses.replace(room.context, backend=destination_store, catalog=catalog, project_id="blocked-copy",
        selected_record=catalog.resolve_snapshot("blocked-copy", "blocked-point"), selected_descriptor=body)
    original = reference._grant

    def admitted(selected, *args):
        if selected.project_id == "blocked-copy":
            raise native.NativeAgentError("native-admission-not-authorized")
        return original(selected, *args)

    monkeypatch.setattr(reference, "_grant", admitted)
    with pytest.raises(native.NativeAgentError, match="not-authorized"):
        reference.copy_reference(room.context, destination, "blocked-copy", "blocked-point")
    assert destination_store.writes == []


def test_capture_revocation_during_decode_prevents_remote_upload(room, monkeypatch):
    decode = native._decode

    def revoked(*args, **kwargs):
        result = decode(*args, **kwargs)
        room.receipt["authority_sha256"] = "e" * 64
        return result

    monkeypatch.setattr(native, "_decode", revoked)
    with pytest.raises(native.NativeAgentError, match="admission-changed"):
        capture(room)
    assert room.store.writes == []


@pytest.mark.parametrize("operation", ["copy", "export"])
def test_copy_or_export_revocation_after_encryption_prevents_publication(room, monkeypatch, operation):
    capture(room)
    encrypted = reference._encrypt

    def revoked(*args, **kwargs):
        result = encrypted(*args, **kwargs)
        room.receipt["authority_sha256"] = "e" * 64
        return result

    monkeypatch.setattr(reference, "_encrypt", revoked)
    if operation == "export":
        output = room.root / "revoked.haul"
        with pytest.raises(native.NativeAgentError, match="admission-changed"):
            reference.export_reference(room.context, output)
        assert not Path(str(output) + ".agents.age").exists()
    else:
        copied_descriptor = descriptor("revoked-copy", "revoked-point", room.scan)
        copied_catalog = add_record(room.context.catalog, copied_descriptor, room.scan)
        store = MemoryStore()
        copied = dataclasses.replace(room.context, backend=store, catalog=copied_catalog, project_id="revoked-copy",
            selected_record=copied_catalog.resolve_snapshot("revoked-copy", "revoked-point"), selected_descriptor=copied_descriptor)
        room.contexts[("revoked-copy", "revoked-point")] = copied
        room.configure(copied, "revoked-copy-profile")
        with pytest.raises(native.NativeAgentError, match="admission-changed"):
            reference.copy_reference(room.context, copied, "revoked-copy", "revoked-point")
        assert store.writes == []


def test_cancellation_after_sidecar_cas_preserves_committed_uncertainty(room):
    publish = room.store._publish

    def cancelled(*args):
        publish(*args)
        raise CLICancelled

    room.store._publish = cancelled
    with pytest.raises(native.NativeAgentError, match="publication-uncertain") as caught:
        capture(room)
    assert caught.value.committed is True
    assert caught.value.sidecar_published is None
    assert room.store.controls and room.store.objects
    assert room.native_file.read_bytes() == room.payload


def test_registered_cli_sigterm_after_real_sidecar_cas_reports_uncertainty(room, monkeypatch, capsys):
    import os
    import signal

    from josh_room import native_agents_cli

    publish = room.store._publish

    def terminated(*args):
        publish(*args)
        assert signal.getsignal(signal.SIGTERM) not in {signal.SIG_DFL, signal.SIG_IGN}
        os.kill(os.getpid(), signal.SIGTERM)

    room.store._publish = terminated
    monkeypatch.setattr(native_agents_cli, "dispatch", lambda args: capture(room))
    assert native_agents_cli.main(["capabilities"]) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["state"] == "commit-uncertain" and receipt["committed"] is True
    assert room.store.controls and room.store.objects
    assert room.native_file.read_bytes() == room.payload


def test_restore_cleanup_failure_preserves_materialized_history_and_uncertainty(room, monkeypatch):
    capture(room)
    cipher_file = reference._cipher_file

    @contextlib.contextmanager
    def failed_cleanup(context, data, *, directory=None):
        with cipher_file(context, data, directory=directory) as path:
            yield path
        if directory == room.grant["local_reference_root"]:
            raise OSError("synthetic encrypted staging cleanup failure")

    monkeypatch.setattr(reference, "_cipher_file", failed_cleanup)
    room.native_file.unlink()
    with pytest.raises(native.NativeAgentError, match="materialization-uncertain") as caught:
        reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION)
    assert caught.value.committed is True
    assert room.native_file.read_bytes() == room.payload


@pytest.mark.parametrize("operation", ["capture", "copy", "close"])
def test_sidecar_encryption_revocation_is_checked_before_actual_control_cas(room, monkeypatch, operation):
    copied = None
    if operation != "capture":
        capture(room)
    if operation == "copy":
        body = descriptor("fenced-copy", "fenced-point", room.scan)
        catalog = add_record(room.context.catalog, body, room.scan)
        copied = dataclasses.replace(room.context, catalog=catalog, project_id="fenced-copy",
            selected_record=catalog.resolve_snapshot("fenced-copy", "fenced-point"), selected_descriptor=body)
        room.contexts[("fenced-copy", "fenced-point")] = copied
        room.configure(copied, "fenced-copy-profile")
    controls = room.store.controls.copy()
    encrypt = reference._encrypt

    def revoked(data, recipients):
        result = encrypt(data, recipients)
        if json.loads(data)["format"] == reference.FORMAT:
            room.receipt["authority_sha256"] = "f" * 64
        return result

    monkeypatch.setattr(reference, "_encrypt", revoked)
    with pytest.raises(native.NativeAgentError) as caught:
        if operation == "capture":
            capture(room)
        elif operation == "copy":
            reference.copy_reference(room.context, copied, "fenced-copy", "fenced-point")
        else:
            reference.close_for_records(room.context, [LogicalJatIdentity("demo", "history-first")],
                {("demo", "history-first"): room.context.selected_descriptor})
    assert caught.value.committed is True
    assert room.store.controls == controls


def test_history_restore_accepts_clean_hydrated_stat_baseline_but_capture_does_not(room):
    import os

    capture(room)
    room.native_file.unlink()
    historical = next(path for path in room.workspace.iterdir() if path.is_file() and path.name != ".josh-room.json")
    before = historical.read_bytes()
    info = historical.stat()
    os.utime(historical, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    hydrated = scan_workspace_for_status(room.workspace)
    assert hydrated.signature != room.scan.signature
    write_stat_workspace_marker(room.workspace, dimension_id=room.context.dimension.dimension_id,
        project_id="demo", snapshot_id="history-first", encryption_domain_id=room.context.material.encryption_domain_id,
        display_name="Synthetic Room", workspace_signature=hydrated.signature,
        signature_algorithm=hydrated.signature_algorithm, capture_policy_sha256=hydrated.capture_policy_sha256)
    restored = reference.restore_reference(room.context, grant=room.grant, tool="codex", session_id=SESSION)
    assert restored["state"] == "history-materialized" and restored["native_resume_verified"] is False
    assert room.native_file.read_bytes() == room.payload and historical.read_bytes() == before
    with pytest.raises(native.NativeAgentError, match="workspace-binding-mismatch"):
        capture(room)
