import base64
import copy
import hashlib
import json
import shutil
import stat
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path

import pytest

import josh_room.pcc_crypto as pcc_crypto_module
from josh_room.adapter_contract import (
    BoundedRecordStream,
    Checkpoint,
    LogicalSourceName,
    SourceRecord,
    StreamLimits,
)
from josh_room.pcc_crypto import (
    CiphertextReceipt,
    CryptoError,
    CryptoErrorCode,
    RecipientSet,
    ResolvedRecipients,
    build_manifest,
    decrypt_envelope,
    encrypt_and_prepare,
    read_decrypted_envelope,
    resolve_recipients,
    stream_encrypt,
    validate_manifest,
)
from josh_room.pcc_outbox import PccOutbox, QueueState
from josh_room.policy import CaptureProfile, Destination, Limits
from josh_room.session_evidence import canonical_json
from josh_room.session_normalizer import (
    AssetReceipt,
    NormalizationContext,
    NormalizationEvent,
    SessionNormalizer,
)


@pytest.fixture(autouse=True)
def _local_only_contract(monkeypatch):
    monkeypatch.setattr("josh_room.device.require_prepare_upload", lambda: None)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "session_evidence"
DAILY_RECIPIENT = "age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq"
RECOVERY_RECIPIENT = "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"
EXTRA_RECIPIENT = "age1qvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcrqvpsewmjt2"
SSH_RECIPIENT = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA "
    "synthetic-key"
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _profile(*, destination: str = "private-r2") -> CaptureProfile:
    return CaptureProfile(
        name="personal",
        profile_id="profile-personal",
        workspace_id="workspace-synthetic",
        allowed_sources=frozenset({"codex.transcript"}),
        capture_mode="transcript-and-assets",
        triggers=frozenset({"manual"}),
        destination=Destination(destination, "binding-synthetic" if destination == "private-r2" else None),
        recipient_set_ref="recipients-personal",
        limits=Limits(100_000, 100_000, 1_000_000, 2_000_000, 30),
        allow_rules=(),
        deny_rules=(),
        downstream_memory=False,
        policy_version="1.0",
        provenance="private-host-config",
    )


def _segment() -> dict[str, object]:
    return _fixture("golden-session-segment.json")


def _recipient_set(**overrides: object) -> RecipientSet:
    values: dict[str, object] = {
        "reference": "recipients-personal",
        "version": 1,
        "daily_use": (DAILY_RECIPIENT,),
        "recovery": (RECOVERY_RECIPIENT,),
        "additional": (),
    }
    values.update(overrides)
    return RecipientSet(**values)


def test_recipient_fingerprint_is_order_independent_and_contains_only_public_set_data():
    profile = _profile()
    first = resolve_recipients(profile, lambda _: _recipient_set(additional=(EXTRA_RECIPIENT,)))
    second = resolve_recipients(
        profile,
        lambda _: _recipient_set(
            daily_use=(DAILY_RECIPIENT,),
            recovery=(RECOVERY_RECIPIENT,),
            additional=(EXTRA_RECIPIENT,),
        ),
    )

    assert first.fingerprint == second.fingerprint
    assert first.ordered == tuple(sorted((DAILY_RECIPIENT, EXTRA_RECIPIENT, RECOVERY_RECIPIENT)))
    assert len(first.fingerprint) == 64
    assert "AGE-SECRET-KEY" not in first.fingerprint


def test_manifest_rejects_forged_resolved_recipient_fingerprint():
    profile = _profile()
    resolved = resolve_recipients(profile, lambda _: _recipient_set())
    forged = ResolvedRecipients(
        resolved.reference,
        resolved.version,
        resolved.ordered,
        "0" * 64,
        resolved.daily_use,
        resolved.recovery,
        resolved.additional,
    )
    with pytest.raises(CryptoError) as error:
        build_manifest(_segment(), profile, forged, "a" * 64, 123)
    assert error.value.code is CryptoErrorCode.RECIPIENT_INVALID


