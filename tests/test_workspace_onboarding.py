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


def test_architecture_retains_section_context_and_later_relationships(tmp_path):
    (tmp_path / 'ARCHITECTURE.md').write_text(
        '# Architecture\n\n'
        '## Current\n\nRequests are served directly by the API.\n\n'
        '## Proposed\n\nRequests will be delegated to a queue worker.\n\n'
        '## Components\n\nThe browser client calls the API over HTTP.\n'
    )
    initialize_workspace(tmp_path, editor='none')
    guide = (tmp_path / 'AGENTS.md').read_text()
    current = guide.index('Current')
    proposed = guide.index('Proposed')
    assert current < guide.index('Requests are served directly') < proposed
    assert proposed < guide.index('Requests will be delegated')
    assert 'The browser client calls the API over HTTP.' in guide


def test_architecture_retains_indented_and_setext_heading_context(tmp_path):
    (tmp_path / 'ARCHITECTURE.md').write_text(
        '# Architecture\n\n## Current\n\nThe API serves requests directly.\n\n'
        '  ## Proposed\n\nA worker may serve requests later.\n\n'
        'Future deployment\n-----------------\n\nA second region is planned.\n'
    )
    initialize_workspace(tmp_path, editor='none')
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert 'Source section: **Architecture / Proposed**\n> A worker' in guide
    assert 'Source section: **Architecture / Future deployment**\n> A second region' in guide


def test_declared_package_manager_applies_to_nested_workspace_tasks(tmp_path):
    (tmp_path / 'package.json').write_text(json.dumps({
        'name': 'synthetic-suite', 'private': True, 'packageManager': 'pnpm@10.0.0',
        'workspaces': ['apps/*'],
    }))
    app = tmp_path / 'apps' / 'client'
    app.mkdir(parents=True)
    (app / 'package.json').write_text(json.dumps({
        'name': 'synthetic-client', 'scripts': {'dev': 'vite', 'test': 'vitest run'},
    }))
    initialize_workspace(tmp_path, files=['tasks.json'])
    tasks = json.loads((tmp_path / '.vscode/tasks.json').read_text())['tasks']
    assert any(t['command'] == 'pnpm run dev'
               and t['options']['cwd'] == '${workspaceFolder}/apps/client' for t in tasks)
    assert not any(t['command'].startswith('npm ') for t in tasks)


def test_documented_working_directory_is_used_for_tasks(tmp_path):
    (tmp_path / 'README.md').write_text(
        '# Synthetic Room\n\nThe API serves the client.\n\n'
        '```sh\ncd services/api\nuv run pytest\n```\n'
    )
    (tmp_path / 'services' / 'api').mkdir(parents=True)
    initialize_workspace(tmp_path, files=['tasks.json'])
    tasks_path = tmp_path / '.vscode/tasks.json'
    assert tasks_path.exists(), 'A documented command with a local cwd must be usable'
    tasks = json.loads(tasks_path.read_text())['tasks']
    assert any(t['command'] == 'uv run pytest'
               and t['options']['cwd'] == '${workspaceFolder}/services/api' for t in tasks)
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert '`uv run pytest` in `services/api`' in guide


def test_documented_external_working_directory_is_not_scaffolded(tmp_path):
    (tmp_path / 'README.md').write_text(
        '# Synthetic Room\n\n```sh\ncd ../outside\nuv run pytest\n```\n'
    )
    initialize_workspace(tmp_path, files=['tasks.json'])
    assert not (tmp_path / '.vscode/tasks.json').exists()


def test_linked_local_architecture_is_used_without_following_external_links(tmp_path):
    (tmp_path / 'README.md').write_text(
        '# Synthetic Suite\n\nSee [system design](docs/system-design.md).\n'
        'See [external](https://example.invalid/private.md).\n'
    )
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'system-design.md').write_text(
        '# Current architecture\n\nThe browser calls the API over HTTP.\n\n'
        '```sh\npnpm run test\n```\n'
    )
    initialize_workspace(tmp_path, files=['tasks.json'])
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert 'The browser calls the API over HTTP.' in guide
    assert '[docs/system-design.md](docs/system-design.md)' in guide
    tasks = json.loads((tmp_path / '.vscode/tasks.json').read_text())['tasks']
    assert any(t['command'] == 'pnpm run test' for t in tasks)


