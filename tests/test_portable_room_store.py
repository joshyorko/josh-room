import base64
import dataclasses
import json

import pytest
from test_minio_encryption_flow import dimension, identity, make_keyset
from test_r2_room_store_material import Broker
from test_room_store_material import RoomStoreBackend

from josh_room import auth
from josh_room import portable_room_store as portable
from josh_room import r2_room_store_material as client
from josh_room.encryption_domain import EncryptionKeyset


@pytest.fixture
def migration(monkeypatch, tmp_path):
    source = make_keyset().upgrade_for_room_store().bind_repository("a" * 64, expected_generation=1)
    material = type("Material", (), {"keyset": source, "identity": identity(tmp_path / "identity")})()
    selected = dimension(options=(("room_store_authority_endpoint", "https://authority.example.invalid"), ("room_store_authority_profile", "portable")))
    backend = RoomStoreBackend(control=source.to_json())
    broker = Broker()
    original = broker.__call__

    def transport(method, session, operation, headers, body):
        if operation == "seal":
            broker.repository_error = "room_store_authority_sealed"
            return 200, {"status": "sealed"}
        from test_r2_room_store_material import CAPABILITY, SESSION
        return original(method, SESSION, operation, {**headers, "Authorization": f"Bearer {CAPABILITY}"}, body)

    def encrypt(data, recipients, output):
        output.write_bytes(data)
        output.chmod(0o600)

    monkeypatch.setattr(client.crypto, "encrypt", encrypt)
    monkeypatch.setattr(client.crypto, "decrypt", lambda path, identities, limit: path.read_bytes())
    monkeypatch.setattr(client.keyring, "store_room_store_secret", lambda *args: None)
    monkeypatch.setattr(auth, "_material_for_keyset", lambda keyset: type("Material", (), {"keyset": keyset, "identity": material.identity})())
    return selected, backend, material, broker, transport


def test_migration_encrypts_only_password_preserves_domain_and_fences_legacy(migration):
    selected, backend, material, broker, transport = migration
    marker = portable.migrate(selected, backend, material, transport=transport)
    assert material.keyset.room_store.secret not in backend.control.decode()
    with pytest.raises((ValueError, TypeError)):
        EncryptionKeyset.from_json(backend.control)
    inner = json.loads(base64.urlsafe_b64decode(broker.record["ciphertext"] + "=" * (-len(broker.record["ciphertext"]) % 4)))
    assert inner["secret"] == material.keyset.room_store.secret
    assert "operational_identity" not in inner
    assert "recovery_recipients" not in inner
    assert broker.record["repositoryId"] == material.keyset.room_store.repository_id
    assert broker.record["keysetGeneration"] == material.keyset.room_store.generation
    resolved = portable.marker_keyset(marker, selected, material_factory=lambda _: material, transport=transport)
    assert resolved == material.keyset
    assert resolved.room_store.secret == material.keyset.room_store.secret


def test_unmigrated_store_keeps_legacy_authority_without_writes(migration, monkeypatch):
    selected, backend, _material, broker, _transport = migration
    before = backend.control
    monkeypatch.setattr(portable, "authority", lambda *_args, **_kwargs: pytest.fail("unmigrated store contacted DO authority"))
    assert portable.selected_keyset(selected, backend) is None
    assert backend.control == before
    assert broker.record is None


def test_rollback_seals_before_restoring_legacy_and_keeps_password(migration):
    selected, backend, material, broker, transport = migration
    portable.migrate(selected, backend, material, transport=transport)
    restored = portable.rollback(selected, backend, transport=transport)
    assert broker.repository_error == "room_store_authority_sealed"
    assert restored == material.keyset
    assert restored.room_store.secret == material.keyset.room_store.secret
    assert EncryptionKeyset.from_json(backend.control) == material.keyset