def test_private_production_profile_requires_daily_use_and_independent_recovery():
    profile = _profile()
    resolved = resolve_recipients(profile, lambda _: _recipient_set())
    assert resolved.daily_use == (DAILY_RECIPIENT,)
    assert resolved.recovery == (RECOVERY_RECIPIENT,)

    for overrides, code in [
        ({"daily_use": ()}, CryptoErrorCode.RECIPIENT_DAILY_MISSING),
        ({"recovery": ()}, CryptoErrorCode.RECIPIENT_RECOVERY_MISSING),
        ({"recovery": (DAILY_RECIPIENT,)}, CryptoErrorCode.RECIPIENT_DUPLICATE),
    ]:
        with pytest.raises(CryptoError) as error:
            resolve_recipients(profile, lambda _, overrides=overrides: _recipient_set(**overrides))
        assert error.value.code is code


def test_local_only_profile_accepts_one_valid_recipient_without_recovery_role():
    profile = _profile(destination="local-only")
    resolved = resolve_recipients(
        profile,
        lambda _: _recipient_set(daily_use=(DAILY_RECIPIENT,), recovery=()),
    )
    assert resolved.ordered == (DAILY_RECIPIENT,)


def test_supported_ssh_recipient_is_canonicalized_without_becoming_a_native_age_key():
    profile = _profile()
    resolved = resolve_recipients(
        profile,
        lambda _: _recipient_set(daily_use=(SSH_RECIPIENT,)),
    )

    canonical = SSH_RECIPIENT.rsplit(" ", 1)[0]
    assert resolved.daily_use == (canonical,)
    assert canonical in resolved.ordered
    assert "synthetic-key" not in resolved.ordered
    assert resolved.fingerprint == resolve_recipients(
        profile,
        lambda _: _recipient_set(daily_use=(canonical,)),
    ).fingerprint


@pytest.mark.parametrize(
    "recipient",
    ["", "age1 has-space", "AGE-SECRET-KEY-1SYNTHETIC", "age1\x00synthetic"],
)
def test_malformed_or_private_recipient_is_rejected_without_echoing_value(recipient: str):
    with pytest.raises(CryptoError) as error:
        resolve_recipients(_profile(destination="local-only"), lambda _: _recipient_set(daily_use=(recipient,), recovery=()))
    assert error.value.code is CryptoErrorCode.RECIPIENT_INVALID
    if recipient:
        assert recipient not in str(error.value)


def test_duplicate_recipient_within_or_across_roles_is_rejected():
    for recipient_set in (
        _recipient_set(daily_use=(DAILY_RECIPIENT, DAILY_RECIPIENT)),
        _recipient_set(additional=(DAILY_RECIPIENT,)),
    ):
        with pytest.raises(CryptoError) as error:
            resolve_recipients(_profile(), lambda _, recipient_set=recipient_set: recipient_set)
        assert error.value.code is CryptoErrorCode.RECIPIENT_DUPLICATE


def test_resolver_reference_mismatch_fails_closed_without_recipient_details():
    with pytest.raises(CryptoError) as error:
        resolve_recipients(_profile(), lambda _: _recipient_set(reference="other-set"))
    assert error.value.code is CryptoErrorCode.RECIPIENT_REFERENCE_MISMATCH
    assert "other-set" not in str(error.value)


def test_manifest_binds_document_identity_profile_policy_and_payload():
    document = _segment()
    profile = _profile()
    recipients = resolve_recipients(profile, lambda _: _recipient_set())
    manifest = build_manifest(document, profile, recipients, "a" * 64, 123)

    assert manifest["format"] == "josh-room.pcc-evidence"
    assert manifest["format_version"] == 1
    assert manifest["schema"] == {"name": "codex-session-evidence", "major": 1, "minor": 0}
    assert manifest["kind"] == "session-segment"
    assert manifest["event_id"] == document["event_id"]
    assert manifest["payload"] == {"sha256": "a" * 64, "size": 123}
    assert manifest["profile"] == {"id": "profile-personal", "workspace_id": "workspace-synthetic"}
    assert manifest["recipient_set"]["fingerprint"] == recipients.fingerprint
    validate_manifest(manifest, document, "a" * 64, 123)


