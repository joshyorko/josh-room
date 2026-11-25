"""Actual Restic/age update+refresh; durable synthetic S3 transport only."""
import copy
import os
import shutil
import subprocess

import pytest
from test_composition_live_tools import NAMESPACE, save

from josh_room.composition_lifecycle import hydrate_composition
from josh_room.composition_updates import refresh_parent, save_child
from josh_room.workspace_state import context_status

pytest_plugins = ('test_composition_live_tools',)
pytestmark = pytest.mark.integration


def test_source_only_multi_repo_capture_excludes_child_git_history(runtime):
    if shutil.which('git') is None:
        pytest.skip('requires real Git for synthetic multi-repository capture')
    r = runtime
    template = r.private / 'empty-git-template'
    template.mkdir()
    env = {**{key: value for key, value in os.environ.items() if not key.startswith('GIT_')},
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_CONFIG_COUNT': '0', 'GIT_TEMPLATE_DIR': str(template),
           'GIT_AUTHOR_NAME': 'Synthetic', 'GIT_COMMITTER_NAME': 'Synthetic',
           'GIT_AUTHOR_EMAIL': 'synthetic@example.invalid', 'GIT_COMMITTER_EMAIL': 'synthetic@example.invalid'}
    for name in ('a', 'b'):
        repo = r.source / name
        (repo / '.josh-roomignore').write_text('# josh-room capture-mode: source-only\n')
        subprocess.run(['git', '-C', str(repo), 'init', '-q'], env=env, check=True)
        (repo / 'past.txt').write_text('synthetic history-only bytes')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], env=env, check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'synthetic history'], env=env, check=True)
        (repo / 'past.txt').unlink()
    result = save(r)
    envelope, _ = r.authority.read()
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        for project in envelope['state']['dimension_catalog']['projects'].values():
            record = project['snapshots'][project['latest']]
            entries = list(store.entries(record['workspace_snapshot_id']))
            assert not any('.git' in entry.path.split('/') or entry.path.endswith('past.txt') for entry in entries)
            if record['snapshot_id'] == result['parent_snapshot_id']:
                assert not any(entry.path in ('a', 'b') or entry.path.startswith(('a/', 'b/')) for entry in entries)
    assert (r.source / 'a/.git').is_dir() and (r.source / 'b/.git').is_dir()


def entered(r):
    result = save(r)
    target = r.tmp / 'parent'
    hydrate_composition(r.dimension, NAMESPACE, 'parent', result['parent_snapshot_id'], target, r.authority, r.factory, r.private)
    child = hydrate_composition(r.dimension, NAMESPACE, 'parent', result['parent_snapshot_id'], target, r.authority, r.factory, r.private, child_path='a')
    return result['parent_snapshot_id'], target, child


def test_whole_catalog_save_publishes_root_and_edited_child_together(runtime):
    from josh_room.composition_updates import save_catalog_workspace

    r = runtime
    baseline, root, child = entered(r)
    (root / 'new-root.txt').write_text('new root payload')
    (root / 'a/new-child.txt').write_text('new child payload')
    args = (root, r.dimension, NAMESPACE, 'parent', baseline, 'full-save', r.authority, r.factory, r.private)
    review = save_catalog_workspace(*args)
    assert review['children_to_update'] == ['a']
    prior_writes = r.transport.writes
    (root / 'a/new-child.txt').write_text('changed after review')
    with pytest.raises(ValueError, match='approval is stale'):
        save_catalog_workspace(*args, approval=review['approval_sha256'])
    assert r.transport.writes == prior_writes
    (root / 'a/new-child.txt').write_text('new child payload')
    review = save_catalog_workspace(*args)
    updated = save_catalog_workspace(*args, approval=review['approval_sha256'])
    assert updated['marker_verified']
    assert context_status(root)['snapshot_id'] == updated['snapshot_id']
    changed = context_status(root / 'a')
    assert changed['snapshot_id'] != child['snapshot_id']
    cold = r.tmp / 'whole-restored'
    hydrate_composition(r.dimension, NAMESPACE, 'parent', updated['snapshot_id'], cold,
                        r.authority, r.factory, r.private)
    entered_child = hydrate_composition(r.dimension, NAMESPACE, 'parent', updated['snapshot_id'], cold,
                                        r.authority, r.factory, r.private, child_path='a')
    assert entered_child['snapshot_id'] == changed['snapshot_id']
    assert (cold / 'new-root.txt').read_text() == 'new root payload'
    assert (cold / 'a/new-child.txt').read_text() == 'new child payload'
    assert not (cold / 'b').exists()


