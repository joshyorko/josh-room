"""Actual Restic/age update+refresh; durable synthetic S3 transport only."""
import copy

import pytest
from test_composition_live_tools import NAMESPACE, save

from josh_room.composition_lifecycle import hydrate_composition
from josh_room.composition_updates import refresh_parent, save_child
from josh_room.workspace_state import context_status

pytest_plugins = ('test_composition_live_tools',)
pytestmark = pytest.mark.integration


def entered(r):
    result = save(r)
    target = r.tmp / 'parent'
    hydrate_composition(r.dimension, NAMESPACE, 'parent', result['parent_snapshot_id'], target, r.authority, r.factory, r.private)
    child = hydrate_composition(r.dimension, NAMESPACE, 'parent', result['parent_snapshot_id'], target, r.authority, r.factory, r.private, child_path='a')
    return result['parent_snapshot_id'], target, child


def update(r, target, child, operation='update', approval=None):
    return save_child(target / 'a', r.dimension, NAMESPACE, child['room_id'], child['snapshot_id'], operation,
                      r.authority, r.factory, r.private, approval=approval)


def refresh(r, parent, pins, operation='refresh', approval=None):
    return refresh_parent(r.dimension, NAMESPACE, 'parent', parent, operation, pins,
                          r.authority, r.factory, r.private, approval=approval)


def test_remote_commit_reports_saved_pin_without_claiming_changed_source_is_saved(runtime, monkeypatch):
    from josh_room import composition_updates

    r = runtime
    _, target, child = entered(r)
    (target / 'a/file.txt').write_text('approved child update bytes')
    plan = update(r, target, child)
    original = composition_updates._bind_child
    def change_before_binding(*args):
        (target / 'a/file.txt').write_text('later unsaved local edits')
        return original(*args)
    monkeypatch.setattr(composition_updates, '_bind_child', change_before_binding)
    result = update(r, target, child, approval=plan['approval_sha256'])
    assert not result['ok'] and result['snapshot_verified'] and not result['marker_verified']
    assert result['status'] == 'saved-local-binding-unverified'
    assert result['snapshot_id'] == plan['snapshot_id']
    assert context_status(target / 'a')['snapshot_id'] == child['snapshot_id']
    assert (target / 'a/file.txt').read_text() == 'later unsaved local edits'
    envelope, _ = r.authority.read()
    record = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots'][result['snapshot_id']]
    descriptor = r.authority.descriptor(record)
    proof = r.private / 'reported-commit-proof'
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        store.restore(descriptor.to_dict()['workspace']['snapshot_id'], proof)
    assert (proof / 'file.txt').read_text() == 'approved child update bytes'


def test_changed_child_save_and_explicit_refresh_keep_old_parent_exact(runtime, monkeypatch):
    from contextlib import contextmanager

    from josh_room import cli, composition_lifecycle

    r = runtime
    parent, target, child = entered(r)
    @contextmanager
    def provider(*_args):
        yield r.authority, r.factory, r.private
    monkeypatch.setattr(composition_lifecycle, 'provider_runtime', provider)
    monkeypatch.setattr(cli, '_effective_dimension', lambda _: r.dimension)
    def invoke(command, approval=None, pin=None):
        args = ['composition', command, '--dimension', 'dimension', '--namespace', NAMESPACE,
                '--operation-id', 'refresh' if pin else 'update']
        args += ['--parent-room', 'parent', '--parent-snapshot', parent, '--set-child', 'a=' + pin] if pin else [
            '--room', child['room_id'], '--child-snapshot', child['snapshot_id'], '--workspace', str(target / 'a')]
        if approval:
            args += ['--approve', approval]
        return cli.dispatch(cli.build_parser().parse_args(args), r.tmp / 'instance')
    before, _ = r.authority.read()
    history = copy.deepcopy(before['state']['operations'][NAMESPACE])
    (target / 'a/file.txt').write_text('new independent child bytes\n')
    preview = invoke('plan-child-save')
    assert preview['changed']
    saved = invoke('save-child', approval=preview['approval_sha256'])
    assert saved['marker_verified'] and not saved['parent_refreshed']
    assert context_status(target / 'a')['snapshot_id'] == saved['snapshot_id']
    after, _ = r.authority.read()
    assert after['state']['operations'][NAMESPACE] == history
    assert after['state']['dimension_catalog']['projects']['parent']['latest'] == parent
    # Unchanged child Save performs no authority writes or new Restic snapshots.
    now = {**child, 'snapshot_id': saved['snapshot_id']}
    writes = r.transport.writes
    noop = update(r, target, now, operation='noop')
    assert not noop['changed']
    assert update(r, target, now, operation='noop', approval=noop['approval_sha256'])['status'] == 'unchanged'
    assert r.transport.writes == writes
    preview = update(r, target, now, operation='late')
    (target / 'a/late.txt').write_text('not in the approved inventory')
    with pytest.raises(ValueError, match='approval'):
        update(r, target, now, operation='late', approval=preview['approval_sha256'])
    assert r.transport.writes == writes
    (target / 'a/late.txt').unlink()
    same = refresh(r, parent, {'a': child['snapshot_id']}, operation='same-pins')
    assert not same['changed']
    assert refresh(r, parent, {'a': child['snapshot_id']}, operation='same-pins', approval=same['approval_sha256'])['status'] == 'unchanged'
    assert r.transport.writes == writes
    plan = invoke('plan-parent-refresh', pin=saved['snapshot_id'])
    latest = invoke('refresh-parent-pins', pin=saved['snapshot_id'], approval=plan['approval_sha256'])
    assert latest['snapshot_id'] != parent and not latest['workspace_modified']
    assert context_status(target)['snapshot_id'] == parent
    for selected, expected, name in ((parent, 'child a original\n', 'old'), (latest['snapshot_id'], 'new independent child bytes\n', 'new')):
        root = r.tmp / name
        hydrate_composition(r.dimension, NAMESPACE, 'parent', selected, root, r.authority, r.factory, r.private)
        restored = hydrate_composition(r.dimension, NAMESPACE, 'parent', selected, root, r.authority, r.factory, r.private, child_path='a')
        assert (root / 'a/file.txt').read_text() == expected
        assert restored['snapshot_id'] == (child['snapshot_id'] if name == 'old' else saved['snapshot_id'])
        assert not (root / 'b').exists()
    after, _ = r.authority.read()
    assert after['state']['operations'][NAMESPACE] == history
    with r.factory(after['secret']) as store:
        store.open_existing()
        assert len(store.snapshots()) == 5


