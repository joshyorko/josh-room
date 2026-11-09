import json
from concurrent.futures import ThreadPoolExecutor

from josh_room.workspace_init import (
    append_exclusions,
    initialize_workspace,
    policy_edit_token,
)


def test_multi_project_evidence_commands_and_scopes(tmp_path):
    api = tmp_path / 'api'
    api.mkdir()
    (api / 'pyproject.toml').write_text('[project]\nname="api"\ndescription="HTTP API for the client"\n[project.scripts]\nserve="api.main:main"\n')
    (api / 'README.md').write_text('# API\n\nServes the browser client. Proposed queue integration is not implemented.\n\n```sh\nuv run pytest\n```\n')
    (api / 'AGENTS.md').write_text('Scoped API rules\n')
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'package.json').write_text(json.dumps({'scripts': {'test': 'vitest run'}}))
    result = initialize_workspace(tmp_path)
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert 'HTTP API for the client' in guide
    assert 'Proposed queue integration is not implemented' in guide
    assert '[api/AGENTS.md](api/AGENTS.md)' in guide
    assert '`uv run pytest` in `api`' in guide
    tasks = json.loads((tmp_path / '.vscode/tasks.json').read_text())['tasks']
    assert any(t['command'] == 'npm run test' and t['options']['cwd'] == '${workspaceFolder}/web' for t in tasks)
    assert result['onboarding']['artifacts']['AGENTS.md'] == 'created'
    assert '.vscode/launch.json' in result['onboarding']['omitted']


def test_none_still_creates_guide_and_preserves_existing_bytes_and_mtime(tmp_path):
    result = initialize_workspace(tmp_path, editor='none')
    assert result['onboarding']['artifacts'] == {'AGENTS.md': 'created'}
    guide = tmp_path / 'AGENTS.md'
    guide.write_text('custom guide\n')
    vscode = tmp_path / '.vscode'
    vscode.mkdir()
    tasks = vscode / 'tasks.json'
    tasks.write_text('// JSONC comment\n{}\n')
    (tmp_path / 'package.json').write_text('{"scripts":{"test":"test-runner"}}')
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (guide, tasks)}
    initialize_workspace(tmp_path)
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == value for p, value in before.items())


def test_exclusion_housekeeping_does_not_generate_onboarding(tmp_path):
    append_exclusions(tmp_path, [], expected_token=policy_edit_token(tmp_path))
    assert (tmp_path / '.josh-roomignore').is_file()
    assert not (tmp_path / 'AGENTS.md').exists()
    assert not (tmp_path / '.vscode').exists()


def test_symlinked_targets_and_external_discovery_are_not_followed(tmp_path):
    root = tmp_path / 'room'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'README.md').write_text('PRIVATE OUTSIDE CONTENT')
    (root / 'external').symlink_to(outside, target_is_directory=True)
    (root / 'AGENTS.md').symlink_to(outside / 'guide')
    (root / '.vscode').symlink_to(outside, target_is_directory=True)
    (root / 'package.json').write_text('{"scripts":{"test":"test-runner"}}')
    result = initialize_workspace(root)
    assert result['onboarding']['artifacts']['AGENTS.md'] == 'unsafe-target'
    assert result['onboarding']['artifacts']['.vscode/tasks.json'] == 'unsafe-target'
    assert not (outside / 'guide').exists()
    assert not (outside / 'tasks.json').exists()


def test_concurrent_initialization_never_overwrites(tmp_path):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: initialize_workspace(tmp_path, editor='none'), range(2)))
    states = [r['onboarding']['artifacts']['AGENTS.md'] for r in results]
    assert sorted(states) == ['already-present', 'created']
    assert (tmp_path / 'AGENTS.md').read_text().startswith('# Room onboarding')


def test_invalid_selection_has_no_side_effects(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        initialize_workspace(tmp_path, files=['../outside'])
    assert list(tmp_path.iterdir()) == []


def test_launch_only_for_local_explicit_node_start_and_selected_files(tmp_path):
    (tmp_path / 'package.json').write_text('{"scripts":{"start":"node app.js"}}')
    (tmp_path / 'app.js').write_text('console.log("synthetic");\n')
    initialize_workspace(tmp_path, files=['launch.json'])
    launch = json.loads((tmp_path / '.vscode/launch.json').read_text())['configurations'][0]
    assert launch['program'] == '${workspaceFolder}/app.js'
    assert not (tmp_path / '.vscode/tasks.json').exists()


def test_absolute_node_entrypoint_is_not_scaffolded(tmp_path):
    (tmp_path / 'package.json').write_text('{"scripts":{"start":"node /outside/private.js"}}')
    result = initialize_workspace(tmp_path)
    assert '.vscode/launch.json' in result['onboarding']['omitted']
    assert not (tmp_path / '.vscode/launch.json').exists()


def test_nonregular_guide_is_preserved_and_symlinked_readme_is_not_read(tmp_path):
    room = tmp_path / 'room'
    room.mkdir()
    (room / 'AGENTS.md').mkdir()
    outside = tmp_path / 'private.md'
    outside.write_text('outside payload must not be read')
    (room / 'README.md').symlink_to(outside)
    result = initialize_workspace(room, editor='none')
    assert result['onboarding']['artifacts']['AGENTS.md'] == 'unsafe-target'
    assert (room / 'AGENTS.md').is_dir()
    (room / 'AGENTS.md').rmdir()
    initialize_workspace(room, editor='none')
    assert 'outside payload' not in (room / 'AGENTS.md').read_text()


def test_directory_swap_cannot_redirect_editor_writes(tmp_path, monkeypatch):
    import os

    import pytest

    if os.mkdir not in os.supports_dir_fd:
        pytest.skip('descriptor-relative directory creation unavailable')
    root, moved, outside = (tmp_path / name for name in ('room', 'moved', 'outside'))
    root.mkdir()
    outside.mkdir()
    (root / 'package.json').write_text('{"scripts":{"test":"test-runner"}}')
    original = os.mkdir

    def swap_directory(path, *args, **kwargs):
        if path == '.vscode' and kwargs.get('dir_fd') is not None:
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'mkdir', swap_directory)
    initialize_workspace(root, files=['tasks.json'])
    assert list(outside.iterdir()) == []
    assert (moved / '.vscode/tasks.json').is_file()
