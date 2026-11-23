"""Catalog destination corroboration through the headless CLI, with synthetic storage."""

import json
import time
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from synthetic_identity import synthetic_identity
from test_minio_encryption_flow import (
    DOMAIN_ID,
    OPERATIONAL_RECIPIENT,
    RECOVERY_RECIPIENT,
    FakeBackend,
    dimension,
    make_keyset,
)

from josh_room import auth, cli, composition_lifecycle, crypto, room_store_bridge
from josh_room.composition_authority import dimension_binding

OTHER = "00000000-0000-4000-8000-000000000002"
NAMESPACE = "1" * 32


@pytest.fixture
def command(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    (workspace / "child").mkdir(parents=True)
    (workspace / "child/file").write_text("synthetic\n")
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "private-config"))
    monkeypatch.setattr(cli, "_instance_root", lambda: tmp_path / "instance")
    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "_identity_environment", nullcontext)
    monkeypatch.setattr(auth, "store_encryption_identity", lambda *_: None)
    monkeypatch.setattr(auth, "lookup_encryption_identity", lambda *_: synthetic_identity("operational"))
    monkeypatch.setattr(crypto, "derive_recipient", lambda _: OPERATIONAL_RECIPIENT)
    monkeypatch.delenv("JOSH_ROOM_ENCRYPTION_MATERIAL", raising=False)
    monkeypatch.delenv("JOSH_ROOM_RESULT_FILE", raising=False)
    monkeypatch.setattr(room_store_bridge, "_create_backend", lambda *_: pytest.fail("opened namespace backend"))
    monkeypatch.setattr(room_store_bridge, "_restic_store_factory", lambda **_: pytest.fail("opened Restic"))

    def invoke(selected, action="capture-plan", *extra):
        monkeypatch.setattr(cli, "_effective_dimension", lambda _: selected)
        code = cli.main(["composition", action, "--dimension", selected.provider,
                         "--namespace", NAMESPACE, "--parent-room", "parent", "--workspace", str(workspace),
                         "--assign", ".=local-only", "--assign", "child=child", *extra, "--json"])
        output = capsys.readouterr().out
        assert synthetic_identity("operational") not in output
        body = json.loads(output)
        assert (code == 0) == body["ok"]
        return body

    return invoke


def native(provider, monkeypatch):
    selected = dimension(provider=provider, dimension_id=provider, region="us-east-1", connection_id="fixture")
    keyset = make_keyset(provider=provider, endpoint=selected.endpoint)
    backend = FakeBackend(control=keyset.to_json())
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_: backend)
    return selected, backend


def capture_at_publication(monkeypatch, preview):
    # Stop at the publication boundary, after the real provider_runtime has
    # constructed the namespace authority from the independently verified session.
    monkeypatch.setattr(room_store_bridge, "_create_backend", lambda *_: SimpleNamespace(client=object()))
    monkeypatch.setattr(room_store_bridge, "_verified_restic_executable", lambda **_: "unused")

    def approved_save(workspace, dimension, namespace, parent, assignments, approval, authority, factory, private):
        assert authority.binding == preview["request"]["destination_binding"]
        assert dimension.encryption_domain_id == authority.material.encryption_domain_id == DOMAIN_ID
        assert approval == preview["approval_sha256"]
        return {"ok": True}

    monkeypatch.setattr(composition_lifecycle, "save_composition", approved_save)


@pytest.mark.parametrize("provider", ["minio", "s3"])
def test_cli_corroborates_one_selected_keyset_per_plan(command, monkeypatch, provider):
    selected, backend = native(provider, monkeypatch)
    assert selected.encryption_domain_id is None
    preview = command(selected)
    assert preview["ok"], preview
    assert preview["request"]["destination_binding"] == dimension_binding(selected, DOMAIN_ID)
    assert command(selected, "capture-plan", "--expected-domain", DOMAIN_ID) == preview
    capture_at_publication(monkeypatch, preview)
    assert command(selected, "save", "--approve", preview["approval_sha256"], "--ack-local-only")["ok"]
    assert [call[0] for call in backend.calls] == ["read_control"] * 3


@pytest.mark.parametrize(("local", "asserted", "error"), [
    (OTHER, DOMAIN_ID, "encryption-domain-mismatch"),
    (DOMAIN_ID, OTHER, "encryption-domain-mismatch"),
    (None, "00000000-0000-1000-8000-000000000001", "encryption-domain-invalid"),
])
def test_cli_conflicting_or_invalid_domain_refused(command, monkeypatch, local, asserted, error):
    selected, backend = native("minio", monkeypatch)
    result = command(replace(selected, encryption_domain_id=local), "capture-plan", "--expected-domain", asserted)
    assert not result["ok"] and result["error_code"] == error
    assert all(call[0] == "read_control" for call in backend.calls)


@pytest.mark.parametrize("changed", ["domain", "connection", "bucket", "endpoint", "request"])
def test_cli_stale_approval_never_opens_namespace(command, monkeypatch, changed):
    selected, backend = native("minio", monkeypatch)
    preview = command(selected)
    flags = []
    if changed == "domain":
        backend.control = make_keyset(encryption_domain_id=OTHER).to_json()
    elif changed == "connection":
        selected = replace(selected, connection_id="other-connection")
    elif changed == "bucket":
        selected = replace(selected, bucket="other-bucket")
        backend.control = make_keyset(bucket=selected.bucket).to_json()
    elif changed == "endpoint":
        selected = replace(selected, endpoint="https://other.example.invalid")
        backend.control = make_keyset(endpoint=selected.endpoint).to_json()
    else:
        flags = ["--child-source-only", "child"]
    result = command(selected, "save", "--approve", preview["approval_sha256"], "--ack-local-only", *flags)
    assert not result["ok"] and result["error_code"] == "composition-approval-stale"
    assert [call[0] for call in backend.calls] == ["read_control", "read_control"]