def test_unchanged_catalog_save_rebinds_root_without_publishing(runtime):
    from josh_room.composition_updates import save_catalog_workspace
    from josh_room.workspace_state import local_status

    r = runtime
    result = save(r)
    baseline = result['parent_snapshot_id']
    root = r.tmp / 'unchanged-catalog'
    hydrate_composition(r.dimension, NAMESPACE, 'parent', baseline, root,
                        r.authority, r.factory, r.private)
    hydrate_composition(r.dimension, NAMESPACE, "parent", baseline, root,
                        r.authority, r.factory, r.private, child_path="a")
    unchanged_child = root / "a/file.txt"
    unchanged_child.write_bytes(unchanged_child.read_bytes())
    assert local_status(root / "a")["state"] == "changed"
    (root / 'notes.txt').write_text('parent note\n')
    assert local_status(root)['state'] == 'changed'
    args = (root, r.dimension, NAMESPACE, 'parent', baseline, 'rebind', r.authority, r.factory, r.private)
    writes = r.transport.writes
    review = save_catalog_workspace(*args)
    assert not review['changed']
    assert save_catalog_workspace(*args, approval=review['approval_sha256'])['status'] == 'unchanged'
    assert r.transport.writes == writes
    assert local_status(root)['state'] == 'clean'
    assert local_status(root / 'a')['state'] == 'clean'


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
    def provider(*_args, **_kwargs):
        yield r.authority, r.factory, r.private
    monkeypatch.setattr(composition_lifecycle, 'provider_runtime', provider)
    monkeypatch.setattr(cli, '_effective_dimension', lambda _: r.dimension)
    def invoke(command, approval=None, pin=None):
        args = ['composition', command, '--dimension', 'dimension', '--namespace', NAMESPACE,
                '--operation-id', 'refresh' if pin else 'update']
        args += ['--parent-room', 'parent', '--parent-snapshot', parent, '--workspace', str(target), '--set-child', 'a=' + pin] if pin else [
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
    def choices(*extra):
        parsed = cli.build_parser().parse_args(['composition', 'parent-children', '--workspace', str(target),
            '--parent-room', 'parent', '--parent-snapshot', parent, '--namespace', NAMESPACE, '--dimension', 'dimension', *extra])
        return cli.dispatch(parsed, r.tmp / 'instance')
    available = choices()
    assert available['current_parent']
    row = next(row for row in available['children'] if row['path'] == 'a')
    assert row['snapshot_id'] == child['snapshot_id']
    assert row['other_point_count'] == 1
    history_page = choices('--child-path', 'a', '--revision', str(available['revision']))
    assert [point['snapshot_id'] for point in history_page['recovery_points']] == [saved['snapshot_id']]
    assert r.transport.writes == writes
    plan = invoke('plan-parent-refresh', pin=saved['snapshot_id'])
    latest = invoke('refresh-parent-pins', pin=saved['snapshot_id'], approval=plan['approval_sha256'])
    assert latest['snapshot_id'] != parent and not latest['workspace_modified']
    assert context_status(target)['snapshot_id'] == parent
    assert not choices()['current_parent']
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


def test_future_created_inherited_exclusions_survive_child_update(runtime):
    r = runtime
    (r.source / '.josh-roomignore').write_text('a/reports/**\n')
    _, target, child = entered(r)
    (target / 'a/reports').mkdir()
    (target / 'a/reports/private.txt').write_text('synthetic never-approved report')
    noop = update(r, target, child, operation='excluded-only')
    assert not noop['changed']
    assert update(r, target, child, operation='excluded-only', approval=noop['approval_sha256'])['status'] == 'unchanged'
    (target / 'a/file.txt').write_text('approved changed source')
    plan = update(r, target, child)
    result = update(r, target, child, approval=plan['approval_sha256'])
    envelope, _ = r.authority.read()
    record = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots'][result['snapshot_id']]
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        entries = list(store.entries(record['workspace_snapshot_id']))
        assert not any(e.path == 'reports' or e.path.startswith('reports/') for e in entries)
        proof = r.private / 'future-rule-proof'
        store.restore(record['workspace_snapshot_id'], proof)
    assert (proof / 'file.txt').read_text() == 'approved changed source'
    assert not (proof / 'reports').exists()
    assert (target / 'a/reports/private.txt').read_text() == 'synthetic never-approved report'


@pytest.mark.parametrize('initial', ['file', 'directory'])
def test_empty_entry_type_change_is_saved_and_real_restore_preserves_type(runtime, initial):
    r = runtime
    entry = r.source / 'a/empty'
    if initial == 'file':
        entry.touch(mode=0o700)
    else:
        entry.mkdir(mode=0o700)
    _, target, child = entered(r)
    first = update(r, target, child, operation='before-noop')
    assert not first['changed']
    assert update(r, target, child, operation='before-noop', approval=first['approval_sha256'])['status'] == 'unchanged'
    entry = target / 'a/empty'
    if initial == 'file':
        entry.unlink()
        entry.mkdir(mode=0o700)
    else:
        entry.rmdir()
        entry.touch(mode=0o700)
    plan = update(r, target, child)
    assert plan['changed'] and plan['request']['plan']['content_digest_algorithm'] == 'sha256-framed-v2'
    saved = update(r, target, child, approval=plan['approval_sha256'])
    assert saved['status'] == 'saved' and saved['snapshot_id'] != child['snapshot_id']
    now = {**child, 'snapshot_id': saved['snapshot_id']}
    noop = update(r, target, now, operation='after-noop')
    assert not noop['changed']
    assert update(r, target, now, operation='after-noop', approval=noop['approval_sha256'])['status'] == 'unchanged'
    envelope, _ = r.authority.read()
    record = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots'][saved['snapshot_id']]
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        proof = r.private / 'entry-type-proof'
        store.restore(record['workspace_snapshot_id'], proof)
        assert len(store.snapshots()) == 4
    assert (proof / 'empty').is_file() == (initial == 'directory')
    assert (proof / 'empty').is_dir() == (initial == 'file')
    assert (proof / 'empty').stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize('lost_reply', [False, True])
def test_lost_backup_reply_recovers_nonzero_accounting_and_refuses_wrong_parent(runtime, monkeypatch, lost_reply):
    import random
    from dataclasses import replace

    from josh_room.restic_store import ResticStore
    r = runtime
    _, target, child = entered(r)
    (target / 'a/file.txt').write_bytes(random.Random(198).randbytes(65536))
    plan = update(r, target, child)
    original_backup, original_snapshot = ResticStore.backup, ResticStore.snapshot
    captured = []
    class ProcessLoss(BaseException):
        pass
    def backup(store, *args, **kwargs):
        summary = original_backup(store, *args, **kwargs)
        captured.append(summary)
        if lost_reply:
            raise ProcessLoss()
        return summary
    monkeypatch.setattr(ResticStore, 'backup', backup)
    if lost_reply:
        with pytest.raises(ProcessLoss):
            update(r, target, child, approval=plan['approval_sha256'])
        assert captured[0].data_added > 0 and captured[0].data_added_packed > 0
    def wrong_parent(store, snapshot_id):
        snapshot = original_snapshot(store, snapshot_id)
        if captured and snapshot_id == captured[0].snapshot_id:
            return replace(snapshot, parent_snapshot_id='f' * 64)
        return snapshot
    monkeypatch.setattr(ResticStore, 'snapshot', wrong_parent)
    with pytest.raises(ValueError, match='ancestry'):
        update(r, target, child, approval=plan['approval_sha256'])
    envelope, _ = r.authority.read()
    assert envelope['state']['child_updates']['update']['phase'] == 'reserved'
    assert envelope['state']['dimension_catalog']['projects'][child['room_id']]['latest'] == child['snapshot_id']
    assert context_status(target / 'a')['snapshot_id'] == child['snapshot_id']
    monkeypatch.setattr(ResticStore, 'snapshot', original_snapshot)
    monkeypatch.setattr(ResticStore, 'backup', original_backup)
    result = update(r, target, child, approval=plan['approval_sha256'])
    envelope, _ = r.authority.read()
    record = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots'][result['snapshot_id']]
    descriptor = r.authority.descriptor(record).to_dict()['workspace']
    assert descriptor['data_added'] == captured[0].data_added > 0
    assert descriptor['data_added_packed'] == captured[0].data_added_packed > 0
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        assert descriptor['parent_snapshot_id'] == store.snapshot(descriptor['snapshot_id']).parent_snapshot_id
        assert len(store.snapshots()) == 4


@pytest.mark.parametrize('provenance', [None, 'inherited-rules-v1'])
def test_legacy_lossy_baseline_refuses_fresh_operation_without_any_publication(runtime, monkeypatch, provenance):
    from josh_room import composition_lifecycle

    r = runtime
    (r.source / '.josh-roomignore').write_text('a/reports/**\n')
    original = composition_lifecycle._capture
    def old_capture(*args, **kwargs):
        from josh_room.logical_jat import LogicalJat
        kwargs.pop('inherited_policy', None)
        descriptor, scan = original(*args, **kwargs)
        if provenance:
            body = descriptor.to_dict()
            body['source']['composition_policy'] = provenance
            descriptor = LogicalJat.from_dict(body)
        return descriptor, scan
    # Reproduce the old actual capture format: no rule semantics for paths not
    # present yet, and no authenticated descriptor provenance.
    with monkeypatch.context() as legacy:
        legacy.setattr(composition_lifecycle, '_inherited_exclusions', lambda _policy, _child, *, compact=False: ())
        legacy.setattr(composition_lifecycle, '_capture', old_capture)
        _, target, child = entered(r)
    (target / 'a/reports').mkdir()
    (target / 'a/reports/private.txt').write_text('synthetic must never upload')
    writes = r.transport.writes
    baseline, _ = r.authority.read()
    with pytest.raises(ValueError, match='verified inherited-rule provenance'):
        update(r, target, child, operation='fresh-new-operation')
    # A user-editable comment cannot impersonate authenticated provenance.
    policy = target / 'a/.josh-roomignore'
    policy.write_text(policy.read_text() + '# inherited-rules-v1\n')
    with pytest.raises(ValueError, match='verified inherited-rule provenance'):
        update(r, target, child, operation='another-new-operation')
    current, _ = r.authority.read()
    assert r.transport.writes == writes and current == baseline
    assert context_status(target / 'a')['snapshot_id'] == child['snapshot_id']
    assert (target / 'a/reports/private.txt').read_text() == 'synthetic must never upload'
    with r.factory(current['secret']) as store:
        store.open_existing()
        assert len(store.snapshots()) == 3


def test_verified_provenance_and_hash_rule_survive_fresh_child_save(runtime):
    r = runtime
    (r.source / '.josh-roomignore').write_text('a/#reports/**\n')
    _, target, child = entered(r)
    envelope, _ = r.authority.read()
    records = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots']
    assert r.authority.descriptor(records[child['snapshot_id']]).to_dict()['source']['composition_policy'] == 'inherited-rules-v2'
    (target / 'a/#reports').mkdir()
    (target / 'a/#reports/private.txt').write_text('synthetic must never upload')
    (target / 'a/file.txt').write_text('approved provenance source edit')
    plan = update(r, target, child, operation='fresh-verified')
    result = update(r, target, child, operation='fresh-verified', approval=plan['approval_sha256'])
    envelope, _ = r.authority.read()
    record = envelope['state']['dimension_catalog']['projects'][child['room_id']]['snapshots'][result['snapshot_id']]
    assert r.authority.descriptor(record).to_dict()['source']['composition_policy'] == 'inherited-rules-v2'
    with r.factory(envelope['secret']) as store:
        store.open_existing()
        proof = r.private / 'verified-hash-rule-proof'
        store.restore(record['workspace_snapshot_id'], proof)
        assert len(store.snapshots()) == 4
    assert not (proof / '#reports').exists()
    assert (proof / 'file.txt').read_text() == 'approved provenance source edit'
    assert '\\#reports/**' in (proof / '.josh-roomignore').read_text().splitlines()


def test_marked_policy_edit_refuses_before_staging_and_keeps_fresh_noop_retry_safe(runtime, monkeypatch):
    from josh_room import composition_updates
    r = runtime
    (r.source / '.josh-roomignore').write_text('a/reports/**\n')
    _, target, child = entered(r)
    noop = update(r, target, child, operation='initial-noop')
    assert not noop['changed']
    assert update(r, target, child, operation='initial-noop', approval=noop['approval_sha256'])['status'] == 'unchanged'
    policy = target / 'a/.josh-roomignore'
    original = policy.read_text()
    assert 'reports/**' in original.splitlines()
    policy.write_text('\n'.join(line for line in original.splitlines() if line != 'reports/**') + '\n')
    (target / 'a/reports').mkdir()
    (target / 'a/reports/private.txt').write_text('synthetic never-authorized report')
    writes = r.transport.writes
    prior, _ = r.authority.read()
    views = set(r.private.glob('child-update-view-*'))
    with monkeypatch.context() as refusal:
        refusal.setattr(composition_updates, '_copy_paths', lambda *_args, **_kwargs: pytest.fail('staging must not start'))
        refusal.setattr(r, 'factory', lambda *_: pytest.fail('backup/proof runtime must not open'))
        with pytest.raises(ValueError, match='policy differs from authenticated baseline'):
            update(r, target, child, operation='fresh-after-removing-rule')
    assert r.transport.writes == writes and r.authority.read()[0] == prior
    assert set(r.private.glob('child-update-view-*')) == views
    assert context_status(target / 'a')['snapshot_id'] == child['snapshot_id']
    # Restoring exact policy bytes permits a fresh no-op, not capture of the
    # newly excluded local report. Every future write still binds that policy.
    policy.write_text(original)
    restored = update(r, target, child, operation='restored-policy-noop')
    assert not restored['changed']
    assert update(r, target, child, operation='restored-policy-noop', approval=restored['approval_sha256'])['status'] == 'unchanged'
    assert r.transport.writes == writes
    (target / 'a/file.txt').write_text('approved same-policy edit')
    plan = update(r, target, child, operation='unchanged-policy-update')
    saved = update(r, target, child, operation='unchanged-policy-update', approval=plan['approval_sha256'])
    assert saved['marker_verified']
    assert update(r, target, child, operation='unchanged-policy-update', approval=plan['approval_sha256'])['snapshot_id'] == saved['snapshot_id']
    current, _ = r.authority.read()
    records = current['state']['dimension_catalog']['projects'][child['room_id']]['snapshots']
    old = r.authority.descriptor(records[child['snapshot_id']]).to_dict()
    new = r.authority.descriptor(records[saved['snapshot_id']]).to_dict()
    assert old['capture_policy_sha256'] == new['capture_policy_sha256']
    assert new['source']['composition_policy'] == 'inherited-rules-v2'
    with r.factory(current['secret']) as store:
        store.open_existing()
        assert len(store.snapshots()) == 4
        for record, expected, name in ((old, 'child a original\n', 'old-policy-proof'), (new, 'approved same-policy edit', 'new-policy-proof')):
            proof = r.private / name
            store.restore(record['workspace']['snapshot_id'], proof)
            assert (proof / 'file.txt').read_text() == expected
            assert not (proof / 'reports').exists()


@pytest.mark.parametrize('point', ['capture', 'descriptor'])
def test_policy_change_before_publication_preserves_old_pin_and_verified_payload(runtime, monkeypatch, point):
    from josh_room import composition_updates
    r = runtime
    (r.source / '.josh-roomignore').write_text('a/reports/**\n')
    _, target, child = entered(r)
    (target / 'a/file.txt').write_text('approved unchanged-policy payload')
    plan = update(r, target, child)
    policy = target / 'a/.josh-roomignore'
    def change():
        policy.write_text('\n'.join(line for line in policy.read_text().splitlines() if line != 'reports/**') + '\n')
    original_capture, original_descriptor = composition_updates._capture, r.authority.put_descriptor
    def capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        if point == 'capture':
            change()
        return result
    def descriptor(value):
        result = original_descriptor(value)
        if point == 'descriptor':
            change()
        return result
    monkeypatch.setattr(composition_updates, '_capture', capture)
    monkeypatch.setattr(r.authority, 'put_descriptor', descriptor)
    with pytest.raises(ValueError, match='policy differs from authenticated baseline'):
        update(r, target, child, approval=plan['approval_sha256'])
    current, _ = r.authority.read()
    assert current['state']['dimension_catalog']['projects'][child['room_id']]['latest'] == child['snapshot_id']
    assert current['state']['child_updates']['update']['phase'] == 'reserved'
    assert context_status(target / 'a')['snapshot_id'] == child['snapshot_id']
    with r.factory(current['secret']) as store:
        store.open_existing()
        assert len(store.snapshots()) == 4


@pytest.mark.parametrize("split", [False, True])
def test_preservation_catalog_keeps_nested_repository_and_optional_child(runtime, split):
    from josh_room.composition_lifecycle import capture_plan, save_composition
    from josh_room.composition_updates import save_catalog_workspace
    from scripts.git_recovery_acceptance import git

    r = runtime
    repo = r.source / 'a'
    git(repo, 'init', '-q')
    git(repo, 'add', 'file.txt')
    git(repo, 'commit', '-qm', 'synthetic repository')
    git_head = git(repo, 'rev-parse', 'HEAD').strip()
    (r.source / '.josh-roomignore').write_text('local/**\n')
    rows = [{'path': '.', 'outcome': 'parent'}]
    if split:
        rows.append({'path': 'b', 'outcome': 'child'})
    args = (r.source, r.dimension, NAMESPACE, 'parent', rows)
    preview = capture_plan(*args, preserve_content=True)
    assert preview['request']['parent_git_count'] == 1
    assert preview['child_count'] == int(split)
    result = save_composition(*args, preview['approval_sha256'], r.authority, r.factory, r.private,
                              preserve_content=True)
    root = r.tmp / 'preserved'
    parent = result['parent_snapshot_id']
    hydrate_composition(r.dimension, NAMESPACE, 'parent', parent, root, r.authority, r.factory, r.private)
    assert (root / 'a/file.txt').read_bytes() == (repo / 'file.txt').read_bytes()
    assert git(root / 'a', 'rev-parse', 'HEAD').strip() == git_head
    git(root / 'a', 'fsck', '--full')
    assert (root / 'notes.txt').is_file()
    assert (root / 'b/file.txt').is_file() is not split
    assert not (root / 'local').exists()
    (root / 'a/new.txt').write_text('retained nested edit')
    if split:
        hydrate_composition(r.dimension, NAMESPACE, 'parent', parent, root,
                            r.authority, r.factory, r.private, child_path='b')
        (root / 'b/new.txt').write_text('retained child edit')
    operation = (root, r.dimension, NAMESPACE, 'parent', parent, 'preserved-save',
                 r.authority, r.factory, r.private)
    approval = save_catalog_workspace(*operation)
    saved = save_catalog_workspace(*operation, approval=approval['approval_sha256'])
    restored = r.tmp / 'preserved-new'
    hydrate_composition(r.dimension, NAMESPACE, 'parent', saved['snapshot_id'], restored,
                        r.authority, r.factory, r.private)
    assert git(restored / 'a', 'rev-parse', 'HEAD').strip() == git_head
    assert (restored / 'a/new.txt').read_text() == 'retained nested edit'
    if split:
        hydrate_composition(r.dimension, NAMESPACE, 'parent', saved['snapshot_id'], restored,
                            r.authority, r.factory, r.private, child_path='b')
        assert (restored / 'b/new.txt').read_text() == 'retained child edit'
        assert r.authority.read()[0]['state']['contract'] == 'composition-safety-v5'


@pytest.mark.parametrize("occupancy", ["folder", "file", "symlink", "corrupt", "wrong-binding"])
def test_occupied_child_aborts_catalog_save_without_committing_a_child(runtime, occupancy):
    from josh_room.composition_updates import save_catalog_workspace

    r = runtime
    baseline, root, _ = entered(r)
    (root / "a/new.txt").write_text("must not commit")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "occupancy", r.authority, r.factory, r.private)
    preview = save_catalog_workspace(*args)
    assert preview["retained_missing_children"] == ["b"]
    blocked = root / "b"
    if occupancy == "file":
        blocked.write_text("occupied")
    elif occupancy == "symlink":
        blocked.symlink_to(root / "a", target_is_directory=True)
    else:
        blocked.mkdir()
        if occupancy == "corrupt":
            (blocked / ".josh-room.json").write_text("{invalid")
        elif occupancy == "wrong-binding":
            shutil.copyfile(root / "a/.josh-room.json", blocked / ".josh-room.json")
    writes = r.transport.writes
    for approval in (None, preview["approval_sha256"]):
        with pytest.raises(ValueError, match="occupied"):
            save_catalog_workspace(*args, approval=approval)
    assert r.transport.writes == writes
    envelope, _ = r.authority.read()
    assert not envelope["state"].get("child_updates")
    assert not envelope.get("save_intent")


