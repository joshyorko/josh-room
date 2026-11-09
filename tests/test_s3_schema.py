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
