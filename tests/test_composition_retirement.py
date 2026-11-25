"""Synthetic exact retirement; no provider or payload deletion is exercised."""
import copy
import json
import shutil
from types import SimpleNamespace

import pytest
from test_composition_transaction import stages

from josh_room.composition_authority import CompositionAuthority
from josh_room.composition_retirement import (
    active_inventory,
    preview_retirement,
    require_active,
    retire,
    validate_active_transition,
    validate_retirements,
)
from josh_room.composition_safety import CompositionSafetyState, plan_digest


class Authority:
    namespace = '1' * 32

    def __init__(self, state=None):
        state = state or stages()[-1].to_dict()
        self.binding = {key: state['scope'][key] for key in (
            'provider', 'connection_id', 'dimension_id', 'encryption_domain_id')}
        self.binding['physical_bucket'] = 'synthetic-bucket'
        self.body = {'format': 'composition-authority-v1', 'namespace': self.namespace,
                     'binding': self.binding, 'state': state, 'request': {},
                     'secret': 'a' * 43, 'repository_id': state['scope']['repository_id'],
                     'capture_claims': {}}
        self.etag = '1'
        self.writes = 0

    def read(self):
        return copy.deepcopy(self.body), self.etag

    def validate(self, body):
        CompositionAuthority.validate(self, body)

    def put(self, body, etag):
        assert etag == self.etag
        self.validate(body)
        self.body = copy.deepcopy(body)
        self.etag = str(int(self.etag) + 1)
        self.writes += 1
        return self.etag


def apply(authority, kind, room=None, snapshot=None):
    plan = preview_retirement(authority, authority.namespace, kind, room, snapshot)
    return retire(authority, authority.namespace, kind, room, snapshot,
                  approved_plan_sha256=plan['approval_sha256'])


def retire_parent(authority):
    return apply(authority, 'recovery-point', 'parent', 'jat-parent')


def test_exact_dependency_order_preserves_history_and_empty_catalog_visibility():
    authority = Authority()
    original = copy.deepcopy(authority.body)
    child = preview_retirement(authority, authority.namespace, 'recovery-point', 'child', 'jat-child')
    assert child['plan']['blockers'] == [{'reason': 'retained-parent-pin', 'operation_id': 'operation',
                                         'room': 'parent', 'snapshot': 'jat-parent'}]
    with pytest.raises(ValueError, match='blocked'):
        retire(authority, authority.namespace, 'recovery-point', 'child', 'jat-child',
               approved_plan_sha256=child['approval_sha256'])
    assert authority.writes == 0
    assert retire_parent(authority)['physical_deletion'] is False
    inventory = active_inventory(authority.body)
    assert inventory['catalog_active']
    assert inventory['rooms']['parent']['recovery_points'] == []
    assert inventory['rooms']['child']['recovery_points'] == ['jat-child']
    apply(authority, 'recovery-point', 'child', 'jat-child')
    preview = preview_retirement(authority, authority.namespace, 'catalog')
    assert preview['plan']['blockers'] == [{'reason': 'catalog-has-child-rooms', 'count': 1}]
    assert active_inventory(authority.body)['rooms']['child']['recovery_points'] == []
    apply(authority, 'room', 'child')
    apply(authority, 'catalog')
    assert active_inventory(authority.body) == {'catalog_active': False, 'rooms': {}}
    for field in ('state', 'request', 'secret', 'repository_id', 'capture_claims', 'binding'):
        assert authority.body[field] == original[field]


def test_older_retained_parent_blocks_child_after_latest_parent_is_retired(tmp_path):
    from josh_room.composition_updates import parent_children
    authority = Authority()
    state = authority.body['state']
    latest = copy.deepcopy(state['operations']['operation'])
    latest['plan']['parent_logical_jat_id'] = 'jat-parent-new'
    latest['plan']['expected_parent_logical_jat_id'] = 'jat-parent'
    latest['plan_sha256'] = plan_digest(latest['plan'])
    latest['parent_pin']['logical_jat_id'] = 'jat-parent-new'
    state['operations']['new-operation'] = latest
    project = state['dimension_catalog']['projects']['parent']
    record = copy.deepcopy(project['snapshots']['jat-parent'])
    record['snapshot_id'] = 'jat-parent-new'
    project['snapshots']['jat-parent-new'] = record
    project['latest'] = 'jat-parent-new'
    CompositionSafetyState.from_dict(state)
    apply(authority, 'recovery-point', 'parent', 'jat-parent-new')
    preview = preview_retirement(authority, authority.namespace, 'recovery-point', 'child', 'jat-child')
    assert [blocker['snapshot'] for blocker in preview['plan']['blockers']] == ['jat-parent']
    assert active_inventory(authority.body)['rooms']['parent']['latest'] == 'jat-parent'
    with pytest.raises(ValueError, match='Catalog choices changed'):
        parent_children(tmp_path, authority, authority.namespace, 'parent', 'jat-parent',
                        revision=state['revision'])