def test_manifest_rejects_event_profile_mismatch_before_encryption():
    document = _segment()
    document["profile_id"] = "profile-work"
    profile = _profile()
    recipients = resolve_recipients(profile, lambda _: _recipient_set())

    with pytest.raises(CryptoError) as error:
        build_manifest(document, profile, recipients, "a" * 64, 123)
    assert error.value.code is CryptoErrorCode.PROFILE_MISMATCH


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(kind="session-final"), CryptoErrorCode.MANIFEST_MISMATCH),
        (lambda value: value["schema"].update(major=2), CryptoErrorCode.UNKNOWN_SCHEMA),
        (lambda value: value["references"].update(previous_segment_sha256="b" * 64), CryptoErrorCode.MANIFEST_MISMATCH),
        (lambda value: value["payload"].update(sha256="b" * 64), CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH),
        (lambda value: value["payload"].update(size=999), CryptoErrorCode.PAYLOAD_SIZE_MISMATCH),
    ],
)
def test_manifest_validation_rejects_kind_schema_reference_digest_and_size_tampering(mutation, code):
    document = _segment()
    profile = _profile()
    recipients = resolve_recipients(profile, lambda _: _recipient_set())
    manifest = build_manifest(document, profile, recipients, "a" * 64, 123)
    mutation(copy.deepcopy(manifest))
    tampered = copy.deepcopy(manifest)
    mutation(tampered)

    with pytest.raises(CryptoError) as error:
        validate_manifest(tampered, document, "a" * 64, 123)
    assert error.value.code is code


def test_manifest_validation_rejects_unknown_document_major_and_wrong_document_kind():
    document = _segment()
    profile = _profile()
    recipients = resolve_recipients(profile, lambda _: _recipient_set())
    manifest = build_manifest(document, profile, recipients, "a" * 64, 123)

    unknown_major = copy.deepcopy(document)
    unknown_major["schema_version"] = {"major": 2, "minor": 0}
    with pytest.raises(CryptoError) as error:
        validate_manifest(manifest, unknown_major, "a" * 64, 123)
    assert error.value.code is CryptoErrorCode.UNKNOWN_SCHEMA

    wrong_kind = copy.deepcopy(document)
    wrong_kind["kind"] = "session-final"
    with pytest.raises(CryptoError) as error:
        validate_manifest(manifest, wrong_kind, "a" * 64, 123)
    assert error.value.code is CryptoErrorCode.MANIFEST_MISMATCH


def _event(kind: str) -> NormalizationEvent:
    if kind == "session-segment":
        return NormalizationEvent(kind, _segment())
    if kind == "session-final":
        return NormalizationEvent(kind, _fixture("golden-session-final.json"))
    if kind == "index-event":
        return NormalizationEvent(kind, _fixture("golden-index-event.json"))
    document = _fixture("golden-session-asset.json")
    document.update({"profile_id": "profile-personal", "workspace_id": "workspace-synthetic"})
    payload = b"synthetic asset payload\n"
    document["sha256"] = __import__("hashlib").sha256(payload).hexdigest()
    document["size"] = len(payload)
    return NormalizationEvent(
        kind,
        document,
        AssetReceipt(document["asset_id"], document["sha256"], len(payload), document["media_category"], document["content_type"]),
    )


def _real_age_recipients(tmp_path: Path) -> tuple[tuple[Path, Path], ResolvedRecipients]:
    if shutil.which("age") is None or shutil.which("age-keygen") is None:
        pytest.skip("managed age and age-keygen are unavailable")
    identities = []
    recipients = []
    for name in ("daily", "recovery"):
        identity = tmp_path / f"{name}.agekey"
        subprocess.run(["age-keygen", "-o", str(identity)], capture_output=True, check=True)
        identity.chmod(0o600)
        identities.append(identity)
        recipients.append(next(line.removeprefix("# public key: ") for line in identity.read_text().splitlines() if line.startswith("# public key: ")))
    profile = _profile()
    resolved = resolve_recipients(profile, lambda _: _recipient_set(daily_use=(recipients[0],), recovery=(recipients[1],)))
    return (identities[0], identities[1]), resolved


@pytest.mark.parametrize("kind", ["session-segment", "session-asset", "session-final", "index-event"])
def test_streaming_age_round_trip_covers_all_typed_event_kinds(tmp_path: Path, kind: str):
    (daily, recovery), recipients = _real_age_recipients(tmp_path)
    event = _event(kind)
    output = tmp_path / f"{kind}.age"
    payload = [b"synthetic ", b"asset payload\n"] if kind == "session-asset" else None
    receipt = stream_encrypt(event, recipients, output, profile=_profile(), payload=payload)

    assert isinstance(receipt, CiphertextReceipt)
    assert receipt.path == output
    assert receipt.size == output.stat().st_size
    assert receipt.sha256 == __import__("hashlib").sha256(output.read_bytes()).hexdigest()
    assert event.document["event_id"].encode() not in output.read_bytes()
    for identity in (daily, recovery):
        decrypted = decrypt_envelope(output, [identity])
        assert decrypted.document == event.document
        if kind == "session-asset":
            assert decrypted.payload == b"synthetic asset payload\n"
        else:
            assert decrypted.payload == canonical_json(event.document)


