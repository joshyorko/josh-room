"""Durable synthetic authority; never a provider admission fixture."""
import copy
import json
import sqlite3

import pytest
from test_composition_safety import fixture

from josh_room.catalog import Catalog
from josh_room.composition_safety import CompositionSafetyState, plan_digest
from josh_room.composition_transaction import (
    manifest_for,
    persist_candidate,
    prepare_parent,
    record_child,
    record_parent,
    reserve,
)
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat


class ProcessLoss(BaseException):
    pass


class DurableCAS:
    """SQLite atomic commit survives closing/reopening, including lost replies."""
    def __init__(self, path, crash=None):
        self.db = sqlite3.connect(path)
        self.db.execute('create table if not exists authority(id integer primary key, body text, revision integer)')
        self.db.execute('insert or ignore into authority values(1,null,0)')
        self.db.commit()
        self.crash = crash
        self.writes = 0

    def read(self):
        body, revision = self.db.execute('select body,revision from authority where id=1').fetchone()
        return (json.loads(body) if body else None), str(revision)

    def compare_and_swap(self, etag, body):
        if self.crash == 'before':
            raise ProcessLoss()
        with self.db:
            result = self.db.execute('update authority set body=?,revision=revision+1 where id=1 and revision=?',
                                     (json.dumps(body), int(etag)))
            if result.rowcount != 1:
                raise ValueError('conflict')
        self.writes += 1
        if self.crash == 'after':
            raise ProcessLoss()
        if self.crash == 'reply':
            raise OSError('synthetic lost reply')


def setup():
    body, descriptors, _ = fixture()
    catalog = copy.deepcopy(body['dimension_catalog'])
    catalog['projects'] = {}
    catalog['revision'] = 0
    plan = body['operations']['operation']['plan']
    state = reserve(Catalog.from_body(catalog), body['scope'], 'operation', plan)
    return state, descriptors, plan_digest(plan), body


def output(body, descriptors, room):
    record = body['dimension_catalog']['projects'][room]['snapshots'][f'jat-{room}']
    return {'descriptor': descriptors[(room, f'jat-{room}')],
                'object_ref': ObjectRef(record['object_key'], record['ciphertext_sha256'], record['ciphertext_size']),
                'workspace_signature': record['workspace_signature'], 'display_name': 'Synthetic Room'}


def stages():
    reserved, descriptors, approved, body = setup()
    child = record_child(reserved, 'operation', approved, 'projects/-child folder', **output(body, descriptors, 'child'))
    prepared = prepare_parent(child, 'operation', approved, policy_sha256='b' * 64)
    committed = record_parent(prepared, 'operation', approved, **output(body, descriptors, 'parent'))
    return [reserved, child, prepared, committed]


@pytest.mark.parametrize('boundary', range(4))
@pytest.mark.parametrize('crash', ['before', 'after', 'reply'])
def test_process_loss_every_cas_reopens_without_duplicate_output(tmp_path, boundary, crash):
    path = tmp_path / 'synthetic.sqlite'
    store = DurableCAS(path)
    previous, etag = None, '0'
    for index, candidate in enumerate(stages()):
        store.crash = crash if index == boundary else None
        if index == boundary and crash != 'reply':
            with pytest.raises(ProcessLoss):
                persist_candidate(store, previous, etag, candidate)
            store.db.close()
            store = DurableCAS(path)
        actual, etag = persist_candidate(store, previous, etag, candidate)
        assert actual.to_dict() == candidate.to_dict()
        previous = actual
    body, etag = store.read()
    assert etag == '4'
    assert body['operations']['operation']['phase'] == 'committed'
    assert sum(len(p['snapshots']) for p in body['dimension_catalog']['projects'].values()) == 2
    store.db.close()


