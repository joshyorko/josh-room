import json
import os

import pytest

from josh_room.component_selection import (
    COMPONENTS,
    selection_digest,
    selection_plan,
    selection_request,
)
from josh_room.room_store_homebrew import (
    RoomStoreHomebrewError,
    prepare_recipe_archive,
    recipe_bytes,
)


def request(**changes):
    return {"format_version": 1, "components": {**dict.fromkeys(COMPONENTS, "keep"), **changes}}


@pytest.mark.parametrize('bad', [{}, {'format_version': True, 'components': {}}, request(agent_state='refresh'), request(homebrew_recovery='upload')])
def test_selection_is_closed(bad):
    with pytest.raises(ValueError):
        selection_request(json.dumps(bad))


def test_selection_change_invalidates_digest_and_omit_does_not_mutate_prior():
    prior = {'homebrew_recovery': {'snapshot': 'synthetic'}}
    assert selection_digest(request()) != selection_digest(request(homebrew_recovery='omit'))
    plan = selection_plan(request(homebrew_recovery='omit'), prior)
    assert plan['homebrew_recovery']['state'] == 'excluded'
    assert prior['homebrew_recovery'] == {'snapshot': 'synthetic'}
    assert selection_plan(request(), prior)['homebrew_recovery']['state'] == 'reused'


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="recipe directory capture requires qualified no-follow dirfd support")
def test_recipe_is_inert_bounded_archive(tmp_path):
    import io
    import tarfile

    import zstandard
    source = tmp_path / 'recipe'
    source.mkdir()
    (source / 'Brewfile').write_text('brew "hello"\n')
    output = prepare_recipe_archive(source, tmp_path / 'recovery.tar.zst')
    with output.open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
        data = stream.read()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert archive.getnames() == ['homebrew-recovery', 'homebrew-recovery/Brewfile']
        assert archive.extractfile('homebrew-recovery/Brewfile').read() == b'brew "hello"\n'
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('unsafe', ['symlink', 'hardlink', 'extra', 'oversize'])
def test_recipe_refuses_unapproved_files(tmp_path, unsafe):
    source = tmp_path / 'recipe'
    source.mkdir()
    target = source / 'Brewfile'
    target.write_text('synthetic')
    if unsafe == 'extra':
        (source / 'auth.json').write_text('synthetic')
    elif unsafe == 'symlink':
        target.unlink()
        target.symlink_to(tmp_path / 'outside')
    elif unsafe == 'hardlink':
        (tmp_path / 'alias').hardlink_to(target)
    else:
        target.write_bytes(b'x' * (1024 * 1024 + 1))
    with pytest.raises(RoomStoreHomebrewError):
        recipe_bytes(source)


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="recipe directory capture requires qualified no-follow dirfd support")
def test_plan_token_changes_with_target_prior_snapshot_and_recipe(tmp_path):
    from josh_room.component_selection import plan_token
    recipe = tmp_path / 'recipe'
    recipe.mkdir()
    (recipe / 'Brewfile').write_text('brew "hello"\n')
    options = {'dimension': 'synthetic', 'project': 'room', 'workspace': tmp_path, 'policy_sha256': 'a' * 64, 'directory': recipe}
    initial = plan_token(request(), **options)
    assert initial != plan_token(request(), **{**options, 'dimension': 'other'})
    assert initial != plan_token(request(), **options, prior_snapshot='synthetic-old')
    (recipe / 'Brewfile').write_text('brew "jq"\n')
    assert initial != plan_token(request(), **options)


def test_recipe_directory_fails_closed_without_platform_containment(tmp_path, monkeypatch):
    source = tmp_path / "recipe"
    source.mkdir()
    (source / "Brewfile").write_text("synthetic recipe")
    monkeypatch.setattr(os, "supports_dir_fd", set())
    with pytest.raises(RoomStoreHomebrewError, match="not qualified"):
        recipe_bytes(source)


def test_cli_token_cannot_bypass_selection_via_local_noop():
    from josh_room.cli import _native_capture_options, build_parser
    args = build_parser().parse_args(['snapshot', 'create', 'synthetic-room', '--component-plan-token', 'a' * 64])
    with pytest.raises(ValueError, match='explicit component selection'):
        _native_capture_options(args)


def test_cli_json_preserves_only_public_plan_digest_and_redacts_credentials(capsys):
    from josh_room.cli import emit
    emit({'ok': True, 'component_plan_token': 'a' * 64, 'session_token': 'synthetic-sensitive', 'other_token': 'b' * 64}, True)
    result = json.loads(capsys.readouterr().out)
    assert result['component_plan_token'] == 'a' * 64
    assert result['session_token'] == '<redacted>'
    assert result['other_token'] == '<redacted>'
    emit({'ok': True, 'component_plan_token': 'not-a-public-digest'}, True)
    assert json.loads(capsys.readouterr().out)['component_plan_token'] == '<redacted>'


@pytest.mark.parametrize('malformed', [None, 123, {}, ['a' * 64], 'A' * 64, 'a' * 63, 'a' * 65])
def test_public_plan_digest_exception_rejects_malformed_shapes(malformed):
    from josh_room.cli import _bounded_json_result
    assert _bounded_json_result({'component_plan_token': malformed})['component_plan_token'] == '<redacted>'


def test_public_plan_digest_does_not_weaken_other_secret_keys():
    from josh_room.cli import _bounded_json_result
    keys = ['password', 'access_key_id', 'secret_access_key', 'identity', 'session_token', 'nested_token', 'component_plan_token_other', 'COMPONENT_PLAN_TOKEN', 'component-plan-token']
    result = _bounded_json_result(dict.fromkeys(keys, 'a' * 64))
    assert set(result.values()) == {'<redacted>'}
