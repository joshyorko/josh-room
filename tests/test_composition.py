"""Synthetic offline composition proof; never accesses a provider."""
import copy
import json
from pathlib import Path

import pytest

from josh_room.composition import (
    discover,
    parse_manifest,
    plan_split,
    validate_manifest,
)
from josh_room.workspace_policy import CapturePolicy, _parts


def manifest():
    return {"format": "room-composition-v1", "children": [
        {"path": "projects/service-a", "provider": "minio", "connection_id": "connection-a",
         "dimension_id": "dimension-a", "room_id": "room-a", "snapshot_id": "jat-1", "hydration": "explicit"}]}


def test_schema_and_parser_agree():
    import jsonschema
    schema = json.loads(Path("schemas/room-composition-v1.schema.json").read_text())
    body = manifest()
    jsonschema.validate(body, schema)
    assert parse_manifest(json.dumps(body).encode()) == body
    for field, value in [("format", "room-composition-v2"), ("unexpected", True)]:
        bad = {**body, field: value}
        with pytest.raises(ValueError):
            validate_manifest(bad)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a//b", "a/./b", "C:/child", "a\\b",
    "a/*", "a?", "a[bc]", "a!", ".git/worktrees", ".josh-room.json", "a/CON.txt", "a/nul",
    "a/trailing.", "a/ trailing", "a/line\n", "e\u0301", "a/#comment"])
def test_paths_reject_untrusted_or_nonportable_names(path):
    body = manifest()
    body["children"][0]["path"] = path
    with pytest.raises(ValueError):
        validate_manifest(body)


@pytest.mark.parametrize("other", ["projects/service-a", "Projects/service-b", "projects/service-a/nested", "projects", "projects/SERVICE-A"])
def test_overlap_and_ancestor_aliases_rejected(other):
    body = manifest()
    body["children"].append({**body["children"][0], "path": other, "room_id": "room-b"})
    with pytest.raises(ValueError):
        validate_manifest(body)


@pytest.mark.parametrize("field,value", [("snapshot_id", "latest"), ("snapshot_id", "LATEST"), ("room_id", "https://example.invalid"),
    ("provider", "custom"), ("hydration", "automatic"), ("connection_id", "../secret"), ("secret", "sentinel")])
def test_closed_references(field, value):
    body = manifest()
    body["children"][0][field] = value
    with pytest.raises(ValueError):
        parse_manifest(json.dumps(body).encode())


def test_parser_bounds_duplicate_fields_and_redacts_errors():
    for raw in [b'{"format":"secret","format":"room-composition-v1","children":[]}', b'x' * 65537, b'\xff', b'[' * 5000]:
        with pytest.raises(ValueError, match="^invalid composition manifest"):
            parse_manifest(raw)
    body = manifest()
    body['children'].append(copy.deepcopy(body['children'][0]))
    body['children'][1]['path'] = 'other'
    with pytest.raises(ValueError, match="duplicate composition child identity"):
        validate_manifest(body)


def test_read_only_selection_sparse_payload_and_exact_exclusion(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / 'loose').mkdir()
    with (tmp_path / 'huge').open('wb') as stream:
        stream.truncate(8 * 1024 ** 3)
    (tmp_path / '.josh-roomignore').write_bytes(b'# keep original\ncache/**\n')
    before = (tmp_path / '.josh-roomignore').read_bytes()
    # After fixture construction, any attempt to read payloads fails this test.
    original = Path.open
    def guarded(path, *args, **kwargs):
        assert path.name != 'huge'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    result = plan_split(tmp_path, ['a', 'c'])
    assert result['can_apply'] is False and result['writes_performed'] is False
    assert result['coverage']['payload_bytes_read'] == 0
    assert result['coverage']['large_files_observed'] == 1
    assert result['coverage']['complete'] is True
    assert [row['path'] for row in result['selected']] == ['a', 'c']
    assert (tmp_path / '.josh-roomignore').read_bytes() == before
    assert not (tmp_path / '.josh-room-composition.json').exists()
    policy = CapturePolicy('synthetic', tuple(_parts(row['proposed_exclusion']) for row in result['selected']), None)
    assert policy.is_excluded('a') and policy.is_excluded('a/payload')
    assert policy.is_excluded('c/.git/objects/payload')
    assert not policy.is_excluded('b/payload')
    assert not policy.is_excluded('other/a/payload')
    assert not policy.is_excluded('.josh-room-composition.json')


def test_bounded_partial_discovery_and_pruning(tmp_path):
    for index in range(280):
        (tmp_path / f'folder-{index:03}').mkdir()
    result = discover(tmp_path)
    coverage = result['coverage']
    assert not coverage['complete']
    assert coverage['directories_visited'] <= 128
    assert coverage['entries_observed'] <= 128 * 257
    assert {'entry-cap', 'directory-cap'} <= set(coverage['omissions'])


def test_unsupported_boundaries_and_no_external_traversal(tmp_path):
    (tmp_path / 'pointer').mkdir()
    (tmp_path / 'pointer/.git').write_text('gitdir: /never/read')
    (tmp_path / 'bound').mkdir()
    (tmp_path / 'bound/.josh-room.json').write_text('not a trusted marker')
    (tmp_path / 'linked').symlink_to('/never/read', target_is_directory=True)
    (tmp_path / 'a/b/c/d').mkdir(parents=True)
    result = discover(tmp_path)
    assert {'linked-entry', 'depth-cap'} <= set(result['coverage']['omissions'])
    assert not any(row['selectable'] for row in result['candidates'] if row['path'] in {'bound', 'pointer'})
    for selected in [['pointer'], ['bound'], ['linked'], ['a', 'a/b']]:
        with pytest.raises(ValueError):
            plan_split(tmp_path, selected)


def test_root_symlinks_and_aliases(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(ValueError):
        discover(tmp_path / 'alias')
    for name in ['A', 'a']:
        (tmp_path / name).mkdir()
    result = discover(tmp_path)
    assert all(not row['selectable'] for row in result['candidates'] if row['path'] in {'A', 'a'})


def test_cli_offline_no_auth_config_or_network(tmp_path, monkeypatch, capsys):
    from josh_room import cli
    def forbidden(*args, **kwargs):
        pytest.fail('offline planner attempted runtime or auth setup')
    for name in ['initialize_system_trust', '_instance_root', 'load_runtime_session', 'private_config']:
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.main(['composition', 'plan', '--workspace', str(tmp_path), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['can_apply'] is False
    assert not list(tmp_path.iterdir())