@pytest.mark.parametrize('kind,room,snapshot', [
    ('catalog', None, None), ('room', 'child', None),
    ('recovery-point', 'parent', 'jat-parent'),
])
def test_stale_approval_rejected_without_writes(kind, room, snapshot):
    authority = Authority()
    preview = preview_retirement(authority, authority.namespace, kind, room, snapshot)
    authority.etag = '2'
    with pytest.raises(ValueError, match='stale'):
        retire(authority, authority.namespace, kind, room, snapshot,
               approved_plan_sha256=preview['approval_sha256'])
    assert authority.writes == 0


def test_same_etag_changed_authenticated_body_rejects_approval():
    authority = Authority()
    preview = preview_retirement(authority, authority.namespace, 'recovery-point', 'parent', 'jat-parent')
    authority.body['request'] = {'changed': True}
    with pytest.raises(ValueError, match='stale'):
        retire(authority, authority.namespace, 'recovery-point', 'parent', 'jat-parent',
               approved_plan_sha256=preview['approval_sha256'])
    assert authority.writes == 0


def test_race_between_preview_and_apply_read_refuses_write():
    authority = Authority()
    preview = preview_retirement(authority, authority.namespace, 'recovery-point', 'parent', 'jat-parent')
    read = authority.read
    calls = []

    def racing_read():
        calls.append(True)
        if len(calls) == 2:
            authority.etag = '2'
        return read()

    authority.read = racing_read
    with pytest.raises(ValueError, match='stale'):
        retire(authority, authority.namespace, 'recovery-point', 'parent', 'jat-parent',
               approved_plan_sha256=preview['approval_sha256'])
    assert authority.writes == 0


@pytest.mark.parametrize('stage', [0, 1, 2])
def test_incomplete_reservations_block_retirement(stage):
    authority = Authority(stages()[stage].to_dict())
    preview = preview_retirement(authority, authority.namespace, 'catalog')
    assert {'reason': 'incomplete-reservation'} in preview['plan']['blockers']
    assert not preview['can_apply']


def test_incomplete_child_update_and_unbound_capture_claim_block():
    from test_composition_updates import reserved
    _, state = reserved()
    authority = Authority(state)
    authority.body['capture_claims'] = {'jat-unrecorded': 'synthetic-claim'}
    preview = preview_retirement(authority, authority.namespace, 'recovery-point', 'parent', 'jat-parent')
    assert preview['plan']['blockers'] == [
        {'reason': 'incomplete-reservation'}, {'reason': 'incomplete-capture-claim'}]


@pytest.mark.parametrize('kind,room,snapshot', [
    ('recovery-point', 'child', 'latest'), ('recovery-point', 'child', 'jat-missing'),
    ('room', 'parent', None), ('catalog', 'parent', None),
    ('room', 'child', 'jat-child'), ('other', None, None),
])
def test_invalid_or_ambiguous_targets_fail_closed(kind, room, snapshot):
    authority = Authority()
    with pytest.raises(ValueError):
        preview_retirement(authority, authority.namespace, kind, room, snapshot)
    assert authority.writes == 0


def test_nonempty_room_and_catalog_never_cascade():
    authority = Authority()
    assert preview_retirement(authority, authority.namespace, 'room', 'child')['plan']['blockers'] == [
        {'reason': 'room-not-empty', 'count': 1}]
    assert preview_retirement(authority, authority.namespace, 'catalog')['plan']['blockers'] == [
        {'reason': 'catalog-has-child-rooms', 'count': 1},
        {'reason': 'catalog-has-recovery-points', 'count': 2}]
    assert authority.writes == 0


