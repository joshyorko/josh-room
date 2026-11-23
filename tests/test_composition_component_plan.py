"""Approval fingerprints for namespace child Save."""
from types import SimpleNamespace

import pytest

from josh_room.composition_components import artifacts_digest, component_plan


def test_approval_binds_exact_child_and_modes(tmp_path):
    child = tmp_path / 'child'
    child.mkdir()
    modes = {'rcc_environment': 'omit', 'homebrew_recovery': 'omit', 'hauler_content': 'omit'}
    options = {'component_selection': {'format_version': 1, 'components': modes}}
    dimension = SimpleNamespace(dimension_id='dimension')
    plan = component_plan(child, dimension, 'child-a', '1'*64, options)
    assert plan['selection']['components'] == modes
    assert plan['inputs_sha256'] != component_plan(child, dimension, 'child-b', '1'*64, options)['inputs_sha256']
    assert plan['artifacts_sha256'] == artifacts_digest(dict.fromkeys(modes))
    with pytest.raises(ValueError):
        component_plan(child, dimension, 'child-a', '1'*64, {**options, 'credential': 'invalid'})


def test_planning_refuses_missing_required_rcc(tmp_path):
    options = {'component_selection': {'format_version': 1, 'components': {
        'rcc_environment': 'refresh', 'homebrew_recovery': 'omit', 'hauler_content': 'omit'}}}
    with pytest.raises((ValueError, RuntimeError), match='unavailable'):
        component_plan(tmp_path, SimpleNamespace(dimension_id='dim'), 'child', '1'*64, options)
