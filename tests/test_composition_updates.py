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


def test_full_repository_child_update_stages_git_only_changes(tmp_path, monkeypatch):
    import subprocess

    from josh_room import composition_updates
    from josh_room.workspace_policy import load_capture_policy

    child, private = tmp_path / 'child', tmp_path / 'private'
    child.mkdir()
    private.mkdir()
    subprocess.run(['git', 'init', '-q', '--template=', str(child)], check=True)
    (child / '.josh-roomignore').write_text('.josh-room-split.json\n')
    (child / '.josh-room-split.json').write_text(json.dumps({
        'namespace': 'ns', 'room_id': 'room', 'snapshot_id': 'baseline'}))
    monkeypatch.setattr(composition_updates, 'context_status', lambda _: {
        'ok': True, 'composition_namespace': 'ns', 'project_id': 'room', 'snapshot_id': 'baseline'})
    policy = load_capture_policy(child)

    def stage():
        return composition_updates._child_view(child, private, 'ns', 'room', 'baseline',
                                                {'baseline'}, policy.sha256, 'full-repository')

    original, first = stage()
    (child / '.git/HEAD').write_text('ref: refs/heads/alternate\n')
    current, second = stage()
    assert first['capture_mode'] == second['capture_mode'] == 'full-repository'
    assert first['content_sha256'] != second['content_sha256']
    assert (original / '.git/HEAD').read_bytes() != (current / '.git/HEAD').read_bytes()


def test_parent_children_reads_only_exact_parent_and_committed_points(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from josh_room import composition_updates as updates
    from josh_room.composition import MANIFEST
    from josh_room.composition_transaction import canonical, manifest_for

    state = stages()[-1]
    body = state.to_dict()
    namespace = '1' * 32
    authority = SimpleNamespace(namespace=namespace, binding=body['scope'], read=lambda: ({'state': body}, 'etag'))
    marker = {'path_matches': True, 'composition_namespace': namespace, 'project_id': 'parent',
              'snapshot_id': 'jat-parent', 'dimension_id': body['scope']['dimension_id'],
              'encryption_domain_id': body['scope']['encryption_domain_id']}
    monkeypatch.setattr(updates, 'context_status', lambda _: marker)
    parent = body['operations']['operation']
    manifest = tmp_path / MANIFEST
    manifest.write_bytes(canonical(manifest_for(state, 'operation', parent['plan_sha256'])))
    result = updates.parent_children(tmp_path, authority, namespace, 'parent', 'jat-parent')
    assert result['current_parent'] and result['parent_snapshot_id'] == 'jat-parent'
    assert result['children'] == [{'path': 'projects/-child folder', 'room_id': 'child', 'snapshot_id': 'jat-child',
                                   'other_point_count': 0}]
    history = updates.parent_children(tmp_path, authority, namespace, 'parent', 'jat-parent',
                                      revision=result['revision'], child_path='projects/-child folder')
    assert history['recovery_points'] == [] and history['next_offset'] is None
    with pytest.raises(ValueError, match='choices changed'):
        updates.parent_children(tmp_path, authority, namespace, 'parent', 'jat-parent', revision=result['revision'] - 1)
    with pytest.raises(ValueError, match='unavailable'):
        updates.parent_children(tmp_path, authority, namespace, 'parent', 'latest')
    marker['path_matches'] = False
    with pytest.raises(ValueError, match='exact saved location'):
        updates.parent_children(tmp_path, authority, namespace, 'parent', 'jat-parent')
    marker['path_matches'] = True
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='manifest differs'):
        updates.parent_children(tmp_path, authority, namespace, 'parent', 'jat-parent')


@pytest.mark.parametrize("inventory", ["32-long-child-paths", "129-versions"])
def test_picker_pages_round_trip_every_row_through_cli_json(inventory):
    from josh_room.cli import _bounded_json_result
    from josh_room.composition_updates import _picker_page

    if inventory == "32-long-child-paths":
        # Each component fits a native 255-byte filename. The escaped JSON
        # encoding of all 32 paths together exceeds a single controller reply.
        prefix = "/".join(["🦕" * 59] * 8)
        rows = [{'path': f"{prefix}/{i}", 'room_id': f"child-{i}", 'snapshot_id': f"jat-{i}", 'other_point_count': 1}
                for i in range(32)]
        key = "children"
    else:
        rows = [{'snapshot_id': f"jat-{i}", 'created_at': "2026-11-18T12:00:00Z"} for i in range(129)]
        key = "recovery_points"
    offset, received, pages = 0, [], 0
    while offset is not None:
        page = _picker_page({'ok': True, 'revision': 42}, key, rows, offset)
        wire = _bounded_json_result(page)
        assert len(json.dumps(wire).encode()) < 65536 and wire == page
        received.extend(wire[key])
        pages += 1
        offset = wire['next_offset']
    assert pages > 1 and received == rows