def test_aggregate_intent_blocks_other_writers_and_survives_child_publication(runtime, monkeypatch):
    from josh_room import composition_updates as updates

    r = runtime
    baseline, root, child = entered(r)
    (root / "a/new.txt").write_text("edited during aggregate Save")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "recover-intent", r.authority, r.factory, r.private)
    approval = updates.save_catalog_workspace(*args)["approval_sha256"]
    original = updates.refresh_parent

    def interrupt(*args, **kwargs):
        if kwargs.get("approval"):
            raise RuntimeError("synthetic interruption before parent publication")
        return original(*args, **kwargs)

    with monkeypatch.context() as crashed:
        crashed.setattr(updates, "refresh_parent", interrupt)
        with pytest.raises(ValueError, match="Catalog Save stopped"):
            updates.save_catalog_workspace(*args, approval=approval)
    pending = updates.inspect_catalog_save(r.authority, NAMESPACE)
    assert pending["status"] == "pending-save"
    assert pending["intent"]["approval_sha256"] == approval
    assert pending["children"][0]["status"] == "committed"
    assert pending["intent"]["retained_missing_children"] == ["b"]
    writes = r.transport.writes
    with pytest.raises(ValueError, match="another Catalog Save intent"):
        save_child(root / "a", r.dimension, NAMESPACE, child["room_id"], pending["children"][0]["snapshot_id"],
                   "other-writer", r.authority, r.factory, r.private, approval="0" * 64)
    assert r.transport.writes == writes
    recovered = updates.save_catalog_workspace(*args, approval=approval)
    assert recovered["marker_verified"]
    assert recovered["retained_missing_children"] == ["b"]
    assert updates.inspect_catalog_save(r.authority, NAMESPACE)["status"] == "no-pending-save"
    envelope, _ = r.authority.read()
    assert len(envelope["state"]["child_updates"]) == 1
    (root / "a/new.txt").write_text("fresh edits after the same operation committed")
    writes = r.transport.writes
    retry = updates.save_catalog_workspace(*args)
    assert retry["approval_sha256"] == approval
    assert retry["changed"] is False
    with pytest.raises(ValueError, match="committed intent"):
        updates.save_catalog_workspace(*args, approval="f" * 64)
    assert r.transport.writes == writes