def test_stale_writer_cannot_overwrite_reservation(tmp_path):
    first = DurableCAS(tmp_path / 'authority.sqlite')
    second = DurableCAS(tmp_path / 'authority.sqlite')
    candidate = stages()[0]
    persist_candidate(first, None, '0', candidate)
    other = candidate.to_dict()
    other['operations']['different'] = other['operations'].pop('operation')
    other = CompositionSafetyState.from_dict(other)
    with pytest.raises(ValueError, match='baseline changed'):
        persist_candidate(second, None, '0', other)
    assert second.writes == 0
    first.db.close()
    second.db.close()


def test_changed_plan_or_reserved_descriptor_cannot_be_reused():
    reserved, descriptors, approved, body = setup()
    with pytest.raises(ValueError, match='approved plan differs'):
        record_child(reserved, 'operation', '0' * 64, 'projects/-child folder', **output(body, descriptors, 'child'))
    with pytest.raises(ValueError, match='reserved slot'):
        record_child(reserved, 'operation', approved, 'projects/-child folder', **output(body, descriptors, 'parent'))
    changed = copy.deepcopy(body['operations']['operation']['plan'])
    changed['capture_policy_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='another plan'):
        reserve(Catalog.from_body(reserved.to_dict()['dimension_catalog']), body['scope'], 'operation', changed, previous=reserved)


def test_immutable_retry_and_exact_child_pins():
    reserved, descriptors, approved, body = setup()
    child = record_child(reserved, 'operation', approved, 'projects/-child folder', **output(body, descriptors, 'child'))
    assert record_child(child, 'operation', approved, 'projects/-child folder', **output(body, descriptors, 'child')) == child
    bad = output(body, descriptors, 'child')
    bad['object_ref'] = ObjectRef('objects/sha256/' + '0' * 64, '0' * 64, 50)
    with pytest.raises(ValueError, match='immutable'):
        record_child(child, 'operation', approved, 'projects/-child folder', **bad)
    assert manifest_for(child, 'operation', approved)['children'][0]['snapshot_id'] == 'jat-child'
    with pytest.raises(ValueError):
        prepare_parent(reserved, 'operation', approved, policy_sha256='b' * 64)


def test_parent_contract_and_phase_skips_refused():
    reserved, descriptors, approved, body = setup()
    with pytest.raises(ValueError, match='not prepared'):
        record_parent(reserved, 'operation', approved, **output(body, descriptors, 'parent'))
    prepared = stages()[2]
    bad = output(body, descriptors, 'parent')
    value = bad['descriptor'].to_dict()
    value['capture_policy_sha256'] = '0' * 64
    bad['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='capture differs'):
        record_parent(prepared, 'operation', approved, **bad)
    with pytest.raises(ValueError, match='metadata differs'):
        prepare_parent(prepared, 'operation', approved, policy_sha256='0' * 64)


def test_reservation_survives_without_child_record_and_inputs_are_copied():
    state, _, _, body = setup()
    body['scope']['dimension_id'] = 'changed'
    assert state.to_dict()['dimension_catalog']['projects'] == {}
    assert state.to_dict()['scope']['dimension_id'] == 'dimension'
    with pytest.raises(ValueError):
        Catalog.from_body(state.to_dict())


def test_cas_cannot_skip_phases_or_remove_historical_outputs(tmp_path):
    store = DurableCAS(tmp_path / 'authority.sqlite')
    reserved, child, prepared, committed = stages()
    _, etag = persist_candidate(store, None, '0', reserved)
    with pytest.raises(ValueError):
        persist_candidate(store, reserved, etag, prepared)
    _, etag = persist_candidate(store, reserved, etag, child)
    _, etag = persist_candidate(store, child, etag, prepared)
    _, etag = persist_candidate(store, prepared, etag, committed)
    removed = reserved.to_dict()
    removed['revision'] = committed.to_dict()['revision'] + 1
    removed = CompositionSafetyState.from_dict(removed)
    with pytest.raises(ValueError, match='historical catalog'):
        persist_candidate(store, committed, etag, removed)
    assert store.read()[0] == committed.to_dict()
    store.db.close()


def test_unconfirmed_write_never_retries_and_altered_readback_fails(tmp_path):
    class Refused(DurableCAS):
        def compare_and_swap(self, etag, body):
            self.writes += 1
            raise OSError('synthetic refused write')
    store = Refused(tmp_path / 'refused.sqlite')
    with pytest.raises(ValueError, match='outcome unconfirmed'):
        persist_candidate(store, None, '0', stages()[0])
    assert store.writes == 1
    assert store.read() == (None, '0')
    store.db.close()

    class Changed(DurableCAS):
        def compare_and_swap(self, etag, body):
            altered = copy.deepcopy(body)
            altered['operations']['other'] = altered['operations'].pop('operation')
            super().compare_and_swap(etag, altered)
    store = Changed(tmp_path / 'changed.sqlite')
    with pytest.raises(ValueError, match='readback differs'):
        persist_candidate(store, None, '0', stages()[0])
    assert store.writes == 1
    store.db.close()


def test_new_reservation_retains_old_parent_and_child_pins(tmp_path):
    committed = stages()[-1]
    body = committed.to_dict()
    plan = copy.deepcopy(body['operations']['operation']['plan'])
    plan['parent_logical_jat_id'] = 'jat-parent-2'
    plan['expected_parent_logical_jat_id'] = 'jat-parent'
    plan['children'][0]['logical_jat_id'] = 'jat-child-2'
    next_state = reserve(Catalog.from_body(body['dimension_catalog']), body['scope'], 'second', plan, previous=committed)
    assert next_state.to_dict()['operations']['operation'] == body['operations']['operation']
    assert manifest_for(next_state, 'operation', plan_digest(body['operations']['operation']['plan']))['children'][0]['snapshot_id'] == 'jat-child'
    # Bypassing the helper cannot erase a committed operation through the CAS.
    store = DurableCAS(tmp_path / 'authority.sqlite')
    previous, etag = None, '0'
    for candidate in stages():
        previous, etag = persist_candidate(store, previous, etag, candidate)
    removed = next_state.to_dict()
    del removed['operations']['operation']
    with pytest.raises(ValueError, match='historical operation'):
        persist_candidate(store, committed, etag, CompositionSafetyState.from_dict(removed))
    persist_candidate(store, committed, etag, next_state)
    store.db.close()


@pytest.mark.parametrize('room', ['child', 'parent'])
def test_descriptor_repository_must_match_reserved_authority(room):
    reserved, descriptors, approved, body = setup()
    output_args = output(body, descriptors, room)
    value = output_args['descriptor'].to_dict()
    value['workspace']['repository_id'] = '0' * 64
    output_args['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='repository differs'):
        if room == 'child':
            record_child(reserved, 'operation', approved, 'projects/-child folder', **output_args)
        else:
            record_parent(stages()[2], 'operation', approved, **output_args)


def test_wrong_manifest_digest_refused_before_cas(tmp_path):
    store = DurableCAS(tmp_path / 'authority.sqlite')
    reserved, child, prepared, _ = stages()
    _, etag = persist_candidate(store, None, '0', reserved)
    _, etag = persist_candidate(store, reserved, etag, child)
    wrong = prepared.to_dict()
    wrong['operations']['operation']['manifest_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='manifest differs'):
        persist_candidate(store, child, etag, CompositionSafetyState.from_dict(wrong))
    assert store.writes == 2
    store.db.close()


@pytest.mark.parametrize('phase', ['baseline', 'readback', 'lost-reply'])
def test_cas_mapping_equality_does_not_accept_floating_revision(tmp_path, phase):
    class Floating(DurableCAS):
        def read(self):
            body, etag = super().read()
            if body and (self.writes or phase == 'baseline'):
                body['revision'] = float(body['revision'])
            return body, etag
    path = tmp_path / 'authority.sqlite'
    seed = DurableCAS(path)
    reserved, child, _, _ = stages()
    persist_candidate(seed, None, '0', reserved)
    seed.db.close()
    store = Floating(path, crash='reply' if phase == 'lost-reply' else None)
    with pytest.raises(ValueError, match='baseline changed|readback differs|unconfirmed'):
        persist_candidate(store, reserved, '1', child)
    assert store.writes == (0 if phase == 'baseline' else 1)
    store.db.close()


def test_parent_lineage_and_retry_contract_checked_before_success():
    _, descriptors, approved, body = setup()
    bad = output(body, descriptors, 'parent')
    value = bad['descriptor'].to_dict()
    value['parent_logical_jat_id'] = 'unexpected-predecessor'
    value['workspace']['parent_snapshot_id'] = '3' * 64
    bad['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='lineage differs'):
        record_parent(stages()[2], 'operation', approved, **bad)
    value.pop('parent_logical_jat_id')
    value['workspace'].pop('parent_snapshot_id')
    value.pop('capture_mode')
    bad['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='capture differs'):
        record_parent(stages()[3], 'operation', approved, **bad)


def test_reopened_historical_manifest_ignores_new_child_latest(tmp_path):
    path = tmp_path / 'authority.sqlite'
    store = DurableCAS(path)
    previous, etag = None, '0'
    for candidate in stages():
        previous, etag = persist_candidate(store, previous, etag, candidate)
    body = previous.to_dict()
    plan = copy.deepcopy(body['operations']['operation']['plan'])
    old_approval = plan_digest(plan)
    plan['parent_logical_jat_id'] = 'jat-parent-2'
    plan['expected_parent_logical_jat_id'] = 'jat-parent'
    plan['children'][0]['logical_jat_id'] = 'jat-child-2'
    reserved = reserve(Catalog.from_body(body['dimension_catalog']), body['scope'], 'second', plan, previous=previous)
    previous, etag = persist_candidate(store, previous, etag, reserved)
    _, descriptors, _, fixture_body = setup()
    approved = plan_digest(plan)
    child_args = output(fixture_body, descriptors, 'child')
    value = child_args['descriptor'].to_dict()
    value['logical_jat_id'] = 'jat-child-2'
    child_args['descriptor'] = LogicalJat.from_dict(value)
    child_args['object_ref'] = ObjectRef('objects/sha256/' + '4' * 64, '4' * 64, 50)
    child = record_child(previous, 'second', approved, plan['children'][0]['path'], **child_args)
    previous, etag = persist_candidate(store, previous, etag, child)
    prepared = prepare_parent(previous, 'second', approved, policy_sha256='b' * 64)
    previous, etag = persist_candidate(store, previous, etag, prepared)
    parent_args = output(fixture_body, descriptors, 'parent')
    value = parent_args['descriptor'].to_dict()
    value['logical_jat_id'] = 'jat-parent-2'
    value['parent_logical_jat_id'] = 'wrong-baseline'
    value['workspace']['parent_snapshot_id'] = '2' * 64
    parent_args['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='lineage differs'):
        record_parent(previous, 'second', approved, **parent_args)
    value['parent_logical_jat_id'] = 'jat-parent'
    value['workspace']['parent_snapshot_id'] = '0' * 64
    parent_args['descriptor'] = LogicalJat.from_dict(value)
    with pytest.raises(ValueError, match='workspace lineage differs'):
        record_parent(previous, 'second', approved, **parent_args)
    value['workspace']['parent_snapshot_id'] = '2' * 64
    parent_args['descriptor'] = LogicalJat.from_dict(value)
    parent_args['object_ref'] = ObjectRef('objects/sha256/' + '5' * 64, '5' * 64, 50)
    committed = record_parent(previous, 'second', approved, **parent_args)
    persist_candidate(store, previous, etag, committed)
    store.db.close()
    reopened = DurableCAS(path)
    state = CompositionSafetyState.from_dict(reopened.read()[0])
    assert state.to_dict()['dimension_catalog']['projects']['child']['latest'] == 'jat-child-2'
    assert manifest_for(state, 'operation', old_approval)['children'][0]['snapshot_id'] == 'jat-child'
    assert manifest_for(state, 'second', approved)['children'][0]['snapshot_id'] == 'jat-child-2'
    reopened.db.close()
