"""Synthetic metadata-only exhaustive discovery, no provider use."""
import json
import time
from pathlib import Path

import pytest

from josh_room.directory_inventory import DirectoryInventory


def finish(scan):
    for _ in range(10000):
        result = scan.advance(seconds=1)
        if result['status'] in ('complete', 'partial', 'stale'):
            return result
    pytest.fail('inventory failed to terminate')


def all_rows(scan):
    rows, after = [], ''
    while True:
        page = scan.page(after=after)
        assert len(json.dumps(page).encode()) < 65536
        rows.extend(page['candidates'])
        if page['next'] is None:
            return rows
        after = page['next']


@pytest.mark.parametrize('count', [1000, 10000])
@pytest.mark.parametrize('nested', [False, True])
def test_scale(tmp_path, count, nested):
    for index in range(count):
        # 25-node branches, no recursion and no >20-level discovery ceiling.
        path = tmp_path / (f'branch-{index // 25:05d}/' + '/'.join(f'n{i:02d}' for i in range(index % 25 + 1)) if nested else f'node-{index:05d}')
        path.mkdir(parents=True, exist_ok=True)
    expected = count + (count // 25 if nested else 0)
    before = tmp_path.stat().st_mtime_ns
    start = time.perf_counter()
    scan = DirectoryInventory(tmp_path)
    try:
        assert scan.advance(budget=2)['complete'] is False
        assert scan.advance(cancelled=True)['status'] == 'cancelled'
        assert finish(scan)['status'] == 'complete'
        rows = all_rows(scan)
        assert len(rows) == expected
        paths = [r['path'] for r in rows]
        assert paths == sorted(set(paths))
        assert scan.lookup(paths[-1])['selected'] == paths[-1]
        assert scan.page(query=paths[-1])['candidates'][0]['path'] == paths[-1]
        print(f'synthetic count={count} nested={nested} indexed={len(rows)} seconds={time.perf_counter()-start:.3f} index_bytes={sum(p.stat().st_size for p in Path(scan.scratch.name).iterdir())}')
        assert tmp_path.stat().st_mtime_ns == before
    finally:
        scratch = scan.scratch.name
        scan.close()
        assert not Path(scratch).exists()


def test_cohesive_and_independent_and_sparse(tmp_path, monkeypatch):
    (tmp_path / '.git').mkdir()
    (tmp_path / 'src/deep/manual').mkdir(parents=True)
    (tmp_path / 'wrappers/project/.git').mkdir(parents=True)
    (tmp_path / 'node_modules/cache').mkdir(parents=True)
    with (tmp_path / 'large.bin').open('wb') as stream:
        stream.truncate(8 * 1024**3)
    original = Path.open
    def guarded(path, *args, **kwargs):
        assert path.name != 'large.bin', 'payload read'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guarded)
    scan = DirectoryInventory(tmp_path)
    try:
        assert finish(scan)['payload_bytes_read'] == 0
        rows = all_rows(scan)
        assert [r['path'] for r in rows if r['recommended']] == ['wrappers/project']
        assert scan.page()['workspace_recommendation'] == 'keep-repository-together'
        assert scan.lookup('src/deep/manual')['selected'] == 'src/deep/manual'
        assert any(r['evidence_code'] == 'policy-excluded' for r in rows)
    finally:
        scan.close()


@pytest.mark.parametrize('mutation', ['late', 'policy', 'ancestor'])
def test_stale_manual_choice(tmp_path, mutation):
    (tmp_path / 'parent/child').mkdir(parents=True)
    scan = DirectoryInventory(tmp_path)
    try:
        finish(scan)
        if mutation == 'policy':
            (tmp_path / '.josh-roomignore').write_text('parent/**\n')
        elif mutation == 'late':
            (tmp_path / 'parent/CHILD').mkdir()
        else:
            (tmp_path / 'parent').rename(tmp_path / 'old')
            (tmp_path / 'parent/child').mkdir(parents=True)
        with pytest.raises(ValueError):
            scan.lookup('parent/child')
        assert scan.status == 'stale'
    finally:
        scan.close()


def test_late_alias_guards_and_symlink(tmp_path):
    for index in range(300):
        (tmp_path / f'f{index:03d}').touch()
    (tmp_path / 'Twin/deep').mkdir(parents=True)
    (tmp_path / 'twin').touch()
    (tmp_path / 'guarded').mkdir()
    (tmp_path / 'guarded/.git').write_text('gitdir: elsewhere')
    (tmp_path / 'loop').symlink_to(tmp_path, target_is_directory=True)
    scan = DirectoryInventory(tmp_path)
    try:
        result = finish(scan)
        assert result['status'] == 'partial'
        assert {'path-collision', 'linked-entry', 'git-pointer-unsupported'} <= set(result['omissions'])
        assert not all_rows(scan)
        for path in ['Twin', 'Twin/deep', 'guarded', '../escape', 'loop']:
            with pytest.raises(ValueError):
                scan.lookup(path)
    finally:
        scan.close()