def test_retiring_latest_parent_and_child_leaves_active_baselines_writable(runtime):
    from josh_room.composition_retirement import preview_retirement, retire
    from josh_room.composition_updates import save_catalog_workspace

    r = runtime
    baseline, root, child = entered(r)
    (root / "notes.txt").write_text("new parent bytes")
    (root / "a/new.txt").write_text("new child bytes")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "first-save", r.authority, r.factory, r.private)
    first = save_catalog_workspace(*args, approval=save_catalog_workspace(*args)["approval_sha256"])
    latest_child = context_status(root / "a")["snapshot_id"]
    for room, point in (("parent", first["snapshot_id"]), (child["room_id"], latest_child)):
        preview = preview_retirement(r.authority, NAMESPACE, "recovery-point", room, point)
        assert preview["can_apply"]
        retire(r.authority, NAMESPACE, "recovery-point", room, point, approved_plan_sha256=preview["approval_sha256"])
    with pytest.raises(ValueError, match="retired"):
        save_child(root / "a", r.dimension, NAMESPACE, child["room_id"], latest_child,
                   "retired", r.authority, r.factory, r.private)
    cold = r.tmp / "active-baselines"
    hydrate_composition(r.dimension, NAMESPACE, "parent", baseline, cold, r.authority, r.factory, r.private)
    hydrate_composition(r.dimension, NAMESPACE, "parent", baseline, cold, r.authority, r.factory, r.private, child_path="a")
    (cold / "notes.txt").write_text("save from retained parent")
    (cold / "a/new.txt").write_text("save from retained child")
    args = (cold, r.dimension, NAMESPACE, "parent", baseline, "second-save", r.authority, r.factory, r.private)
    assert save_catalog_workspace(*args, approval=save_catalog_workspace(*args)["approval_sha256"])["marker_verified"]