@pytest.mark.parametrize(("control", "catalog", "error"), [
    (None, None, "encryption-initialization-required"),
    (None, b"older bytes", "legacy-encryption-migration-required"),
    (b"invalid", None, "encryption-keyset-invalid"),
])
def test_cli_never_initializes_or_migrates_during_plan(command, monkeypatch, control, catalog, error):
    selected, backend = native("minio", monkeypatch)
    backend.control, backend.catalog = control, catalog
    result = command(selected)
    assert not result["ok"] and result["error_code"] == error
    assert all(call[0] in {"read_control", "read_catalog"} for call in backend.calls)


@pytest.mark.parametrize("change", ["physical-binding", "missing-recovery"])
def test_s3_unsafe_keyset_never_reaches_capture(command, monkeypatch, change):
    selected, backend = native("s3", monkeypatch)
    if change == "physical-binding":
        selected = replace(selected, bucket="other-bucket")
    else:
        keyset = json.loads(backend.control)
        keyset["recovery_recipients"] = []
        backend.control = json.dumps(keyset).encode()
    result = command(selected)
    assert result["error_code"] == "encryption-keyset-invalid"
    assert [call[0] for call in backend.calls] == ["read_control"]


def test_s3_access_denied_is_sanitized_before_planning(command, monkeypatch):
    from botocore.exceptions import ClientError

    selected, backend = native("s3", monkeypatch)

    def denied(*_):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "private diagnostic"}}, "GetObject")

    monkeypatch.setattr(backend, "read_control", denied)
    result = command(selected)
    assert result["error_code"] == "access-denied"
    assert "private diagnostic" not in str(result)
    assert backend.calls == []


def test_r2_uses_existing_broker_read_for_each_command(command, tmp_path, monkeypatch):
    selected = dimension(provider="r2", dimension_id="r2", credential_profile="oauth-runtime")
    identity = tmp_path / "identity"
    config = tmp_path / "config"
    metadata = tmp_path / "metadata"
    identity.write_text(synthetic_identity("operational") + "\n")
    config.write_text(json.dumps({"r2": {"endpoint": selected.endpoint, "bucket": selected.bucket},
                                 "age_recipients": [OPERATIONAL_RECIPIENT, RECOVERY_RECIPIENT]}))
    expires = int(time.time()) + 300
    binding = "c" * 64
    metadata.write_text(json.dumps({"room_store_session_id": "a" * 64, "room_store_capability": "b" * 43,
                                    "room_store_domain_id": binding, "expires_at": expires,
                                    "room_store_capability_expires_at": expires}))
    for path in (identity, config, metadata):
        path.chmod(0o600)
    monkeypatch.setattr(cli, "load_runtime_session", lambda **_: True)
    monkeypatch.setattr(auth, "_read_runtime", lambda: ("connected", {"r2"}))
    monkeypatch.setattr(auth, "_runtime_paths", lambda: (None, identity, config, metadata))
    monkeypatch.setattr(auth, "_worker_url", lambda: "https://broker.example.invalid")
    monkeypatch.setattr(auth.keyring, "available", lambda: False)
    reads = []

    def read(authority):
        reads.append(authority)
        return SimpleNamespace(physical_binding=binding, secret="s" * 43,
                               encryption_domain_id=DOMAIN_ID, key_generation=1)

    monkeypatch.setattr(auth.R2RoomStoreAuthority, "read_material_details", read)
    monkeypatch.setattr(auth.R2RoomStoreAuthority, "ensure_material_details", lambda *_: pytest.fail("initialized R2"))
    preview = command(selected)
    assert preview["request"]["destination_binding"] == dimension_binding(selected, DOMAIN_ID)
    assert len(reads) == 1

    capture_at_publication(monkeypatch, preview)
    result = command(selected, "save", "--approve", preview["approval_sha256"], "--ack-local-only")
    assert result["ok"]
    assert len(reads) == 2
    rejected = command(replace(selected, bucket="other-bucket"))
    assert not rejected["ok"]
    assert len(reads) == 2


def test_minio_encryption_status_sanitizes_sdk_denial(tmp_path, monkeypatch, capsys):
    from botocore.exceptions import ClientError

    selected = dimension()
    registry = SimpleNamespace(select=lambda _: selected)
    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "DimensionRegistry", lambda _: registry)
    monkeypatch.setattr(cli, "_instance_root", lambda: tmp_path)
    monkeypatch.delenv("JOSH_ROOM_RESULT_FILE", raising=False)

    def denied(*_):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "private-status-sentinel"}}, "HeadObject")

    monkeypatch.setattr(cli, "_backend", lambda *_: SimpleNamespace(read_control=denied))
    assert cli.main(["encryption", "status", "--dimension", selected.dimension_id, "--json"]) != 0
    output = capsys.readouterr()
    assert json.loads(output.out)["error_code"] == "access-denied"
    assert "private-status-sentinel" not in output.out + output.err