def test_repository_boundary_without_supported_manifest_is_retained(tmp_path):
    service = tmp_path / 'service'
    service.mkdir()
    (service / '.git').mkdir()
    (service / 'AGENTS.md').write_text('Service instructions.\n')
    initialize_workspace(tmp_path, editor='none')
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert 'Working directory: `service`' in guide
    assert 'Repository boundary: local .git marker present' in guide
    assert 'Development commands: unknown' in guide


def test_linked_documentation_does_not_read_escaped_or_symlinked_files(tmp_path):
    root = tmp_path / 'room'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('OUTSIDE SYNTHETIC PAYLOAD')
    (root / 'linked.md').symlink_to(outside)
    (root / 'README.md').write_text(
        '# Synthetic Room\n\n[escape](../outside.md)\n'
        '[symlink](linked.md)\n[malformed](https://[broken)\n[nul](bad%00.md)\n'
    )
    initialize_workspace(root, editor='none')
    assert 'OUTSIDE SYNTHETIC PAYLOAD' not in (root / 'AGENTS.md').read_text()


def test_linked_repository_readme_keeps_commands_in_its_own_scope(tmp_path):
    (tmp_path / 'README.md').write_text('# Suite\n\nSee [service](service/README.md).\n')
    service = tmp_path / 'service'
    service.mkdir()
    (service / '.git').mkdir()
    (service / 'README.md').write_text('# Service\n\n```sh\nnpm test\n```\n')
    initialize_workspace(tmp_path, files=['tasks.json'])
    tasks = json.loads((tmp_path / '.vscode/tasks.json').read_text())['tasks']
    assert len(tasks) == 1
    assert tasks[0]['command'] == 'npm test'
    assert tasks[0]['options']['cwd'] == '${workspaceFolder}/service'


def test_actual_cli_initializes_selected_source_without_executing_project_code(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = tmp_path / 'selected'
    root.mkdir()
    (root / 'package.json').write_text(json.dumps({
        'packageManager': 'pnpm@10.0.0', 'scripts': {'test': 'touch SHOULD-NOT-RUN'},
    }))
    (root / 'ARCHITECTURE.md').write_text(
        '# Architecture\n\n## Current\n\nThe client calls the API.\n\n'
        '## Proposed\n\nA queue may replace synchronous requests.\n'
    )
    env = {**os.environ, 'JOSH_ROOM_CONFIG_DIR': str(tmp_path / 'private-config'),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    command = [sys.executable, '-m', 'josh_room', 'workspace', 'init',
               '--workspace', str(root), '--json']
    first = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    onboarding = json.loads(first.stdout)['onboarding']
    assert onboarding['artifacts']['AGENTS.md'] == 'created'
    assert onboarding['discovery']['complete'] is True
    assert onboarding['discovery']['observed']['components_admitted'] == 1
    assert onboarding['discovery']['caps']['documents_per_component'] == 8
    assert onboarding['warnings'] == []
    guide = (root / 'AGENTS.md').read_text()
    assert 'Current' in guide and 'Proposed' in guide and 'The client calls the API.' in guide
    tasks = json.loads((root / '.vscode/tasks.json').read_text())['tasks']
    assert tasks[0]['command'] == 'pnpm run test'
    assert tasks[0]['options']['cwd'] == '${workspaceFolder}'
    files = [root / 'AGENTS.md', root / '.vscode/tasks.json', root / '.josh-roomignore']
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    second = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    assert json.loads(second.stdout)['onboarding']['artifacts']['AGENTS.md'] == 'already-present'
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
    assert not (root / 'SHOULD-NOT-RUN').exists()


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
