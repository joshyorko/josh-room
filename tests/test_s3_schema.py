import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SCHEMA = json.loads((Path(__file__).parents[1] / 'schemas/private-config.schema.json').read_text())


def test_private_schema_admits_explicit_s3_without_aws_enumeration_or_inline_keys():
    body = {'default_backend': 's3', 'connections': {'synthetic': {
        'provider': 's3', 'region': 'eu-west-1', 'credential_profile': 'synthetic-profile',
    }}, 'dimensions': {'synthetic-room': {
        'display_name': 'Synthetic Room', 'connection_id': 'synthetic', 'bucket': 'synthetic-room',
    }}}
    Draft202012Validator(SCHEMA).validate(body)


@pytest.mark.parametrize('extra', [
    {'endpoint': 'http://127.0.0.1:9000'},
    {'region': ''},
    {'region': 'auto'},
    {'verify_tls': False},
    {'secret_access_key': 'synthetic-not-permitted'},
])
def test_s3_schema_rejects_insecure_config(extra):
    connection = {'provider': 's3', 'region': 'eu-west-1', 'credential_profile': 'synthetic-profile', **extra}
    assert not Draft202012Validator(SCHEMA).is_valid({'connections': {'synthetic': connection}})


def test_s3_portable_options_round_trip_through_scoped_connection():
    from josh_room.config import ConnectionConfig, DimensionConfig
    connection = {
        'provider': 's3', 'region': 'us-west-2', 'credential_profile': 'synthetic-profile',
        'room_store_authority_endpoint': 'https://authority.example.invalid',
        'room_store_authority_profile': 'separate-authority-capability',
    }
    body = {'connections': {'aws': connection}, 'dimensions': {'room': {
        'display_name': 'Room', 'connection_id': 'aws', 'bucket': 'synthetic-room',
    }}}
    Draft202012Validator(SCHEMA).validate(body)
    parsed = ConnectionConfig.from_private('aws', connection)
    selected = DimensionConfig.from_private('room', body['dimensions']['room'], {'aws': parsed})
    assert selected.provider == 's3'
    assert selected.region == 'us-west-2'
    assert selected.endpoint == 'https://s3.us-west-2.amazonaws.com'
    assert selected.credential_profile == 'synthetic-profile'
    assert selected.option('room_store_authority_profile') == 'separate-authority-capability'
    assert selected.option('room_store_authority_endpoint') == connection['room_store_authority_endpoint']
    assert parsed.to_private()['room_store_authority_endpoint'] == connection['room_store_authority_endpoint']
