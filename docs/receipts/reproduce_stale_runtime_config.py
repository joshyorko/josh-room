"""Configuration-only diagnostic; never contacts a provider or reads user config.

Run from the reviewed checkout: python3 <EVIDENCE>/reproduce_stale_runtime_config.py
"""
import importlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

EXPECTED_SOURCE = '4274a59fd4a15e97bf51dd2f74aca7ef41bac86e'
source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
assert source == EXPECTED_SOURCE, 'Run from the exact reviewed source checkout'
sys.path.insert(0, str(Path.cwd() / 'src'))

def reject_external_access(event, args):
    if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system'}:
        raise RuntimeError('Configuration-only fixture forbids network and child processes')

# Source identity was obtained above; prohibit subsequent network/process actions.
sys.addaudithook(reject_external_access)
with tempfile.TemporaryDirectory(prefix='room-config-only-fixture-') as temporary:
    root = Path(temporary)
    os.environ['JOSH_ROOM_CONFIG_DIR'] = str(root / 'persisted')
    os.environ['JOSH_ROOM_RUNTIME_CONFIG'] = str(root / 'session-config.json')
    config = importlib.import_module('josh_room.config')
    def forbid_keyring(*args, **kwargs):
        raise AssertionError('Keyring access is forbidden in this fixture')
    config.available = forbid_keyring
    snapshot = root / 'session-config.json'
    initial = {'connections': {}}
    snapshot.write_text(json.dumps(initial))
    body = {'connections': {'synthetic-minio': {
        'display_name': 'Synthetic fixture', 'provider': 'minio',
        'endpoint': 'http://127.0.0.1:19000',
        'credential_profile': 'synthetic-unused-profile', 'region': 'us-east-1',
    }}}
    config.save_private_config(body)
    observed = {
        'persisted_connection_present': 'synthetic-minio' in config.connection_configs(config.persisted_config()),
        'runtime_lookup_missing': config.connection_configs(config.private_config()).get('synthetic-minio') is None,
        'session_snapshot_unchanged': json.loads(snapshot.read_text()) == initial,
    }
    assert all(observed.values()), observed
receipt = {
    'format_version': 1,
    'assessment': 'unresolved-stale-runtime-config',
    'source_git_sha': source,
    'execution': {
        'command': 'python3 <EVIDENCE>/reproduce_stale_runtime_config.py',
        'working_directory': '<REVIEWED_CHECKOUT>',
        'interpreter': {'implementation': platform.python_implementation(), 'version': platform.python_version()},
        'environment': 'existing task Python; repository source imported directly; temporary synthetic configuration only',
        'dependencies_installed': False,
    },
    'result': {'status': 'REPRODUCED_CONFIGURATION_LAYER', 'expected': {k: True for k in observed}, 'observed': observed},
    'boundaries': {
        'credentials_present': False, 'keyring_calls': False,
        'network_calls': False, 'provider_operations': False,
        'user_configuration_accessed': False, 'production_source_modified': False,
        'temporary_fixture_removed': True,
    },
    'scope': {
        'full_r2_then_minio_ui_sequence': 'NOT_REPRODUCED',
        'later_missing_rooms': 'NOT_REPRODUCED',
        'reported_immediate_bucket_error_causality': 'HIGH_CONFIDENCE_SOURCE_LEAD_NOT_FULL_UI_PROOF',
        'fix_status': 'UNRESOLVED_NO_REPAIR_ATTEMPTED',
    },
    'source_references': {
        'session_snapshot_copy': 'src/josh_room/auth.py:639',
        'session_snapshot_write': 'src/josh_room/auth.py:695',
        'runtime_snapshot_selection': 'src/josh_room/auth.py:475',
        'snapshot_read_precedence': 'src/josh_room/config.py:367',
        'persisted_write': 'src/josh_room/config.py:383',
        'connection_creation': 'src/josh_room/cli.py:1873',
        'bucket_lookup_missing_error': 'src/josh_room/cli.py:1903',
    },
}
print(json.dumps(receipt, indent=2, sort_keys=True))