def test_randomized_age_ciphertext_is_different_for_same_immutable_object(tmp_path: Path):
    (daily, recovery), recipients = _real_age_recipients(tmp_path)
    del daily, recovery
    event = _event("session-final")
    first = tmp_path / "first.age"
    second = tmp_path / "second.age"
    stream_encrypt(event, recipients, first, profile=_profile())
    stream_encrypt(event, recipients, second, profile=_profile())
    assert first.read_bytes() != second.read_bytes()


def test_real_age_tampering_truncation_and_unrelated_identity_fail_closed(tmp_path: Path):
    (daily, recovery), recipients = _real_age_recipients(tmp_path)
    del recovery
    event = _event("session-final")
    output = tmp_path / "final.age"
    stream_encrypt(event, recipients, output, profile=_profile())

    corrupted = tmp_path / "corrupted.age"
    corrupted_bytes = bytearray(output.read_bytes())
    corrupted_bytes[-1] ^= 1
    corrupted.write_bytes(corrupted_bytes)
    with pytest.raises(CryptoError):
        decrypt_envelope(corrupted, [daily])

    truncated = tmp_path / "truncated.age"
    truncated.write_bytes(output.read_bytes()[:-1])
    with pytest.raises(CryptoError):
        decrypt_envelope(truncated, [daily])

    unrelated = tmp_path / "unrelated.agekey"
    subprocess.run(["age-keygen", "-o", str(unrelated)], capture_output=True, check=True)
    unrelated.chmod(0o600)
    with pytest.raises(CryptoError):
        decrypt_envelope(output, [unrelated])


def test_asset_stream_digest_and_size_mismatch_fails_and_cleans_output(tmp_path: Path):
    event = _event("session-asset")
    output = tmp_path / "asset.age"
    recipients = resolve_recipients(_profile(), lambda _: _recipient_set())
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(CryptoError) as error:
        stream_encrypt(event, recipients, output, payload=[b"x" * event.document["size"]], age_executable=passthrough)
    assert error.value.code is CryptoErrorCode.PAYLOAD_DIGEST_MISMATCH
    assert not output.exists()
    assert not list(tmp_path.glob(".*"))


def test_asset_bytes_are_one_stream_chunk_for_file_backed_encryption(tmp_path: Path):
    event = _event("session-asset")
    output = tmp_path / "asset.age"
    recipients = resolve_recipients(_profile(), lambda _: _recipient_set())
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)

    stream_encrypt(
        event,
        recipients,
        output,
        profile=_profile(),
        payload=b"synthetic asset payload\n",
        age_executable=passthrough,
    )
    assert read_decrypted_envelope(output).document == event.document


def test_age_failure_cancellation_and_cleanup_are_fail_closed(tmp_path: Path):
    fake_age = tmp_path / "fake-age"
    fake_age.write_text("#!/bin/sh\nexit 17\n")
    fake_age.chmod(fake_age.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "failed.age"
    event = _event("session-final")
    recipients = resolve_recipients(_profile(), lambda _: _recipient_set())
    with pytest.raises(CryptoError) as error:
        stream_encrypt(event, recipients, output, age_executable=fake_age)
    assert error.value.code is CryptoErrorCode.AGE_FAILED
    assert not output.exists()
    assert not list(tmp_path.glob(".*"))

    with pytest.raises(CryptoError) as error:
        stream_encrypt(event, recipients, output, cancel_check=lambda: True, age_executable=fake_age)
    assert error.value.code is CryptoErrorCode.CANCELLED
    assert not output.exists()


def test_directory_fsync_failure_is_not_acknowledged_as_success(tmp_path: Path, monkeypatch):
    event = _event("session-final")
    recipients = resolve_recipients(_profile(), lambda _: _recipient_set())
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)
    output = tmp_path / "final.age"

    def fail_fsync(_descriptor):
        raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(pcc_crypto_module.os, "fsync", fail_fsync)
    with pytest.raises(CryptoError) as error:
        stream_encrypt(event, recipients, output, profile=_profile(), age_executable=passthrough)

    assert error.value.code is CryptoErrorCode.OUTPUT_FAILED
    assert not list(tmp_path.glob(".*.age.*"))