@pytest.mark.parametrize('point', ['child-snapshot', 'child-claim', 'child-commit', 'child-marker', 'parent-snapshot', 'parent-commit'])
def test_update_and_refresh_crash_retry_reuse_output(runtime, monkeypatch, point):
    class ProcessLoss(BaseException):
        pass
    r = runtime
    parent, target, child = entered(r)
    (target / 'a/file.txt').write_text('new child retry bytes')
    child_plan = update(r, target, child)
    original_descriptor = r.authority.put_descriptor
    original_put = r.authority.put
    from josh_room import composition_updates
    original_marker = composition_updates.write_stat_workspace_marker
    hit = False

    def descriptor(value):
        nonlocal hit
        result = original_descriptor(value)
        if not hit and point.endswith('snapshot') and (value.to_dict()['room_id'] == 'parent') == point.startswith('parent'):
            hit = True
            raise ProcessLoss()
        return result

    def put(body, etag):
        nonlocal hit
        result = original_put(body, etag)
        updates = body['state'].get('child_updates', {})
        op = body['state']['operations'].get('refresh')
        if not hit and ((point == 'child-commit' and updates.get('update', {}).get('phase') == 'committed') or
                (point == 'child-claim' and child_plan['snapshot_id'] in body['capture_claims']) or
                (point == 'parent-commit' and op and op['phase'] == 'committed')):
            hit = True
            raise ProcessLoss()
        return result
    monkeypatch.setattr(r.authority, 'put_descriptor', descriptor)
    monkeypatch.setattr(r.authority, 'put', put)
    def marker(*args, **kwargs):
        nonlocal hit
        if point == 'child-marker' and not hit:
            hit = True
            raise ProcessLoss()
        return original_marker(*args, **kwargs)
    monkeypatch.setattr(composition_updates, 'write_stat_workspace_marker', marker)
    if point.startswith('child'):
        with pytest.raises(ProcessLoss):
            update(r, target, child, approval=child_plan['approval_sha256'])
    if point == 'child-claim':
        with pytest.raises(ValueError, match='claimed'):
            update(r, target, child, approval=child_plan['approval_sha256'])
        envelope, _ = r.authority.read()
        with r.factory(envelope['secret']) as store:
            store.open_existing()
            assert len(store.snapshots()) == 3
        return
    saved = update(r, target, child, approval=child_plan['approval_sha256'])
    pins = {'a': saved['snapshot_id']}
    plan = refresh(r, parent, pins)
    if point.startswith('parent'):
        with pytest.raises(ProcessLoss):
            refresh(r, parent, pins, approval=plan['approval_sha256'])
    result = refresh(r, parent, pins, approval=plan['approval_sha256'])
    assert hit and result['status'] == 'saved'
    envelope, _ = r.authority.read()
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        assert len(store.snapshots()) == 5
