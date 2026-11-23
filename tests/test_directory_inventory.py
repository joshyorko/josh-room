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


def test_source_changes_mid_stream(tmp_path):
    for index in range(300):
        (tmp_path / f'node-{index}').mkdir()
    scan = DirectoryInventory(tmp_path)
    try:
        scan.advance(budget=10)
        (tmp_path / 'late-entry').mkdir()
        assert finish(scan)['status'] == 'stale'
        assert scan.page()['candidates'] == []
    finally:
        scan.close()


def test_inaccessible_scope_keeps_other_choices(tmp_path, monkeypatch):
    (tmp_path / 'blocked').mkdir()
    (tmp_path / 'safe').mkdir()
    scan = DirectoryInventory(tmp_path)
    original = scan._open
    def blocked(path):
        if path == 'blocked':
            raise PermissionError('synthetic')
        return original(path)
    monkeypatch.setattr(scan, '_open', blocked)
    try:
        assert finish(scan)['status'] == 'partial'
        assert [r['path'] for r in all_rows(scan)] == ['safe']
    finally:
        scan.close()


def test_mount_boundary_fails_closed(tmp_path, monkeypatch):
    from josh_room import directory_inventory
    (tmp_path / 'mounted').mkdir()
    scan = DirectoryInventory(tmp_path)
    monkeypatch.setattr(directory_inventory, 'mount_id', lambda fd: scan.mount + 1)
    try:
        assert finish(scan)['status'] == 'partial'
        assert not all_rows(scan)
    finally:
        scan.close()


def test_private_index_refuses_source_containing_tempdir(tmp_path, monkeypatch):
    from josh_room import directory_inventory
    monkeypatch.setattr(directory_inventory.tempfile, 'gettempdir', lambda: str(tmp_path))
    with pytest.raises(ValueError, match='outside source'):
        DirectoryInventory(tmp_path)


def test_entry_failure_never_admits_staged_children(tmp_path, monkeypatch):
    from josh_room import directory_inventory
    (tmp_path / 'child/deep').mkdir(parents=True)
    scan = DirectoryInventory(tmp_path)
    original = directory_inventory.os.scandir
    class FailingStream:
        def __init__(self, fd):
            self.stream = original(fd)
            self.done = False
        def __next__(self):
            if self.done:
                raise OSError('synthetic late iterator failure')
            self.done = True
            return next(self.stream)
        def close(self):
            self.stream.close()
    monkeypatch.setattr(directory_inventory.os, 'scandir', FailingStream)
    try:
        assert finish(scan)['status'] == 'partial'
        assert scan.db.execute("select state from dirs where path='child'").fetchone() == (1,)
        assert all_rows(scan) == []
    finally:
        monkeypatch.setattr(directory_inventory.os, 'scandir', original)
        scan.close()


def test_marked_source_root_browsable_nested_marker_closed(tmp_path):
    (tmp_path / '.josh-room.json').write_text('not read')
    (tmp_path / 'safe/deep').mkdir(parents=True)
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'nested/.josh-room.json').write_text('not read')
    scan = DirectoryInventory(tmp_path)
    try:
        assert finish(scan)['status'] == 'partial'
        assert [r['path'] for r in all_rows(scan)] == ['safe', 'safe/deep']
        assert scan.lookup('safe/deep')['selected'] == 'safe/deep'
    finally:
        scan.close()


def test_undecodable_file_does_not_hide_siblings(tmp_path):
    import os
    (tmp_path / 'safe').mkdir()
    fd = os.open(os.fsencode(tmp_path) + b'/bad-\xff', os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    scan = DirectoryInventory(tmp_path)
    try:
        assert 'undecodable-name' in finish(scan)['omissions']
        assert [r['path'] for r in all_rows(scan)] == ['safe']
    finally:
        scan.close()


def test_large_display_path_has_bounded_id_cursor(tmp_path):
    scan = DirectoryInventory(tmp_path)
    try:
        # Larger than both the legacy protocol and the encoded page budget.
        path = '/'.join(['long-component'] * 5000)
        scan.db.execute('insert into dirs(path,parent,name,alias,kind,cohesive,excluded,state) values(?,?,?,?,?,?,?,?)', (path, '', '', '', 'folder', 0, 0, 2))
        scan.status = 'partial'
        page = scan.page(limit=1)
        row = page['candidates'][0]
        assert row['path_truncated'] and len(row['path']) < 2100
        assert row['id'].isdigit()
        assert len(json.dumps(page).encode()) < 65536
    finally:
        scan.close()


def test_recoverable_lookup_does_not_close_session(tmp_path):
    import io

    from josh_room.directory_inventory import serve
    (tmp_path / 'safe').mkdir()
    requests = [{'action': 'advance'}] * 4 + [{'action': 'lookup', 'path': 'typo'}, {'action': 'lookup', 'path': 'safe'}, {'action': 'close'}]
    output = io.StringIO()
    serve(tmp_path, io.StringIO(''.join(json.dumps(r) + '\n' for r in requests)), output)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[-2]['ok'] is False and records[-2]['recoverable'] is True
    assert records[-1]['selected'] == 'safe'
    assert records[-1]['id'].isdigit()



def test_inventory_protocol_page_limit_preserves_all_candidates(tmp_path):
    import io

    from josh_room.directory_inventory import serve

    names = [f"repo-{i:02}" for i in range(59)]
    for name in names:
        (tmp_path / name).mkdir()
    records = []

    def frames():
        while not records or records[-1]["status"] != "complete":
            yield {"action": "advance"}
        yield {"action": "page"}
        assert len(records[-1]["candidates"]) == 50
        for invalid in (0, 51, True, "32"):
            yield {"action": "page", "limit": invalid}
            assert records[-1]["recoverable"]
        yield {"action": "page", "limit": 32}
        first = records[-1]
        assert len(first["candidates"]) == 32
        yield {"action": "page", "limit": 32, "after": first["next"]}
        last = records[-1]
        assert last["next"] is None
        assert [r["path"] for r in first["candidates"] + last["candidates"]] == names
        yield {"action": "close"}

    class Input:
        def __init__(self):
            self.frames = frames()

        def readline(self, _limit):
            return json.dumps(next(self.frames)) + "\n"

    class Output(io.StringIO):
        def write(self, response):
            records.append(json.loads(response))
            return super().write(response)

    serve(tmp_path, Input(), Output())
