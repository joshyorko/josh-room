"""Source-only captures preserve selected bytes without touching Git storage."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from josh_room.git_workspace import GitWorkspaceError, validate_git_storage
from josh_room.room_store_operations import RoomStoreOperationsError, _scan_workspace
from josh_room.workspace_policy import load_capture_policy

DIRECTIVE = '# josh-room capture-mode: source-only\n'


def test_source_only_worktrees_policy_and_relocated_payload(tmp_path):
    root = tmp_path / 'original'
    root.mkdir()
    (root / '.git').mkdir()
    (root / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    (root / '.git' / 'config').write_text('[core]\n')
    (root / '.git' / 'objects').mkdir()
    (root / '.git' / 'refs').mkdir()
    admin = root / '.git' / 'worktrees' / 'outside'
    admin.mkdir(parents=True)
    (admin / 'gitdir').write_text('/unavailable/external/.git\n')
    linked = root / 'keep-worktree'
    linked.mkdir()
    (linked / '.git').write_text('gitdir: /unavailable/admin\n')
    (linked / 'untracked.txt').write_text('unique worktree data')
    excluded = root / 'omit-worktree'
    excluded.mkdir()
    (excluded / 'private.txt').write_text('excluded by explicit choice')
    for name in ['app.py', 'package.json', 'lockfile', 'settings.conf', 'data.db', 'uploads']:
        (root / name).write_text('selected:' + name)
    with pytest.raises(RoomStoreOperationsError, match='Git'):
        _scan_workspace(root, load_capture_policy(root))
    original_git = (admin / 'gitdir').read_bytes()
    (root / '.josh-roomignore').write_text(DIRECTIVE + 'omit-worktree\n')
    policy = load_capture_policy(root)
    scan = _scan_workspace(root, policy)
    assert 'keep-worktree/untracked.txt' in scan.paths
    assert not any('.git' in path.split('/') for path in scan.paths)
    assert not any(path.startswith('omit-worktree') for path in scan.paths)
    assert (admin / 'gitdir').read_bytes() == original_git
    restored = tmp_path / 'relocated'
    restored.mkdir()
    for relative in sorted(scan.paths):
        source = root / relative
        target = restored / relative
        if source.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    shutil.rmtree(root)  # disposable fixture: original unavailable during validation
    _scan_workspace(restored, None, capture_mode='source-only')
    assert (restored / 'keep-worktree/untracked.txt').read_text() == 'unique worktree data'
    assert (restored / 'data.db').read_text() == 'selected:data.db'
    assert not (restored / '.git').exists()


def test_source_only_retains_filesystem_checks_and_rejects_injected_git(tmp_path):
    (tmp_path / '.josh-roomignore').write_text(DIRECTIVE)
    (tmp_path / 'unsafe').symlink_to('/unavailable/source-only-fixture')
    with pytest.raises(RoomStoreOperationsError, match='symlink'):
        _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    with pytest.raises(GitWorkspaceError, match='contains Git'):
        validate_git_storage(tmp_path, {'.git'}, capture_mode='source-only')


@pytest.mark.parametrize('text', [DIRECTIVE + DIRECTIVE, '# josh-room capture-mode: invalid\n'])
def test_capture_mode_invalid_directives_fail_closed(tmp_path, text):
    (tmp_path / '.josh-roomignore').write_text(text)
    with pytest.raises(ValueError, match='capture mode'):
        load_capture_policy(tmp_path)


def test_capture_mode_python_js_parity_and_fingerprint(tmp_path):
    default = load_capture_policy(tmp_path)
    assert default.capture_mode == 'full-repository'
    (tmp_path / '.josh-roomignore').write_text(DIRECTIVE)
    policy = load_capture_policy(tmp_path)
    assert policy.sha256 != default.sha256
    script = '''const p=require('./vscode-extension/dirty').loadCapturePolicy(process.argv[1]); console.log(JSON.stringify({mode:p.captureMode,sha:p.sha256,git:p.isExcluded('nested/.git/config')}));'''
    result = subprocess.run(['node', '-e', script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'mode': 'source-only', 'sha': policy.sha256, 'git': True}


@pytest.mark.integration
@pytest.mark.skipif(shutil.which('restic') is None, reason='requires pinned restic')
def test_source_only_real_restic_save_and_restore(tmp_path):
    from types import SimpleNamespace

    from josh_room.private_paths import protect_private_directory, secure_private_file, validate_private_directory
    from josh_room.room_store_operations import RoomStoreOperations
    from tests.test_room_store_operations import PASSWORD, _descriptor

    workspace = tmp_path / 'source'
    workspace.mkdir()
    (workspace / '.josh-roomignore').write_text(DIRECTIVE + 'omit-worktree\n')
    (workspace / '.git').write_text('gitdir: /unavailable/admin\n')
    (workspace / 'selected.db').write_bytes(b'selected database bytes')
    (workspace / 'omit-worktree').mkdir()
    (workspace / 'omit-worktree' / 'other.txt').write_text('omit by user choice')
    cache = tmp_path / 'cache'
    private = tmp_path / 'private'
    cache.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    protect_private_directory(cache)
    protect_private_directory(private)
    keyset = SimpleNamespace(room_store=SimpleNamespace(secret=PASSWORD, generation=3, repository_id=None))
    latest = []
    def bind(*args, expected_generation):
        keyset.room_store.repository_id = args[2]
        return keyset
    def publish(descriptor, **kwargs):
        latest.append(descriptor)
    base = _descriptor().to_dict()
    metadata = {key: base[key] for key in ['dimension_id', 'encryption_domain_id', 'room_id', 'components', 'source', 'producer']}
    operations = RoomStoreOperations(
        workspace=workspace, repository=tmp_path / 'restic-repository', cache_dir=cache,
        password_dir=private, dimension=object(), backend=object(),
        ensure_keyset=lambda *_: keyset, bind_repository=bind,
        read_latest=lambda: (latest[-1] if latest else None, 'etag'),
        read_snapshot_entries=lambda _: (), publish_descriptor=publish,
        write_marker=lambda *args, **kwargs: None, descriptor_metadata=metadata,
        secure_private_file=secure_private_file, validate_private_directory=validate_private_directory,
    )
    saved = operations.save()
    assert saved.descriptor.to_dict()['capture_mode'] == 'source-only'
    assert (workspace / '.git').read_text() == 'gitdir: /unavailable/admin\n'
    shutil.rmtree(workspace)  # only the disposable fixture
    restored = tmp_path / 'different-path'
    operations.restore(saved.descriptor, restored, write_restore_marker=lambda *args: None)
    assert (restored / 'selected.db').read_bytes() == b'selected database bytes'
    assert not (restored / '.git').exists()
    assert not (restored / 'omit-worktree').exists()
    with pytest.raises(RoomStoreOperationsError, match='destination must be absent'):
        operations.restore(saved.descriptor, restored, write_restore_marker=lambda *args: None)


def test_descriptor_old_default_and_mode_validation():
    from josh_room.logical_jat import LogicalJat
    from tests.test_room_store_operations import _descriptor
    body = _descriptor().to_dict()
    assert body.get('capture_mode', 'full-repository') == 'full-repository'
    body['capture_mode'] = 'source-only'
    assert LogicalJat.from_dict(body).to_dict()['capture_mode'] == 'source-only'
    body['capture_mode'] = 'unsafe'
    with pytest.raises(ValueError, match='capture mode'):
        LogicalJat.from_dict(body)


def test_policy_change_invalidates_deletion_confirmation(tmp_path):
    from josh_room.room_store_operations import _deletion_token
    (tmp_path / '.josh-roomignore').write_text(DIRECTIVE + '.josh-roomignore\n')
    before = _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    (tmp_path / '.josh-roomignore').write_text(DIRECTIVE + '.josh-roomignore\n# changed policy\n')
    after = _scan_workspace(tmp_path, load_capture_policy(tmp_path))
    assert before.signature == after.signature  # excluded policy file
    assert _deletion_token('parent', ('removed',), before) != _deletion_token('parent', ('removed',), after)


def test_source_only_rejects_retained_bare_git_store(tmp_path):
    (tmp_path / '.josh-roomignore').write_text(DIRECTIVE)
    (tmp_path / 'HEAD').write_text('ref: refs/heads/main')
    (tmp_path / 'config').write_text('[core]\n')
    (tmp_path / 'objects').mkdir()
    with pytest.raises(RoomStoreOperationsError, match='bare Git store'):
        _scan_workspace(tmp_path, load_capture_policy(tmp_path))


def test_source_only_plaintext_export_still_rejects_gitmodules_credentials(tmp_path):
    (tmp_path / '.gitmodules').write_text('[submodule "example"]\nurl = https://user:password@example.invalid/repository\n')
    with pytest.raises(GitWorkspaceError, match='credentials'):
        validate_git_storage(tmp_path, {'.gitmodules'}, capture_mode='source-only', portable_export=True)
