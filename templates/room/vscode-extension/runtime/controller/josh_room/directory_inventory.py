"""Private, metadata-only Linux directory index. No capture authority.

Directory streams remain open within a session; pause/resume never rescans a
prefix or relies on filesystem enumeration order. Process loss requires restart.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

from .composition import MANIFEST, SPLIT_GUARD, portable_path
from .workspace_onboarding import _directory
from .workspace_policy import load_capture_policy


def mount_id(fd):
    if not sys.platform.startswith("linux"):
        raise ValueError("mount-aware inventory unavailable")
    for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
        if line.startswith("mnt_id:"):
            return int(line.split()[1])
    raise ValueError("mount identity unavailable")


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def discovery_path(path):
    if not isinstance(path, str) or not path:
        raise ValueError('invalid discovery path')
    for part in path.split('/'):
        portable_path(part)


def recommendation(kind, cohesive, excluded):
    """Pure labels, never selection or permission. Stable rank then path."""
    if excluded:
        return (30, 'policy-excluded', 'Keep local only under current capture policy', 'local-only')
    if kind == 'git-directory-unverified':
        return (10, 'git-boundary', 'One cohesive repository candidate; Git storage still unverified', 'optional-child')
    if cohesive:
        return (40, 'cohesive-repository', 'Keep within the enclosing repository by default', 'retain-parent')
    return (50, 'manual-directory', 'Ownership unknown; available for explicit manual choice', 'retain-parent')


class DirectoryInventory:
    """Bounded work and result pages backed by a private temporary SQLite index.

    Public pages are withheld until every sibling alias has been checked and a
    complete metadata revalidation pass finishes. Inaccessible scopes are
    explicit partial coverage. Complete is an observation, not an FS snapshot.
    """
    def __init__(self, root):
        if os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd:
            raise ValueError('safe directory inventory unavailable on this platform')
        root = Path(root)
        if '..' in root.parts:
            raise ValueError('unsafe source')
        self.root = Path(os.path.abspath(root))
        self.fd = _directory(self.root)
        self.device = os.fstat(self.fd).st_dev
        self.mount = mount_id(self.fd)
        scratch_base = Path(tempfile.gettempdir()).resolve()
        if scratch_base == self.root or self.root in scratch_base.parents:
            os.close(self.fd)
            raise ValueError("private index must be outside source")
        self.policy = load_capture_policy(self.root, directory_fd=self.fd)
        self.scratch = tempfile.TemporaryDirectory(prefix='codex-directory-inventory-')
        os.chmod(self.scratch.name, 0o700)
        self.db = sqlite3.connect(str(Path(self.scratch.name) / 'index.sqlite'))
        self.db.execute('pragma cache_size=-1024')
        self.db.execute('create table dirs(path text primary key, parent text, name text, alias text, stamp text, kind text, cohesive integer, excluded integer, state integer default 0)')
        self.db.execute('create index pending on dirs(state,path)')
        self.db.execute('create index siblings on dirs(parent,alias)')
        self.db.execute('create table names(parent text, alias text, name text, primary key(parent,name))')
        self.db.execute('create index aliases on names(parent,alias)')
        self.db.execute('create table omissions(code text primary key)')
        self.db.execute('insert into dirs(path,parent,name,alias,cohesive,excluded) values(?,?,?,?,?,?)', ('', '', '', '', 0, 0))
        self.active = None
        self.generation = uuid.uuid4().hex
        self.status = 'scanning'
        self.observed = 0
        self.validated = ''
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._close_active()
        self.db.close()
        os.close(self.fd)
        self.scratch.cleanup()

    def _close_active(self):
        if self.active:
            _, fd, iterator, _ = self.active
            iterator.close()
            os.close(fd)
            self.active = None

    def _omit(self, code):
        self.db.execute('insert or ignore into omissions values(?)', (code,))

    def _open(self, path):
        fd = os.dup(self.fd)
        try:
            for part in path.split('/') if path else ():
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != self.device or mount_id(fd) != self.mount:
                    raise ValueError('filesystem-boundary')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _kind(self, fd):
        kind = 'folder'
        for name in ('.git', '.josh-room.json', MANIFEST, SPLIT_GUARD):
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if name != '.git':
                return 'reserved-boundary'
            kind = 'git-directory-unverified' if stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400 else 'git-pointer-unsupported'
        return kind

    def _source_current(self):
        fd = _directory(self.root)
        try:
            if stamp(os.fstat(fd)) != stamp(os.fstat(self.fd)):
                return False
            return load_capture_policy(self.root, directory_fd=self.fd).sha256 == self.policy.sha256
        finally:
            os.close(fd)

    def advance(self, budget=256, seconds=0.1, cancelled=False):
        if cancelled:
            self.status = 'cancelled'
            return self.progress()
        if self.status == 'cancelled':
            self.status = 'scanning'
        if self.status in ('stale', 'complete', 'partial'):
            return self.progress()
        if not self._source_current():
            self.status = 'stale'
            return self.progress()
        deadline = time.monotonic() + min(max(seconds, 0.001), 1)
        for _ in range(min(max(budget, 1), 1024)):
            if time.monotonic() >= deadline:
                break
            if self.status == 'validating':
                row = self.db.execute('select path,stamp from dirs where state=2 and path>=? order by path limit 1', (self.validated,)).fetchone()
                if not row:
                    self.status = 'partial' if self.db.execute('select 1 from omissions limit 1').fetchone() else 'complete'
                    break
                path, expected = row
                self.validated = path + '\x00'
                try:
                    fd = self._open(path)
                    try:
                        if json.dumps(stamp(os.fstat(fd))) != expected:
                            self.status = 'stale'
                            break
                    finally:
                        os.close(fd)
                except (OSError, ValueError):
                    self.status = 'stale'
                    break
                continue
            if self.active is None:
                row = self.db.execute('select path,cohesive from dirs where state=0 order by path limit 1').fetchone()
                if row is None:
                    self.status = 'validating'
                    continue
                path, cohesive = row
                try:
                    fd = self._open(path)
                    queued = self.db.execute('select stamp from dirs where path=?', (path,)).fetchone()[0]
                    if queued and queued != json.dumps(stamp(os.fstat(fd))):
                        os.close(fd)
                        self.status = 'stale'
                        break
                    kind = self._kind(fd)
                    expected = stamp(os.fstat(fd))
                    self.db.execute('update dirs set stamp=?,kind=? where path=?', (json.dumps(expected), kind, path))
                    if kind in ('reserved-boundary', 'git-pointer-unsupported'):
                        self._omit(kind)
                        self.db.execute('update dirs set state=3 where path=?', (path,))
                        os.close(fd)
                        continue
                    self.active = (path, fd, os.scandir(fd), bool(cohesive or kind == 'git-directory-unverified'))
                except (OSError, ValueError):
                    self._omit('inaccessible-or-filesystem-boundary')
                    self.db.execute('update dirs set state=3 where path=?', (path,))
                    continue
            path, fd, iterator, cohesive = self.active
            try:
                entry = next(iterator)
                self.observed += 1
                info = entry.stat(follow_symlinks=False)
                alias = unicodedata.normalize('NFC', entry.name.rstrip(' .').casefold())
                self.db.execute('insert or ignore into names values(?,?,?)', (path, alias, entry.name))
                if entry.name == '.git':
                    continue
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    self._omit('linked-entry')
                elif stat.S_ISDIR(info.st_mode):
                    relative = path + '/' + entry.name if path else entry.name
                    try:
                        discovery_path(relative)
                    except ValueError:
                        self._omit('nonportable-path')
                        continue
                    if info.st_dev != self.device:
                        self._omit('filesystem-boundary')
                        continue
                    self.db.execute('insert into dirs(path,parent,name,alias,cohesive,excluded,stamp) values(?,?,?,?,?,?,?)', (relative, path, entry.name, alias, int(cohesive), int(self.policy.is_excluded(relative)), json.dumps(stamp(info))))
                elif not stat.S_ISREG(info.st_mode):
                    self._omit('special-entry')
            except StopIteration:
                stored = self.db.execute('select stamp from dirs where path=?', (path,)).fetchone()[0]
                if json.dumps(stamp(os.fstat(fd))) != stored:
                    self.status = 'stale'
                collisions = self.db.execute('select alias from names where parent=? group by alias having count(*)>1', (path,))
                for (alias,) in collisions:
                    self._omit('path-collision')
                    self.db.execute('update dirs set state=3 where parent=? and alias=?', (path, alias))
                self.db.execute('update dirs set state=2 where path=?', (path,))
                self._close_active()
            except OSError:
                self._omit('inaccessible-entry')
                self.db.execute('update dirs set state=3 where path=?', (path,))
                self._close_active()
        self.db.commit()
        return self.progress()

    def progress(self):
        return {'ok': True, 'generation': self.generation, 'freshness': 'observed-metadata-not-snapshot', 'status': self.status, 'can_apply': False,
                'complete': self.status == 'complete', 'entries_observed': self.observed,
                'directories_indexed': self.db.execute("select count(*) from dirs where path!=''").fetchone()[0],
                'payload_bytes_read': 0, 'capture_policy_sha256': self.policy.sha256,
                'omissions': [r[0] for r in self.db.execute('select code from omissions order by code')],
                'resume_scope': 'live-session-only'}

    def page(self, after='', query='', limit=50):
        if self.status not in ('complete', 'partial'):
            return {**self.progress(), 'candidates': [], 'next': None}
        if not self._source_current():
            self.status = 'stale'
            return {**self.progress(), 'candidates': [], 'next': None}
        rows = self.db.execute("select path,kind,cohesive,excluded from dirs where path>? and state=2 and instr(lower(path),lower(?))>0 order by path limit ?", (after, query, min(max(limit, 1), 50) + 1)).fetchall()
        result = []
        for path, kind, cohesive, excluded in rows[:50 if limit > 50 else max(limit, 1)]:
            rank, code, reason, consequence = recommendation(kind, cohesive, excluded)
            candidate = {'path': path, 'kind': kind, 'selectable': True, 'rank': rank,
                           'evidence_code': code, 'reason': reason, 'confidence': 'unknown',
                           'planned_consequence': consequence, 'recommended': code == 'git-boundary'}
            if len(json.dumps(result + [candidate], ensure_ascii=True).encode()) > 32000:
                break
            result.append(candidate)
        root_kind = self.db.execute("select kind from dirs where path=''").fetchone()[0]
        return {**self.progress(), 'workspace_recommendation': 'keep-repository-together' if root_kind == 'git-directory-unverified' else 'ownership-unknown', 'candidates': result,
                'next': result[-1]['path'] if len(rows) > len(result) else None}

    def lookup(self, path):
        discovery_path(path)
        result = self.page(query=path, limit=1)
        row = self.db.execute('select state from dirs where path=?', (path,)).fetchone()
        if self.status not in ('complete', 'partial') or row != (2,):
            raise ValueError('manual path unavailable or unsupported')
        # Validate each ancestor's identity, full sibling generation and guards;
        # a typed path receives exactly the same checks as a browsed path.
        for length in range(len(path.split('/')) + 1):
            ancestor = '/'.join(path.split('/')[:length])
            expected = self.db.execute('select stamp from dirs where path=? and state=2', (ancestor,)).fetchone()
            fd = self._open(ancestor)
            try:
                if (not expected or json.dumps(stamp(os.fstat(fd))) != expected[0]
                        or self._kind(fd) not in ('folder', 'git-directory-unverified')):
                    self.status = 'stale'
                    raise ValueError('manual choice generation is stale')
            finally:
                os.close(fd)
        return {**result, 'candidates': [], 'selected': path, 'writes_performed': False}


def serve(root, input_stream, output_stream):
    """Local JSON-lines protocol; close/EOF removes index. No paths in logs."""
    inventory = DirectoryInventory(root)
    try:
        while True:
            line = input_stream.readline(4097)
            if not line:
                break
            if len(line) > 4096:
                raise ValueError('inventory request too large')
            request = json.loads(line)
            action = request.get('action')
            if action == 'close':
                break
            if action in ('advance', 'cancel'):
                result = inventory.advance(cancelled=action == 'cancel')
            elif action == 'page':
                result = inventory.page(after=request.get('after', ''), query=request.get('query', ''))
            elif action == 'lookup':
                result = inventory.lookup(request['path'])
            else:
                raise ValueError('unknown inventory action')
            output_stream.write(json.dumps(result, ensure_ascii=True) + '\n')
            output_stream.flush()
    finally:
        inventory.close()
