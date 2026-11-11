"""Synthetic coverage regressions for bounded, non-executing onboarding."""
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from josh_room.workspace_init import initialize_workspace
from josh_room.workspace_onboarding import _guide, discover


def component(root, name):
    path = root / name
    path.mkdir(parents=True)
    (path / '.git').mkdir()
    (path / 'package.json').write_text('{"scripts":{"test":"never-execute"}}')
    return path


def reasons(result):
    return {item['code']: item for item in result['coverage']['omissions']}


def test_large_room_prioritizes_evidence_and_keeps_scopes_after_project_cap(tmp_path):
    for index in range(150):
        (tmp_path / f'incidental-{index:03}').mkdir()
    for index in range(36):
        path = component(tmp_path, f'repo-{index:02}')
        (path / 'AGENTS.md').write_text('Instructions for this scope only.')
    late = tmp_path / 'z-suite'
    late.mkdir()
    (late / 'README.md').write_text('# Suite\n\nNested packages live here.')
    component(late, 'important')
    (late / 'important' / 'AGENTS.md').write_text('Nested scope.')
    result = discover(tmp_path)
    coverage = result['coverage']
    assert coverage['complete'] is False
    assert coverage['observed']['directories_visited'] == 128
    assert coverage['observed']['directories_probed'] <= 1 + 128 * 256
    assert coverage['observed']['directory_entries_observed'] <= 128 * 257
    assert coverage['observed']['components_admitted'] == 32
    assert coverage['observed']['components_observed'] >= 38
    assert 'repo-35/AGENTS.md' in result['instructions']
    assert 'z-suite/important/AGENTS.md' in result['instructions']
    assert all(p['repository'] for p in result['projects'])
    assert {'project-cap', 'pending-directory-cap', 'visited-directory-cap'} <= reasons(result).keys()
    assert len(result['warnings']) == len(set(result['warnings']))
    guide = _guide(result).decode()
    assert 'Coverage: partial' in guide
    assert 'Undiscovered instruction scopes remain unknown' in guide
    assert len(guide.split('## Discovery limits')[1].splitlines()) < 18


def test_project_cap_stays_partial_with_empty_queue_and_scopes_survive(tmp_path):
    for index in range(33):
        path = component(tmp_path, f'repo-{index:02}')
        (path / 'AGENTS.md').write_text('Scope')
    result = discover(tmp_path)
    assert result['coverage']['complete'] is False
    assert result['coverage']['observed']['directories_visited'] == 34
    assert len(result['instructions']) == 33
    assert reasons(result)['project-cap']['observed'] == 1


def test_document_budget_deduplicates_cycles_and_counts_additional_targets(tmp_path):
    (tmp_path / 'README.md').write_text('# Room\n\n' + '\n'.join(
        f'[doc](doc-{index:02}.md) [duplicate](./doc-{index:02}.md#section)'
        for index in range(12)))
    for index in range(12):
        (tmp_path / f'doc-{index:02}.md').write_text('# Current\n\n[cycle](README.md)\n')
    result = discover(tmp_path)
    assert len(result['projects'][0]['documents']) == 8
    coverage = result['coverage']
    assert coverage['observed']['documents_read'] == 8
    assert coverage['observed']['additional_document_links_observed'] == 5
    assert reasons(result)['document-cap']['observed'] == 5
    assert len(result['warnings']) == 1


def test_entry_and_depth_truncation_cannot_claim_complete(tmp_path):
    for index in range(270):
        (tmp_path / f'entry-{index:03}.txt').touch()
    result = discover(tmp_path)
    assert result['coverage']['complete'] is False
    assert result['coverage']['enumeration_order_dependent'] is True
    assert result['coverage']['observed']['directory_entries_observed'] == 257
    assert reasons(result)['directory-entry-cap']['observed'] == 1
    deep = tmp_path / 'deep'
    (deep / 'one/two/three/four').mkdir(parents=True)
    (deep / 'one/two/three/four/AGENTS.md').write_text('Not reached')
    result = discover(deep)
    assert 'depth-cap' in reasons(result)
    assert result['instructions'] == []
    assert result['coverage']['complete'] is False


def test_admission_is_stable_under_creation_and_enumeration_permutations(tmp_path, monkeypatch):
    roots = [tmp_path / 'forward', tmp_path / 'reverse']
    names = [f'plain-{i:02}' for i in range(140)] + ['z-scope', 'z-repo']
    for root, order in zip(roots, (names, list(reversed(names)))):
        root.mkdir()
        for name in order:
            (root / name).mkdir()
        (root / 'z-scope/AGENTS.md').write_text('Instructions')
        (root / 'z-repo/package.json').write_text('{}')
    normal = discover(roots[0])
    scandir = os.scandir

    @contextmanager
    def reversed_entries(path):
        with scandir(path) as entries:
            yield iter(reversed(list(entries)))

    monkeypatch.setattr(os, 'scandir', reversed_entries)
    reverse = discover(roots[1])
    assert normal['coverage'] == reverse['coverage']
    assert normal['instructions'] == reverse['instructions'] == ['z-scope/AGENTS.md']
    assert [p['directory'] for p in normal['projects']] == ['z-repo']
    assert [p['directory'] for p in reverse['projects']] == ['z-repo']


