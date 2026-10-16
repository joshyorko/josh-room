import pytest

from josh_room import jat
from josh_room.workspace_policy import load_capture_policy


def test_room_policy_resolves_literal_roots_without_following_links(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '.josh-roomignore').write_text('generated\n')
    (source / 'generated').mkdir()
    (source / 'generated' / 'python').symlink_to('/unavailable/python')
    (source / 'node_modules').mkdir()
    (source / 'main.py').write_text('selected')
    (source / '.git').mkdir()
    (source / '.git' / 'venv').write_text('history stays')
    assert jat._capture_selection(source, load_capture_policy(source)) == ['generated', 'node_modules']


def test_filtered_handoff_rejects_old_jat_before_build(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    monkeypatch.setattr(jat, '_run_task', lambda *args: pytest.fail('must reject before execution'))
    with pytest.raises(ValueError, match='does not support filtered'):
        jat.run_build(tmp_path, source, tmp_path / 'out', capture_policy=load_capture_policy(source))


def test_filtered_handoff_passes_literal_paths_and_mode(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'node_modules').mkdir()
    models = tmp_path / 'src/jat/models.py'
    models.parent.mkdir(parents=True)
    models.write_text('WORKSPACE_CAPTURE_POLICY_VERSION = 1\n')
    seen = {}
    def run(root, task, request):
        seen.update(request)
        return {'success': True}
    monkeypatch.setattr(jat, '_run_task', run)
    jat.run_build(tmp_path, source, tmp_path / 'out', capture_policy=load_capture_policy(source))
    assert seen['workspace_exclusions'] == ['node_modules']
    assert seen['capture_mode'] == 'full-repository'