def test_completed_parent_releases_intent_before_attempting_local_repair(runtime, monkeypatch):
    from josh_room import composition_updates as updates

    r = runtime
    baseline, root, _ = entered(r)
    (root / "notes.txt").write_text("approved Catalog edit")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "parent-crash", r.authority, r.factory, r.private)
    review = updates.save_catalog_workspace(*args)

    class ProcessLoss(BaseException):
        pass

    with monkeypatch.context() as crashed:
        def interrupt(*args, **kwargs):
            raise ProcessLoss("synthetic loss after remote parent publication")

        crashed.setattr(updates, "_bind_parent_save", interrupt)
        with pytest.raises(ProcessLoss):
            updates.save_catalog_workspace(*args, approval=review["approval_sha256"])

    assert updates.inspect_catalog_save(r.authority, NAMESPACE)["parent_committed"]
    (root / "notes.txt").write_text("new local edit after publication")
    writes = r.transport.writes
    preview = updates.save_catalog_workspace(*args)
    assert preview["approval_sha256"] == review["approval_sha256"]
    assert r.transport.writes == writes
    with pytest.raises(ValueError, match="Local metadata could not be rebound"):
        updates.save_catalog_workspace(*args, approval=review["approval_sha256"])
    assert r.transport.writes == writes + 1
    assert (root / "notes.txt").read_text() == "new local edit after publication"
    assert updates.inspect_catalog_save(r.authority, NAMESPACE)["status"] == "no-pending-save"