def test_migration_cannot_overwrite_a_concurrent_legacy_bind(migration):
    selected, backend, material, _broker, transport = migration
    backend.replace_conflict_winner = material.keyset
    with pytest.raises(RuntimeError, match="publication unverified"):
        portable.migrate(selected, backend, material, transport=transport)
    assert EncryptionKeyset.from_json(backend.control) == material.keyset


def test_marker_rejects_wrong_authority_alias_before_network_or_decryption(migration):
    selected, backend, material, _broker, transport = migration
    marker = portable.migrate(selected, backend, material, transport=transport)
    marker["authority"] = "https://wrong.example.invalid"
    with pytest.raises(ValueError, match="selection mismatch"):
        portable.marker_keyset(marker, selected, material_factory=lambda _: pytest.fail("no decryption"), transport=lambda *args: pytest.fail("no network"))


def test_migration_never_accepts_same_metadata_with_a_different_password(migration):
    selected, backend, material, broker, transport = migration
    changed = dataclasses.replace(material.keyset, room_store=dataclasses.replace(material.keyset.room_store, secret=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")))
    backend.control = changed.to_json()
    with pytest.raises(ValueError, match="current format-2"):
        portable.migrate(selected, backend, material, transport=transport)
    assert broker.record is None


def s3_enrollment(migration):
    selected, backend, material, broker, transport = migration
    selected = dataclasses.replace(
        selected, provider="s3", endpoint="https://s3.us-west-2.amazonaws.com", region="us-west-2",
    )
    source = dataclasses.replace(material.keyset, provider="s3", endpoint=selected.endpoint, format_version=1, room_store=None)
    material.keyset = source
    backend.control = source.to_json()
    backend.config.endpoint = selected.endpoint
    return selected, backend, material, broker, transport


def test_new_s3_room_selects_shared_authority_without_plaintext_password(migration, monkeypatch):
    selected, backend, material, broker, transport = s3_enrollment(migration)
    original = portable.authority
    monkeypatch.setattr(portable, "authority", lambda dimension, material, **kwargs: original(dimension, material, transport=transport))
    monkeypatch.setattr(auth, "store_room_store_secret", lambda *args: None)
    result = auth.ensure_room_store_keyset(selected, backend)
    assert result.provider == "s3"
    assert result.encryption_domain_id == material.keyset.encryption_domain_id
    assert result.room_store.repository_id is None
    assert broker.record is not None
    assert json.loads(backend.control)["format"] == portable.MARKER_FORMAT
    assert result.room_store.secret.encode() not in backend.control
    bound = auth.bind_room_store_repository(selected, backend, "a" * 64, expected_generation=1)
    assert bound.room_store.generation == 2
    assert bound.room_store.physical_binding == result.binding


def test_s3_read_only_does_not_select_or_initialize_authority(migration, monkeypatch):
    selected, backend, _material, broker, _transport = s3_enrollment(migration)
    monkeypatch.setattr(portable, "authority", lambda *args, **kwargs: pytest.fail("read contacted authority"))
    before = backend.control
    assert portable.selected_keyset(selected, backend) is None
    assert backend.control == before
    assert broker.record is None


def test_existing_s3_store_is_not_implicitly_migrated(migration, monkeypatch):
    selected, backend, material, broker, _transport = s3_enrollment(migration)
    existing = material.keyset.upgrade_for_room_store()
    backend.control = existing.to_json()
    monkeypatch.setattr(portable, "authority", lambda *args, **kwargs: pytest.fail("existing store contacted authority"))
    monkeypatch.setattr(auth, "store_room_store_secret", lambda *args: None)
    before = backend.control
    assert auth.ensure_room_store_keyset(selected, backend).room_store.secret == existing.room_store.secret
    assert backend.control == before
    assert broker.record is None


def test_s3_lost_create_response_recovers_same_winner(migration, monkeypatch):
    selected, backend, _material, broker, transport = s3_enrollment(migration)
    broker.ambiguous = True
    original = portable.authority
    monkeypatch.setattr(portable, "authority", lambda dimension, material, **kwargs: original(dimension, material, transport=transport))
    result = portable.selected_keyset(selected, backend, initialize=True)
    assert result.room_store.generation == 1
    before = backend.control
    assert portable.selected_keyset(selected, backend).room_store.secret == result.room_store.secret
    assert backend.control == before


def test_s3_failed_selection_never_overwrites_concurrent_enrollment(migration, monkeypatch):
    selected, backend, material, _broker, transport = s3_enrollment(migration)
    backend.replace_conflict_winner = dataclasses.replace(material.keyset, key_generation=2)
    original = portable.authority
    monkeypatch.setattr(portable, "authority", lambda dimension, material, **kwargs: original(dimension, material, transport=transport))
    with pytest.raises(RuntimeError, match="enrollment publication unverified"):
        portable.selected_keyset(selected, backend, initialize=True)
    assert EncryptionKeyset.from_json(backend.control).key_generation == 2


def test_s3_scope_uses_own_provider_bucket_and_endpoint(migration):
    selected, _backend, _material, _broker, _transport = s3_enrollment(migration)
    assert portable.domain_id(selected) != portable.domain_id(dataclasses.replace(selected, provider="minio"))
    assert portable.domain_id(selected) != portable.domain_id(dataclasses.replace(selected, bucket="other-bucket"))
    assert portable.domain_id(selected) != portable.domain_id(dataclasses.replace(selected, endpoint="https://s3.eu-west-1.amazonaws.com", region="eu-west-1"))
    assert portable.domain_id(selected) == portable.domain_id(dataclasses.replace(selected, endpoint="https://S3.US-WEST-2.AMAZONAWS.COM:443/"))


def test_unmigrated_minio_format_one_never_implicitly_selects_do(migration, monkeypatch):
    selected, backend, material, broker, _transport = migration
    source = dataclasses.replace(material.keyset, format_version=1, room_store=None)
    backend.control = source.to_json()
    monkeypatch.setattr(portable, "authority", lambda *args, **kwargs: pytest.fail("MinIO contacted DO"))
    assert portable.selected_keyset(selected, backend, initialize=True) is None
    assert backend.control == source.to_json()
    assert broker.record is None


def test_new_s3_room_without_opt_in_keeps_existing_route(migration, monkeypatch):
    selected, backend, _material, broker, _transport = s3_enrollment(migration)
    selected = dataclasses.replace(selected, options=())
    monkeypatch.setattr(portable, "authority", lambda *args, **kwargs: pytest.fail("unconfigured S3 contacted DO"))
    before = backend.control
    assert portable.selected_keyset(selected, backend, initialize=True) is None
    assert backend.control == before
    assert broker.record is None


def test_s3_real_age_client_executes_shared_actor_and_cold_readback(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from pathlib import Path

    from josh_room import crypto
    from josh_room.config import DimensionConfig
    from josh_room.encryption_domain import EncryptionMaterial
    from josh_room.s3_backend import S3Backend, S3Config

    if not all(shutil.which(tool) for tool in ('node', 'age', 'age-keygen')):
        pytest.skip('local shared actor and actual age require Node, age and age-keygen')
    operational = crypto.generate_identity(tmp_path / 'operational.age')
    recovery = crypto.generate_identity(tmp_path / 'recovery.age')
    selected = DimensionConfig(
        'aws', 'Synthetic AWS', 's3', 'https://s3.us-west-2.amazonaws.com',
        'synthetic-room-store', 'storage-only-profile', region='us-west-2',
        options=(('room_store_authority_endpoint', 'https://authority.example.invalid'), ('room_store_authority_profile', 'authority-only-profile')),
    )
    source = EncryptionKeyset.create('s3', selected.endpoint, selected.bucket,
        operational.read_text().splitlines()[-1], crypto.derive_recipient(operational),
        [crypto.derive_recipient(recovery)])
    material = EncryptionMaterial(source, operational)
    backend = RoomStoreBackend(control=source.to_json())
    backend.config.endpoint = selected.endpoint
    monkeypatch.setattr(auth, '_material_for_keyset', lambda keyset: EncryptionMaterial(keyset, operational))
    monkeypatch.setattr(auth, 'store_room_store_secret', lambda *args: None)
    monkeypatch.setattr(client.keyring, 'store_room_store_secret', lambda *args: None)
    monkeypatch.setattr(auth, 'start_oauth_session', lambda *args, **kwargs: pytest.fail('S3 must not use Cloudflare OAuth'))
    state = tmp_path / 'actor-state.json'
    def transport(method, session, operation, headers, body):
        result = subprocess.run(['node', str(Path(__file__).with_name('portable_room_store_actor.mjs')), portable.domain_id(selected), str(state)],
            input=json.dumps({'method': method, 'operation': operation, 'body': None if body is None else json.loads(body)}),
            text=True, capture_output=True, check=True, timeout=10)
        result = json.loads(result.stdout)
        return result['status'], result['body']
    original = portable.authority
    monkeypatch.setattr(portable, 'authority', lambda dimension, material, **kwargs: original(dimension, material, transport=transport))
    created = auth.ensure_room_store_keyset(selected, backend)
    assert created.encryption_domain_id == source.encryption_domain_id
    assert created.room_store.secret.encode() not in backend.control
    assert created.room_store.secret not in state.read_text()
    assert source.operational_identity not in state.read_text()
    bound = auth.bind_room_store_repository(selected, backend, 'd' * 64, expected_generation=1)
    assert bound.room_store.generation == 2
    before = (backend.control, backend.control_etag, state.read_bytes())
    read, _etag = auth._read_keyset_record_from_backend(selected, backend)
    assert read.room_store.secret == created.room_store.secret
    assert read.room_store.repository_id == 'd' * 64
    assert (backend.control, backend.control_etag, state.read_bytes()) == before
    with pytest.raises(client.R2RoomStoreError, match='generation_conflict'):
        auth.bind_room_store_repository(selected, backend, 'd' * 64, expected_generation=9)
    wrong = dataclasses.replace(selected, bucket='wrong-bucket')
    with pytest.raises(ValueError, match='selection mismatch'):
        portable.marker_keyset(json.loads(backend.control), wrong, material_factory=lambda base: material)
    # Exercise the existing S3 SDK's control-object wire contract without AWS.
    import boto3
    from botocore.stub import Stubber
    sdk = boto3.client('s3', endpoint_url=selected.endpoint, region_name=selected.region,
        aws_access_key_id='synthetic-access', aws_secret_access_key='synthetic-secret')
    storage = S3Backend(S3Config.from_dimension(selected), client=sdk)
    from josh_room.encryption_domain import KEYSET_CONTROL_KEY
    with Stubber(sdk) as stub:
        stub.add_response('put_object', {'ETag': '"marker-v2"'}, {
            'Bucket': selected.bucket, 'Key': KEYSET_CONTROL_KEY, 'Body': backend.control,
            'IfMatch': '"format-one"', 'ContentLength': len(backend.control),
        })
        import io

        from botocore.response import StreamingBody
        stub.add_response('head_object', {'ETag': '"marker-v2"', 'ContentLength': len(backend.control)},
            {'Bucket': selected.bucket, 'Key': KEYSET_CONTROL_KEY})
        stub.add_response('get_object', {'ETag': '"marker-v2"', 'ContentLength': len(backend.control),
            'Body': StreamingBody(io.BytesIO(backend.control), len(backend.control))},
            {'Bucket': selected.bucket, 'Key': KEYSET_CONTROL_KEY, 'IfMatch': '"marker-v2"'})
        storage.replace_control(KEYSET_CONTROL_KEY, backend.control, '"format-one"')
        stub.assert_no_pending_responses()