def test_exact_reads_and_updates_refuse_retired_point_before_payload_access(tmp_path):
    from josh_room.composition_lifecycle import hydrate_composition
    from josh_room.composition_updates import _descriptor, save_child
    authority = Authority()
    retire_parent(authority)
    with pytest.raises(ValueError, match='retired'):
        _descriptor(authority, CompositionSafetyState.from_dict(authority.body['state']), 'parent', 'jat-parent')
    with pytest.raises(ValueError, match='retired'):
        hydrate_composition(None, authority.namespace, 'parent', 'jat-parent', tmp_path,
                            authority, None, tmp_path)
    apply(authority, 'recovery-point', 'child', 'jat-child')
    with pytest.raises(ValueError, match='retired'):
        save_child(tmp_path, None, authority.namespace, 'child', 'jat-child', 'new-update', authority, None, tmp_path)
    apply(authority, 'room', 'child')
    with pytest.raises(ValueError, match='retired'):
        require_active(authority.body, 'child')


@pytest.mark.parametrize('mutation', ['unknown', 'duplicate', 'sequence', 'digest', 'dependency'])
def test_authenticated_ledger_rejects_malformed_or_dependency_breaking_records(mutation):
    authority = Authority()
    retire_parent(authority)
    body = copy.deepcopy(authority.body)
    record = body['retirements'][0]
    if mutation == 'unknown':
        record['extra'] = True
    elif mutation == 'duplicate':
        second = copy.deepcopy(record)
        second['sequence'] = 2
        body['retirements'].append(second)
    elif mutation == 'sequence':
        record['sequence'] = True
    elif mutation == 'digest':
        record['approval_sha256'] = 'z' * 64
    else:
        record['target'].update(room='child', snapshot='jat-child')
    with pytest.raises(ValueError):
        authority.validate(body)


@pytest.mark.parametrize('mutation', ['downgrade', 'rewrite', 'state', 'stale'])
def test_real_authority_write_preflight_rejects_tampering_before_encryption(mutation):
    authority = Authority()
    retire_parent(authority)
    body, etag = authority.read()
    if mutation == 'downgrade':
        body['format'] = 'composition-authority-v1'
        body.pop('retirements')
    elif mutation == 'rewrite':
        body['retirements'][0]['approval_sha256'] = 'b' * 64
    elif mutation == 'state':
        body['request'] = {'changed': True}
    else:
        etag = 'stale'
    with pytest.raises(ValueError):
        CompositionAuthority.put(authority, body, etag)


def test_ledger_validation_preserves_unrelated_pending_updates():
    from test_composition_updates import reserved
    authority = Authority()
    retire_parent(authority)
    previous = copy.deepcopy(authority.body)
    _, pending = reserved()
    pending['child_updates']['update']['plan']['content_digest_algorithm'] = 'sha256-framed-v2'
    pending['child_updates']['update']['plan_sha256'] = plan_digest(pending['child_updates']['update']['plan'])
    authority.body['state'] = pending
    validate_retirements(authority.body)
    validate_active_transition(previous, authority.body)


def test_new_update_cannot_reserve_a_retired_exact_baseline():
    from test_composition_updates import reserved
    authority = Authority()
    retire_parent(authority)
    apply(authority, 'recovery-point', 'child', 'jat-child')
    previous = copy.deepcopy(authority.body)
    _, pending = reserved()
    pending['child_updates']['update']['plan']['content_digest_algorithm'] = 'sha256-framed-v2'
    pending['child_updates']['update']['plan_sha256'] = plan_digest(pending['child_updates']['update']['plan'])
    authority.body['state'] = pending
    with pytest.raises(ValueError, match='retired'):
        validate_active_transition(previous, authority.body)


def test_old_v1_reader_fails_closed_on_new_envelope():
    authority = Authority()
    retire_parent(authority)
    # The shipped v1 reader requires exactly this version and closed key set.
    assert authority.body['format'] != 'composition-authority-v1'
    assert set(authority.body) != {'format', 'namespace', 'binding', 'request', 'secret',
                                   'repository_id', 'state', 'capture_claims'}


