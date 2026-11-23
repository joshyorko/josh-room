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


@pytest.mark.parametrize('changed', [False, True])
def test_lost_component_backup_reply_reuses_only_exact_reserved_bytes(tmp_path, changed):
    import shutil

    from josh_room.composition_components import _CaptureStore
    from josh_room.restic_store import SnapshotEntry

    stage = tmp_path / 'stage'
    stage.mkdir()
    name = 'homebrew-recovery.tar.zst'
    (stage / name).write_bytes(b'approved')
    saved = tmp_path / 'saved'
    snapshot = SimpleNamespace(snapshot_id='a'*64, parent_snapshot_id=None)
    def backup(source, *, parent, cancellation, tags):
        assert tags == ('composition-component:output:homebrew_recovery',)
        shutil.copytree(source, saved)
        raise ConnectionError('lost reply')
    claims = []
    store = SimpleNamespace(
        repository_info=SimpleNamespace(repository_id='b'*64, repository_format=2),
        snapshots=lambda **_: [snapshot] if saved.exists() else [],
        backup=backup, snapshot=lambda _: snapshot,
        entries=lambda _: [SnapshotEntry(name, 'file', 8, 0o644, None)],
        restore=lambda _, target: shutil.copytree(saved, target),
    )
    opened = _CaptureStore(store, 'output', claims.append, tmp_path)
    with pytest.raises(ConnectionError):
        opened.backup(stage, parent=None, cancellation=None)
    if changed:
        (stage / name).write_bytes(b'changed!')
        with pytest.raises(ValueError, match='bytes differ'):
            opened.backup(stage, parent=None, cancellation=None)
    else:
        assert opened.backup(stage, parent=None, cancellation=None).snapshot_id == snapshot.snapshot_id
    assert claims == ['homebrew_recovery']