def test_unavailable_paths_and_oversize_files_report_partial(tmp_path, monkeypatch):
    import josh_room.workspace_onboarding as onboarding
    blocked = tmp_path / 'blocked'
    blocked.mkdir()
    (tmp_path / 'README.md').write_bytes(b'x' * (64 * 1024 + 1))
    original = onboarding._directory

    def deny(path):
        if Path(path) == blocked:
            raise PermissionError('synthetic denied path')
        return original(path)

    monkeypatch.setattr(onboarding, '_directory', deny)
    result = discover(tmp_path)
    assert {'directory-unavailable', 'file-size-cap'} <= reasons(result).keys()
    assert result['coverage']['complete'] is False
    assert all(len(item['examples']) <= 3 for item in result['coverage']['omissions'])


def test_complete_means_exhausted_supported_scope_not_all_room_files(tmp_path):
    result = initialize_workspace(tmp_path, editor='none')['onboarding']
    assert result['discovery']['complete'] is True
    assert result['discovery']['scope'] == '.'
    assert result['warnings'] == []
    assert result['discovery']['caps']['projects'] == 32


def test_unreadable_document_is_not_silently_absent(tmp_path, monkeypatch):
    import josh_room.workspace_onboarding as onboarding
    (tmp_path / 'README.md').write_text('# Synthetic Room')
    original = onboarding.os.open

    def deny(path, *args, **kwargs):
        if path == 'README.md':
            raise PermissionError('synthetic unreadable document')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(onboarding.os, 'open', deny)
    result = discover(tmp_path)
    assert 'file-unavailable' in reasons(result)


def test_missing_explicit_document_link_reports_unavailable_evidence(tmp_path):
    (tmp_path / 'README.md').write_text('# Room\n\n[missing design](missing.md)')
    result = discover(tmp_path)
    assert result['coverage']['complete'] is False
    assert reasons(result)['file-unavailable']['examples'] == ['missing.md']


@pytest.mark.parametrize('bad_link', ['.aws/notes.md', 'node_modules/notes.md', 'target/notes.md'])
def test_links_do_not_read_excluded_stores(tmp_path, bad_link):
    target = tmp_path / bad_link
    target.parent.mkdir()
    target.write_text('EXCLUDED SYNTHETIC PAYLOAD')
    (tmp_path / 'README.md').write_text(f'# Room\n\n[excluded]({bad_link})')
    assert 'EXCLUDED SYNTHETIC PAYLOAD' not in _guide(discover(tmp_path)).decode()


def test_linked_commands_cannot_belong_to_an_unvisited_repository(tmp_path):
    # The linked repository is beyond the walk's depth cap. Its command must
    # not become a root task just because its README was linked from the root.
    nested = tmp_path / 'one/two/three/four'
    nested.mkdir(parents=True)
    (nested / '.git').write_text('gitdir: synthetic-unused-marker')
    (nested / 'README.md').write_text('# Current\n\n```sh\nnpm test\n```')
    (tmp_path / 'README.md').write_text('# Suite\n\n[component](one/two/three/four/README.md)')
    result = initialize_workspace(tmp_path, files=['tasks.json'])
    assert '.vscode/tasks.json' in result['onboarding']['omitted']
    assert not (tmp_path / '.vscode/tasks.json').exists()
    assert 'one/two/three/four/README.md' in (tmp_path / 'AGENTS.md').read_text()


def test_late_nested_manifest_is_admitted_ahead_of_readme_only_components(tmp_path):
    for index in range(40):
        path = tmp_path / f'doc-{index:02}'
        path.mkdir()
        (path / 'README.md').write_text('# Incidental\n\nDocumentation only.')
    suite = tmp_path / 'z-suite'
    suite.mkdir()
    (suite / 'README.md').write_text('# Suite\n\n[design](api/ARCHITECTURE.md)')
    api = component(suite, 'api')
    (api / 'AGENTS.md').write_text('Rules only for API.')
    (api / 'ARCHITECTURE.md').write_text('# Architecture\n\n## Current\n\nDirect requests.\n\n## Proposed\n\nQueue later.')
    initialize_workspace(tmp_path, files=['tasks.json'])
    guide = (tmp_path / 'AGENTS.md').read_text()
    assert 'Working directory: `z-suite/api`' in guide
    assert '[z-suite/api/AGENTS.md](z-suite/api/AGENTS.md)' in guide
    assert '**Architecture / Proposed**\n> Queue later.' in guide
    tasks = json.loads((tmp_path / '.vscode/tasks.json').read_text())['tasks']
    assert tasks == [{'label': 'z-suite/api: npm run test', 'type': 'shell', 'command': 'npm run test',
                      'options': {'cwd': '${workspaceFolder}/z-suite/api'}, 'problemMatcher': []}]