def test_cli_has_separate_preview_and_approval_required_apply():
    from josh_room.cli import build_parser
    command = ['composition', 'retirement-preview', '--dimension', 'synthetic',
               '--namespace', '1' * 32, '--kind', 'catalog', '--json']
    args = build_parser().parse_args(command)
    assert args.composition_command == 'retirement-preview'
    command[1] = 'retire'
    with pytest.raises(SystemExit):
        build_parser().parse_args(command)
    args = build_parser().parse_args([*command, '--approve', 'a' * 64])
    assert args.approve == 'a' * 64


@pytest.mark.parametrize('reply', ['ok', 'lost', 'conflict'])
def test_real_authority_apply_uses_conditional_write_and_exact_readback(tmp_path, monkeypatch, reply):
    authority = Authority()
    authority.private = tmp_path
    authority.recipients = ['synthetic-operational', 'synthetic-recovery']
    authority.bucket = 'synthetic-bucket'
    authority.prefix = 'composition/v1/' + authority.namespace + '/'
    calls = []

    def put_object(**kwargs):
        calls.append(kwargs)
        assert kwargs['IfMatch'] == '1'
        if reply == 'conflict':
            authority.body['request'] = {'concurrent': True}
            authority.etag = '2'
            raise OSError('synthetic conflict')
        authority.body = json.loads(kwargs['Body'])
        authority.etag = '2'
        if reply == 'lost':
            raise OSError('synthetic reply loss')

    authority.client = SimpleNamespace(put_object=put_object)
    monkeypatch.setattr('josh_room.composition_authority.encrypt',
                        lambda raw, _recipients, path: path.write_bytes(raw))
    authority.put = lambda body, etag: CompositionAuthority.put(authority, body, etag)
    if reply == 'conflict':
        with pytest.raises(ValueError, match='unconfirmed'):
            retire_parent(authority)
        assert authority.body['format'] == 'composition-authority-v1'
    else:
        assert retire_parent(authority)['status'] == 'retired'
        assert authority.body['format'] == 'composition-authority-v2'
    assert len(calls) == 1
    assert calls[0]['Key'].endswith('/authority.age')
    assert not (tmp_path / 'authority-write.age').exists()


@pytest.mark.integration
@pytest.mark.skipif(not all(shutil.which(tool) for tool in ('age', 'age-keygen')),
                    reason='requires actual local age and age-keygen')
def test_real_age_encrypted_retirement_over_synthetic_sqlite_transport(tmp_path):
    from test_composition_live_tools import SQLiteS3

    from josh_room.config import DimensionConfig
    from josh_room.crypto import derive_recipient, generate_identity

    state = stages()[-1].to_dict()
    scope = state['scope']
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    identity = generate_identity(private / 'operational.age')
    recovery = generate_identity(private / 'recovery.age')
    material = SimpleNamespace(identity=identity, recipient=derive_recipient(identity),
        encryption_domain_id=scope['encryption_domain_id'],
        keyset=SimpleNamespace(recovery_recipients=(derive_recipient(recovery),)))
    dimension = DimensionConfig(scope['dimension_id'], 'Synthetic Dimension', scope['provider'],
        'https://storage.example.invalid', 'synthetic-bucket', 'synthetic-profile',
        encryption_domain_id=scope['encryption_domain_id'], connection_id=scope['connection_id'])
    transport = SQLiteS3(tmp_path / 'objects.sqlite')
    try:
        authority = CompositionAuthority(SimpleNamespace(client=transport), dimension, '1' * 32, material, private)
        body = {'format': 'composition-authority-v1', 'namespace': authority.namespace,
            'binding': authority.binding, 'state': state, 'request': {}, 'secret': 'a' * 43,
            'repository_id': scope['repository_id'], 'capture_claims': {}}
        authority.put(body, None)
        retire_parent(authority)
        observed, _ = authority.read()
        assert observed['state'] == state
        assert observed['format'] == 'composition-authority-v2'
        raw, = transport.db.execute('select body from objects').fetchone()
        assert raw.startswith(b'age-encryption.org/v1')
        assert b'composition-authority-v2' not in raw
        assert transport.writes == 2
        record = state['dimension_catalog']['projects']['parent']['snapshots']['jat-parent']
        with pytest.raises(ValueError, match='retired'):
            authority.descriptor(record)
    finally:
        transport.db.close()