def test_occupied_sibling_after_intent_admission_stops_before_first_child_reservation(runtime, monkeypatch):
    from josh_room import composition_updates as updates

    r = runtime
    baseline, root, _ = entered(r)
    (root / "a/new.txt").write_text("reviewed child edit")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "occupied-race", r.authority, r.factory, r.private)
    review = updates.save_catalog_workspace(*args)
    start = updates._start_save

    def occupy(*args, **kwargs):
        result = start(*args, **kwargs)
        (root / "b").mkdir()
        (root / "b/local.txt").write_text("new occupied path")
        return result

    monkeypatch.setattr(updates, "_start_save", occupy)
    with pytest.raises(ValueError, match="Catalog Save stopped"):
        updates.save_catalog_workspace(*args, approval=review["approval_sha256"])
    envelope, _ = r.authority.read()
    assert not envelope["state"].get("child_updates")
    assert (root / "b/local.txt").read_text() == "new occupied path"


def test_root_only_save_checks_missing_child_before_reserving_parent(runtime, monkeypatch):
    from josh_room import composition_updates as updates

    r = runtime
    baseline, root, _ = entered(r)
    (root / "notes.txt").write_text("reviewed root bytes")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "root-occupancy", r.authority, r.factory, r.private)
    approval = updates.save_catalog_workspace(*args)["approval_sha256"]
    refresh = updates.refresh_parent

    def occupy(*args, **kwargs):
        if kwargs.get("approval") is not None:
            (root / "b").mkdir()
        return refresh(*args, **kwargs)

    before, _ = r.authority.read()
    monkeypatch.setattr(updates, "refresh_parent", occupy)
    with pytest.raises(ValueError, match="Catalog Save stopped"):
        updates.save_catalog_workspace(*args, approval=approval)
    after, _ = r.authority.read()
    assert after["state"] == before["state"]
    assert after["capture_claims"] == before["capture_claims"]


def test_two_callers_with_same_ticket_can_finalize_one_parent(runtime, monkeypatch):
    from josh_room import composition_updates as updates

    r = runtime
    baseline, root, _ = entered(r)
    (root / "notes.txt").write_text("reviewed root bytes")
    args = (root, r.dimension, NAMESPACE, "parent", baseline, "terminal-race", r.authority, r.factory, r.private)
    plan = updates.save_catalog_workspace(*args)
    approval = plan["approval_sha256"]
    bind = updates._bind_parent_save

    def finish_second(*args_to_bind, **kwargs):
        bind(*args_to_bind, **kwargs)
        monkeypatch.setattr(updates, "_bind_parent_save", bind)
        retry = updates.save_catalog_workspace(*args, approval=approval)
        assert retry["marker_verified"] and retry["snapshot_id"] == plan["snapshot_id"]

    monkeypatch.setattr(updates, "_bind_parent_save", finish_second)
    result = updates.save_catalog_workspace(*args, approval=approval)
    assert result["marker_verified"]
    authority, _ = r.authority.read()
    assert authority["save_intent"] is None
    assert authority["completed_saves"]["terminal-race"] == approval