def test_duplicate_links_at_exact_budget_do_not_make_coverage_partial(tmp_path):
    (tmp_path / 'README.md').write_text('# Room\n\n' + '\n'.join(
        f'[doc](doc-{index}.md)' for index in range(7)))
    for index in range(7):
        (tmp_path / f'doc-{index}.md').write_text('# Doc\n\n[cycle](./README.md#room)')
    result = discover(tmp_path)
    assert result['coverage']['complete'] is True
    assert result['coverage']['observed']['documents_read'] == 8
    assert result['warnings'] == []


def test_depth_three_leaf_can_be_complete(tmp_path):
    (tmp_path / 'one/two/three').mkdir(parents=True)
    assert discover(tmp_path)['coverage']['complete'] is True


def test_examples_and_warning_paths_cannot_inject_markdown_or_control_characters(tmp_path):
    name = 'synthetic\n[unsafe]'
    scope = tmp_path / name
    scope.mkdir()
    (scope / 'README.md').write_bytes(b'x' * (64 * 1024 + 1))
    result = discover(tmp_path)
    examples = reasons(result)['file-size-cap']['examples']
    assert examples == ['synthetic??unsafe?/README.md']
    assert all('\n' not in warning for warning in result['warnings'])


def test_bounded_scan_never_enumerates_beyond_entry_sentinel(tmp_path, monkeypatch):
    scandir = os.scandir

    @contextmanager
    def endless_entries(path):
        with scandir(path) as entries:
            first = next(entries)
        def repeated():
            for _ in range(257):
                yield first
            raise AssertionError('Scanner exceeded the bounded entry sample')
        yield repeated()

    (tmp_path / 'synthetic.txt').touch()
    monkeypatch.setattr(os, 'scandir', endless_entries)
    result = discover(tmp_path)
    assert result['coverage']['observed']['directory_entries_observed'] == 257
    assert result['coverage']['complete'] is False


def test_entry_type_checks_keep_scandir_descriptor_open(tmp_path, monkeypatch):
    import josh_room.workspace_onboarding as onboarding
    if not onboarding._HAS_DIR_FD:
        pytest.skip('descriptor-relative discovery unavailable')
    component(tmp_path, 'repo')
    scandir = os.scandir

    @contextmanager
    def uncached_types(descriptor):
        # Filesystems without d_type need the live directory FD for fstatat.
        class Entry:
            def __init__(self, entry):
                self.entry, self.name = entry, entry.name

            def is_dir(self, *, follow_symlinks):
                os.fstat(descriptor)
                return self.entry.is_dir(follow_symlinks=follow_symlinks)

        with scandir(descriptor) as entries:
            yield (Entry(entry) for entry in entries)

    monkeypatch.setattr(os, 'scandir', uncached_types)
    result = discover(tmp_path)
    assert [p['directory'] for p in result['projects']] == ['repo']
    assert result['coverage']['complete'] is True


def test_root_readme_is_admitted_alongside_32_repositories(tmp_path):
    (tmp_path / 'README.md').write_text('# Room\n\nRoot architecture context.')
    for index in range(32):
        component(tmp_path, f'repo-{index:02}')
    result = discover(tmp_path)
    assert result['projects'][0]['directory'] == '.'
    assert 'Root architecture context.' in _guide(result).decode()


def test_long_guide_prefers_architecture_and_stays_bounded(tmp_path):
    (tmp_path / 'README.md').write_text('# Room\n\n' + '\n\n'.join(f'Prerequisite paragraph {i}.' for i in range(30)) + '\n\n## Architecture\n\nArchitecture map is useful.')
    guide = _guide(discover(tmp_path)).decode()
    assert len(guide.encode()) <= 16 * 1024
    assert 'Architecture map is useful.' in guide
    assert guide.count('Documented commands are evidence') == 1


def test_twelve_paragraphs_have_no_false_truncation_notice():
    from josh_room.workspace_onboarding import _documentation_excerpt
    result = '\n\n'.join(f'Paragraph {i}.' for i in range(12))
    assert 'Excerpt limit reached' not in '\n'.join(_documentation_excerpt(result))
