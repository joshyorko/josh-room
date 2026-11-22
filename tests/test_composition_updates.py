"""Metadata-only negative tests; synthetic identities, no provider access."""
import copy
import json
from pathlib import Path

import pytest
from test_composition_transaction import stages

from josh_room.composition_safety import CompositionSafetyState, plan_digest
from josh_room.composition_transaction import _require_extension


def reserved():
    previous = stages()[-1].to_dict()
    body = copy.deepcopy(previous)
    body.update(contract='composition-safety-v2', child_updates={})
    plan = {'room_id': 'child', 'logical_jat_id': 'jat-update', 'expected_logical_jat_id': 'jat-child',
            'source_path_sha256': 'a' * 64, 'source_records_sha256': 'b' * 64, 'source_identity': [1, 2],
            'source_signature': 'c' * 64, 'capture_policy_sha256': 'd' * 64, 'content_sha256': 'e' * 64,
            'capture_mode': 'source-only'}
    body['child_updates']['update'] = {'plan': plan, 'plan_sha256': plan_digest(plan), 'phase': 'reserved', 'pin': None}
    body['revision'] += 1
    return previous, body


def test_v1_history_can_only_gain_an_unfulfilled_child_reservation():
    previous, body = reserved()
    validated = CompositionSafetyState.from_dict(body).to_dict()
    _require_extension(previous, validated)
    assert validated['operations'] == previous['operations']
    assert validated['dimension_catalog'] == previous['dimension_catalog']
    with pytest.raises(ValueError):
        _require_extension(validated, previous)


@pytest.mark.parametrize('field,value', [('room_id', 'parent'), ('logical_jat_id', 'latest'),
    ('expected_logical_jat_id', 'jat-missing'), ('source_identity', [True, 1]), ('capture_mode', 'full-repository')])
def test_invalid_update_plan_is_rejected(field, value):
    _, body = reserved()
    update = body['child_updates']['update']
    update['plan'][field] = value
    update['plan_sha256'] = plan_digest(update['plan'])
    with pytest.raises((KeyError, ValueError)):
        CompositionSafetyState.from_dict(body)


def test_two_pending_updates_cannot_capture_the_same_room():
    _, body = reserved()
    body['child_updates']['other'] = copy.deepcopy(body['child_updates']['update'])
    body['child_updates']['other']['plan']['logical_jat_id'] = 'jat-other'
    body['child_updates']['other']['plan_sha256'] = plan_digest(body['child_updates']['other']['plan'])
    with pytest.raises(ValueError, match='overlapping'):
        CompositionSafetyState.from_dict(body)


def test_historical_parent_pin_cannot_be_replaced_during_child_reservation():
    previous, body = reserved()
    body['operations']['operation']['manifest_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='manifest'):
        _require_extension(previous, body)


def test_v2_schema_resolves_closed_v1_shapes_and_update_ledger():
    jsonschema = pytest.importorskip('jsonschema')
    from referencing import Registry, Resource
    directory = Path(__file__).resolve().parents[1] / 'schemas'
    registry = Registry()
    for name in ('dimension-catalog-v3.schema.json', 'composition-safety-v1.schema.json'):
        value = json.loads((directory / name).read_text())
        registry = registry.with_resource(value['$id'], Resource.from_contents(value))
    schema = json.loads((directory / 'composition-safety-v2.schema.json').read_text())
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    _, body = reserved()
    validator.validate(body)
    body['child_updates']['update']['plan']['content_digest_algorithm'] = 'sha256-framed-v2'
    body['child_updates']['update']['plan_sha256'] = plan_digest(body['child_updates']['update']['plan'])
    validator.validate(body)
    body['child_updates']['update']['unknown'] = True
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(body)


def test_digest_algorithm_is_versioned_without_rewriting_legacy_history():
    _, legacy = reserved()
    assert 'content_digest_algorithm' not in CompositionSafetyState.from_dict(legacy).to_dict()['child_updates']['update']['plan']
    current = copy.deepcopy(legacy)
    update = current['child_updates']['update']
    update['plan']['content_digest_algorithm'] = 'sha256-framed-v2'
    update['plan_sha256'] = plan_digest(update['plan'])
    assert CompositionSafetyState.from_dict(current).to_dict() == current
    update['plan']['content_digest_algorithm'] = 'sha256-unframed-v1'
    update['plan_sha256'] = plan_digest(update['plan'])
    with pytest.raises(ValueError, match='algorithm'):
        CompositionSafetyState.from_dict(current)


def test_legacy_reserved_update_refuses_resume_before_payload_or_authority_writes(tmp_path):
    from types import SimpleNamespace

    from josh_room.composition_updates import save_child
    _, legacy = reserved()
    authority = SimpleNamespace(namespace='1' * 32, read=lambda: ({'state': legacy}, 'etag'))
    with pytest.raises(ValueError, match='legacy child update digest requires reconciliation'):
        save_child(tmp_path, None, '1' * 32, 'child', 'jat-child', 'update', authority, None, tmp_path)
