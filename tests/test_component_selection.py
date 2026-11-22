import json

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
        assert archive.getnames() == ['homebrew-recovery/Brewfile']
        assert archive.extractfile(archive.getmembers()[0]).read() == b'brew "hello"\n'
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