def _plain_envelope(manifest: dict[str, object], payload: bytes, *, name: str = "payload.json") -> bytes:
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for member_name, body in (("manifest.json", canonical_json(manifest)), (name, payload)):
            info = tarfile.TarInfo(member_name)
            info.size = len(body)
            info.mtime = 0
            archive.addfile(info, BytesIO(body))
    return stream.getvalue()


def test_plaintext_reader_rejects_tampered_manifest_truncation_and_unknown_members():
    event = _event("session-final")
    profile = _profile()
    recipients = resolve_recipients(profile, lambda _: _recipient_set())
    payload = canonical_json(event.document)
    manifest = build_manifest(event.document, profile, recipients, __import__("hashlib").sha256(payload).hexdigest(), len(payload))
    valid = _plain_envelope(manifest, payload)
    read = read_decrypted_envelope(valid)
    assert read.document == event.document

    tampered = copy.deepcopy(manifest)
    tampered["kind"] = "session-asset"
    with pytest.raises(CryptoError) as error:
        read_decrypted_envelope(_plain_envelope(tampered, payload))
    assert error.value.code is CryptoErrorCode.MANIFEST_MISMATCH

    with pytest.raises(CryptoError):
        read_decrypted_envelope(valid[:100])
    with pytest.raises(CryptoError):
        read_decrypted_envelope(_plain_envelope(manifest, payload, name="unexpected.bin"))


def _queue_checkpoint(event_number: int = 1) -> dict[str, object]:
    return {
        "source": "codex.transcript",
        "representation": "active-jsonl",
        "start": event_number - 1,
        "end": event_number,
        "prefix_sha256": f"{event_number:064x}",
    }


def _prepare_queue(outbox: PccOutbox, event: NormalizationEvent, number: int = 1) -> None:
    source = event.document.get("source")
    source_surface = source.get("surface", "cli") if isinstance(source, dict) else "cli"
    source_adapter = source.get("adapter", "codex.transcript") if isinstance(source, dict) else "codex.transcript"
    source_adapter_version = source.get("adapter_version", "1") if isinstance(source, dict) else "1"
    outbox.enqueue(
        event_id=event.document["event_id"],
        session_id=event.document.get("session_id", "session-synthetic"),
        checkpoint=event.document.get("checkpoint", _queue_checkpoint(number)),
        metadata={
            "workspace_id": "workspace-synthetic",
            "source_surface": source_surface,
            "source_adapter": source_adapter,
            "source_adapter_version": source_adapter_version,
            "object_kind": event.kind,
            "destination_class": "private-r2",
            "destination_binding_id": "binding-synthetic",
            "policy_decision": "allow",
            "capture_status": "complete",
            "sensitivity": "normal",
        },
    )
    outbox.claim("worker-one")
    outbox.transition(event.document["event_id"], "worker-one", QueueState.SOURCE_SNAPSHOTTED)


def test_exact_normalization_event_to_stream_to_prepared_outbox_handoff(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox")
    profile = _profile()
    recipients = _recipient_set()
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)
    event = _event("session-segment")
    _prepare_queue(outbox, event)

    prepared = encrypt_and_prepare(
        event,
        outbox,
        "worker-one",
        profile,
        lambda _: recipients,
        age_executable=passthrough,
    )
    assert prepared.event_id == event.document["event_id"]
    assert prepared.kind == "session-segment"
    queue_record = outbox.inspect_record(event.document["event_id"])
    assert queue_record.state is QueueState.PREPARED_ENCRYPTED
    assert not (outbox.prepared.directory / f".{event.document['event_id']}.age.pending").exists()
    prepared_path = outbox.prepared.directory / f"{event.document['event_id']}.age"
    assert read_decrypted_envelope(prepared_path).document == event.document
    public_state = (outbox.prepared.directory / f"{event.document['event_id']}.json").read_text()
    assert "content_sha256" not in public_state
    assert "profile-personal" not in public_state
    assert "passthrough-age" not in public_state


