"""Metadata-only negative tests; synthetic identities, no provider access."""
import copy

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