def test_index_event_uses_same_file_backed_handoff_and_asset_requires_payload(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox")
    profile = _profile()
    recipients = _recipient_set()
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)
    index = _event("index-event")
    _prepare_queue(outbox, index)
    encrypt_and_prepare(index, outbox, "worker-one", profile, lambda _: recipients, age_executable=passthrough)
    assert outbox.inspect_record(index.document["event_id"]).state is QueueState.PREPARED_ENCRYPTED

    asset = _event("session-asset")
    _prepare_queue(outbox, asset, number=2)
    with pytest.raises(CryptoError) as error:
        encrypt_and_prepare(asset, outbox, "worker-one", profile, lambda _: recipients, age_executable=passthrough)
    assert error.value.code is CryptoErrorCode.ASSET_PAYLOAD_MISSING
    assert not (outbox.prepared.directory / f"{asset.document['event_id']}.age").exists()


def test_event_cannot_redirect_host_profile_or_recipient_reference(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox")
    event = _event("session-final")
    event.document["profile_id"] = "profile-work"
    _prepare_queue(outbox, event)
    with pytest.raises(CryptoError) as error:
        encrypt_and_prepare(event, outbox, "worker-one", _profile(), lambda _: _recipient_set())
    assert error.value.code is CryptoErrorCode.PROFILE_MISMATCH
    assert not list(outbox.prepared.directory.glob("*.age"))


def test_capture_gap_preserves_ciphertext_without_prepared_plaintext(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox", max_bytes=100_000)
    event = _event("session-final")
    _prepare_queue(outbox, event)
    queue_path = outbox.queue_directory / f"{event.document['event_id']}.json"
    outbox.max_bytes = queue_path.stat().st_size + 1
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(CryptoError) as error:
        encrypt_and_prepare(
            event,
            outbox,
            "worker-one",
            _profile(),
            lambda _: _recipient_set(),
            age_executable=passthrough,
        )
    assert error.value.code is CryptoErrorCode.CAPTURE_GAP
    record = outbox.inspect_record(event.document["event_id"])
    assert record is not None
    assert record.state is QueueState.CAPTURE_GAP
    assert not list(outbox.prepared.directory.glob("*.age"))
    pending = list(outbox.quarantine_directory.glob("capture-gap-*.age"))
    assert len(pending) == 1
    assert pending[0].stat().st_mode & 0o077 == 0
    assert not list(outbox.root.glob(".*.age.pending.*"))


class _IntegrationAssetSink:
    def __init__(self, writer: "_IntegrationAssetWriter", asset_id: str, content_type: str, media_category: str):
        self.writer = writer
        self.asset_id = asset_id
        self.content_type = content_type
        self.media_category = media_category
        self.body = bytearray()
        self.aborted = False

    def write(self, chunk: bytes) -> None:
        if self.aborted:
            raise RuntimeError("aborted sink")
        self.body.extend(chunk)

    def commit(self) -> AssetReceipt:
        body = bytes(self.body)
        digest = hashlib.sha256(body).hexdigest()
        self.writer.payloads[digest] = body
        return AssetReceipt(self.asset_id, digest, len(body), self.media_category, self.content_type)

    def abort(self) -> None:
        self.aborted = True
        self.body.clear()


class _IntegrationAssetWriter:
    def __init__(self):
        self.payloads: dict[str, bytes] = {}

    def open(self, asset_id: str, content_type: str, media_category: str) -> _IntegrationAssetSink:
        return _IntegrationAssetSink(self, asset_id, content_type, media_category)


def test_production_normalizer_asset_dedupe_to_real_age_prepared_outbox(tmp_path: Path):
    (daily, recovery), recipients = _real_age_recipients(tmp_path)
    del recovery
    session_id = "session-crypto-integration"
    source_id = "source-crypto-integration"
    asset_body = b"repeated synthetic asset payload"
    encoded_asset = base64.b64encode(asset_body).decode("ascii")
    values = [
        {"record_kind": "message", "role": "user", "text": "synthetic text"},
        {
            "record_kind": "asset_payload",
            "asset_id": "source-asset-one",
            "chunk": encoded_asset,
            "chunk_index": 0,
            "final": True,
            "content_type": "image/png",
            "media_category": "image",
        },
        {
            "record_kind": "asset_payload",
            "asset_id": "source-asset-two",
            "chunk": encoded_asset,
            "chunk_index": 0,
            "final": True,
            "content_type": "image/png",
            "media_category": "image",
        },
    ]
    records = [
        SourceRecord(
            LogicalSourceName.TRANSCRIPT,
            session_id,
            index,
            json.dumps(value, separators=(",", ":")).encode(),
            "asset" if value["record_kind"] == "asset_payload" else "transcript",
        )
        for index, value in enumerate(values)
    ]
    observed_size = sum(record.byte_size for record in records)

    def checkpoint_factory(count: int, byte_count: int) -> Checkpoint:
        return Checkpoint(
            logical_source=LogicalSourceName.TRANSCRIPT,
            session_id=session_id,
            source_id=source_id,
            representation="active-jsonl",
            next_record_index=count,
            next_byte_offset=byte_count,
            observed_size=observed_size,
            prefix_digest=hashlib.sha256(f"prefix:{count}:{byte_count}".encode()).hexdigest(),
        )

    stream = BoundedRecordStream(
        records,
        checkpoint_factory,
        StreamLimits(2 * 1024 * 1024, 2 * 1024 * 1024, 16, observed_size + 1),
    )
    writer = _IntegrationAssetWriter()
    normalizer = SessionNormalizer(
        stream,
        NormalizationContext(
            session_id=session_id,
            profile_id="profile-personal",
            workspace_id="workspace-synthetic",
            device_id="device-synthetic",
            source={"surface": "cli", "adapter": "codex.transcript", "adapter_version": "1"},
        ),
        asset_writer=writer,
        finalize=True,
    )
    events = list(normalizer.normalize())
    assert [event.kind for event in events] == ["session-asset", "session-segment", "session-final"]
    assert normalizer.metrics.assets_externalized == 1
    assert normalizer.metrics.assets_deduped == 1

    outbox = PccOutbox(tmp_path / "outbox")
    profile = _profile()
    for number, event in enumerate(events, 1):
        _prepare_queue(outbox, event, number)
        asset_payload = writer.payloads[event.document["sha256"]] if event.kind == "session-asset" else None
        encrypt_and_prepare(
            event,
            outbox,
            "worker-one",
            profile,
            lambda _: RecipientSet(
                "recipients-personal", 1, (recipients.daily_use[0],), (recipients.recovery[0],), ()
            ),
            asset_payload=asset_payload,
        )
        prepared_path = outbox.prepared.directory / f"{event.document['event_id']}.age"
        assert decrypt_envelope(prepared_path, [daily]).document == event.document

    state = "".join(path.read_text() for path in outbox.prepared.directory.glob("*.json"))
    assert asset_body.decode() not in state
    assert str(tmp_path) not in state

def test_encrypt_prepare_rejects_queue_document_identity_mismatch(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox")
    event = _event("session-segment")
    _prepare_queue(outbox, event)
    profile = _profile()
    passthrough = tmp_path / "passthrough-age"
    passthrough.write_text("#!/bin/sh\ncat\n")
    passthrough.chmod(passthrough.stat().st_mode | stat.S_IXUSR)

    mismatches = []
    session_changed = copy.deepcopy(event.document)
    session_changed["session_id"] = "other-session"
    mismatches.append(session_changed)
    checkpoint_changed = copy.deepcopy(event.document)
    checkpoint_changed["checkpoint"] = dict(checkpoint_changed["checkpoint"])
    checkpoint_changed["checkpoint"]["end"] += 1
    mismatches.append(checkpoint_changed)
    kind_changed = copy.deepcopy(event.document)
    kind_changed["kind"] = "session-final"
    mismatches.append(kind_changed)

    for document in mismatches:
        candidate = NormalizationEvent("session-segment", document)
        with pytest.raises(CryptoError) as error:
            encrypt_and_prepare(
                candidate,
                outbox,
                "worker-one",
                profile,
                lambda _: _recipient_set(),
                age_executable=passthrough,
            )
        assert error.value.code in {CryptoErrorCode.OUTBOX_PRECONDITION, CryptoErrorCode.MANIFEST_MISMATCH}


def test_encrypt_prepare_rejects_queue_workspace_metadata_mismatch(tmp_path: Path):
    outbox = PccOutbox(tmp_path / "outbox")
    event = _event("session-segment")
    _prepare_queue(outbox, event)
    queue_path = outbox.queue_directory / f"{event.document['event_id']}.json"
    record = json.loads(queue_path.read_text())
    record["metadata"]["workspace_id"] = "workspace-other"
    queue_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
    with pytest.raises(CryptoError) as error:
        encrypt_and_prepare(
            event,
            outbox,
            "worker-one",
            _profile(),
            lambda _: _recipient_set(),
        )
    assert error.value.code is CryptoErrorCode.OUTBOX_PRECONDITION
