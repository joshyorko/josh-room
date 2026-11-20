"""TLC sibling-admission model plus bounded executions of the real scanner.

Standard library only. No provider, credential, Git, payload, or user-tree reads.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.dont_write_bytecode = True
JAR_SHA256 = '936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88'
TLC_TOOL = ('http:tla2tools[url=https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar,'
            f'checksum=sha256:{JAR_SHA256}]@1.7.4')
JAVA_TOOL = 'java@temurin-25.0.5+8.0.LTS'
SOURCE = ROOT / 'src/josh_room/directory_inventory.py'
TERMINAL = {'complete', 'partial', 'stale'}
POOL = ('safe', 'SAFE', 'other')


class Violation(AssertionError):
    def __init__(self, message, admission=None):
        super().__init__(message)
        self.admission = admission


def require(condition, message):
    if not condition:
        raise Violation(message)


def load_source(source, filename):
    # Compile the actual production file (or an exact scratch mutation), not a
    # second implementation of advance(). Relative imports use real modules.
    require(Path(filename).read_text() == source, 'source input changed before loading')
    spec = importlib.util.spec_from_file_location('josh_room._formal_subject', filename)
    require(spec is not None and spec.loader is not None, 'cannot load product module')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay(module, root, order, failure, fail_at, pause_at, fault_scope=""):
    scan = module.DirectoryInventory(root)
    native_scandir = module.os.scandir
    native_open = scan._open
    directories = [root, *(path for path in root.rglob('*') if path.is_dir())]
    scopes = {path.stat().st_ino: '' if path == root else path.relative_to(root).as_posix() for path in directories}
    names = {scope: tuple(child.name for child in (root / scope).iterdir()) for scope in scopes.values()}
    names[''] = order
    events = []
    eof, failed = set(), set()
    case = {'order': order, 'failure': failure, 'fail_at': fail_at,
            'pause_at': pause_at, 'fault_scope': fault_scope}

    def safe(path, action):
        if not path:
            return
        parent, _, name = path.rpartition('/')
        parent_state = scan.db.execute('select state from dirs where path=?', (parent,)).fetchone()
        evidence = {'parent': parent, 'path': path, 'action': action,
                    'eof': parent in eof, 'failed': parent in failed,
                    'parent_state': parent_state[0] if parent_state else None}
        if (parent not in eof or parent in failed or parent_state != (2,)
                or sum(sibling.casefold() == name.casefold() for sibling in names[parent]) != 1):
            raise Violation('child lacks complete unambiguous parent admission', evidence)

    class Entry:
        def __init__(self, entry, position, scope):
            self.entry, self.position, self.scope = entry, position, scope
        @property
        def name(self):
            return self.entry.name
        def stat(self, **kwargs):
            if self.scope == fault_scope and failure == 'stat' and self.position == fail_at:
                failed.add(self.scope)
                events.append('entry stat fails in ' + (self.scope or '(root)'))
                raise OSError('synthetic entry stat failure')
            return self.entry.stat(**kwargs)

    class Stream:
        def __init__(self, fd, scope):
            self.scope = scope
            with native_scandir(fd) as stream:
                entries = {entry.name: entry for entry in stream}
            self.entries = [entries[name] for name in names[scope]]
            self.position = 0
        def __next__(self):
            if self.scope == fault_scope and failure == 'iterator' and self.position == fail_at:
                failed.add(self.scope)
                events.append('directory iterator fails in ' + (self.scope or '(root)'))
                raise OSError('synthetic directory iterator failure')
            if self.position == len(self.entries):
                eof.add(self.scope)
                events.append('all sibling names consumed in ' + (self.scope or '(root)'))
                raise StopIteration
            item = Entry(self.entries[self.position], self.position, self.scope)
            self.position += 1
            events.append('read sibling ' + item.name)
            return item
        def close(self):
            pass

    def controlled_scandir(fd):
        return Stream(fd, scopes[os.fstat(fd).st_ino])

    def guarded_open(path):
        events.append('open directory ' + (path or '(root)'))
        safe(path, 'open')
        return native_open(path)

    def check_page():
        page = scan.page(limit=1)
        offered = set()
        for _ in range(16):
            for row in page['candidates']:
                events.append('offer directory ' + row['path'])
                require(row['path'] not in offered, 'pagination duplicated an identity')
                offered.add(row['path'])
                safe(row['path'], 'offer')
            if page['next'] is None:
                return offered
            page = scan.page(after=page['next'], limit=1)
        raise Violation('pagination failed to terminate at bound')

    scan._open = guarded_open
    try:
        # One budget=1 call is one work iteration; latency is out of scope.
        with (patch.object(module.os, 'scandir', controlled_scandir),
              patch.object(module.time, 'monotonic', return_value=0)):
            for step in range(128):
                if step == pause_at:
                    before = scan.db.execute('select path,state from dirs order by path').fetchall()
                    scan.advance(cancelled=True)
                    require(before == scan.db.execute('select path,state from dirs order by path').fetchall(),
                            'pause changed admission state')
                    events.append('pause')
                    require(not check_page(), 'paused session exposed candidates')
                    events.append('resume')
                status = scan.advance(budget=1)['status']
                offered = check_page()
                if status in TERMINAL:
                    require(failure == 'none' or fault_scope in failed, 'scheduled fault did not fire in its designated scope')
                    expected = {name for name in order if (root / name).is_dir()
                                and sum(n.casefold() == name.casefold() for n in order) == 1}
                    if '' in failed:
                        expected.clear()
                    else:
                        expected -= failed
                        expected |= {name + '/leaf' for name in tuple(expected)}
                    require(offered == expected, 'completed safe choices missing or fabricated')
                    require(not failed or status == 'partial', 'failed scope claimed complete')
                    return step + 1
            raise Violation('scanner failed to terminate within 128 work iterations')
    except Violation as error:
        error.args = (json.dumps({'property': 'SiblingAdmission', 'case': case,
                                 'trace': events, 'admission': error.admission, 'violation': str(error)}, ensure_ascii=True),)
        raise
    finally:
        # Restore patched scandir BEFORE TemporaryDirectory/shutil cleanup.
        scan.close()


def verify_implementation(module, scratch):
    cases, transitions = 0, 0
    # Every subset, file/directory assignment, enumeration permutation, one
    # iterator/stat failure position, and zero/one pause at each work boundary.
    with patch.object(module.tempfile, 'tempdir', str(scratch)):
        for count in range(4):
            for names in itertools.combinations(POOL, count):
                for directory_bits in itertools.product((False, True), repeat=count):
                    with tempfile.TemporaryDirectory(prefix='tree-', dir=scratch) as directory:
                        root = Path(directory)
                        for name, is_dir in zip(names, directory_bits):
                            if is_dir:
                                (root / name / 'leaf').mkdir(parents=True)
                            else:
                                (root / name).touch()
                        for order in itertools.permutations(names):
                            faults = [('', 'none', -1)] + [('', 'iterator', i) for i in range(count + 1)] + [('', 'stat', i) for i in range(count)]
                            for name, is_dir in zip(names, directory_bits):
                                if is_dir and sum(n.casefold() == name.casefold() for n in names) == 1:
                                    faults += [(name, 'iterator', 0), (name, 'iterator', 1), (name, 'stat', 0)]
                            for fault_scope, failure, fail_at in faults:
                                steps = replay(module, root, order, failure, fail_at, -1, fault_scope)
                                cases += 1
                                transitions += steps
                                for pause_at in range(steps):
                                    transitions += replay(module, root, order, failure, fail_at, pause_at, fault_scope)
                                    cases += 1
    return {'cases': cases, 'work_iterations': transitions}


def tools():
    jar = os.environ.get('TLA2TOOLS')
    java = os.environ.get('JAVA')
    def where(tool):
        if not shutil.which('mise'):
            return None
        result = subprocess.run(['mise', 'where', tool], check=False, text=True, capture_output=True, timeout=10)
        return Path(result.stdout.strip()) if result.returncode == 0 else None
    if not jar:
        directory = where(TLC_TOOL)
        jar = directory / 'tla2tools.jar' if directory else None
    if not java:
        directory = where(JAVA_TOOL)
        java = directory / 'bin/java' if directory else None
    if not jar or not java:
        raise ValueError('Install TLC 1.7.4 and Java 25.0.5; set TLA2TOOLS and JAVA. No tools installed automatically.')
    if hashlib.sha256(Path(jar).read_bytes()).hexdigest() != JAR_SHA256:
        raise ValueError('TLC jar does not match pinned 1.7.4 SHA256')
    return str(java), str(jar)


def tlc(java, jar, config, scratch, broken=False):
    directory = Path(scratch) / config.stem
    command = [java, '-Xmx256m', '-XX:+UseParallelGC', '-cp', jar, 'tlc2.TLC',
               '-workers', '1', '-metadir', str(directory), '-config', str(config),
               str(ROOT / 'verification/discovery/Admission.tla')]
    result = subprocess.run(command, check=False, text=True, capture_output=True, timeout=60, cwd=scratch)
    log = result.stdout + result.stderr
    (Path(scratch) / (config.stem + '.log')).write_text(log)
    if broken:
        require(result.returncode != 0 and 'Invariant SiblingAdmission is violated' in log,
                'broken model did not fail the admission invariant')
    else:
        require(result.returncode == 0 and 'Model checking completed. No error has been found.' in log,
                'TLC failed; rerun with /statechart. Log:\n' + log[:24000])
    return [line.strip() for line in log.splitlines() if 'distinct states found' in line or 'depth of' in line]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test', action='store_true', help='also require real-code and model mutations to fail')
    args = parser.parse_args()
    def expired(_signum, _frame):
        raise TimeoutError('formal verification exceeded 60 seconds')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(60)
    start = time.monotonic()
    java, jar = tools()
    with tempfile.TemporaryDirectory(prefix='codex-discovery-formal-') as scratch:
        source = SOURCE.read_text()
        subject = load_source(source, SOURCE)
        result = {'tlc': tlc(java, jar, ROOT / 'verification/discovery/Admission.cfg', scratch),
                  'implementation': verify_implementation(subject, scratch)}
        if args.self_test:
            result['broken_models'] = {}
            for config in ('Admission-broken.cfg', 'Admission-priority-broken.cfg', 'Admission-offer-broken.cfg'):
                result['broken_models'][config] = tlc(java, jar, ROOT / 'verification/discovery' / config, scratch, broken=True)
            mutations = {
                'early-admission': ('cohesive,excluded,stamp,state) values(?,?,?,?,?,?,?,1)',
                                    'cohesive,excluded,stamp,state) values(?,?,?,?,?,?,?,0)'),
                'queue-priority': ('where state in (0,4) order by state desc,path limit 1',
                                   'where state in (0,4) order by state,path limit 1'),
                'unsafe-offer': ('where path>? and state=2 and instr',
                                 'where path>? and state in (1,2) and instr'),
            }
            result['broken_implementations'] = {}
            mutant_file = Path(scratch) / 'directory_inventory-broken.py'
            for name, (anchor, replacement) in mutations.items():
                require(source.count(anchor) == 1, 'mutation anchor drifted: ' + name)
                mutant_file.write_text(source.replace(anchor, replacement))
                mutant = load_source(mutant_file.read_text(), mutant_file)
                try:
                    verify_implementation(mutant, scratch)
                except Violation as error:
                    evidence = error.admission
                    require(evidence is not None, 'unrelated failure cannot count as admission mutation detection: ' + str(error))
                    trace = json.loads(str(error))
                    if name in ('early-admission', 'unsafe-offer'):
                        require(evidence['failed'] and trace['case']['failure'] == 'iterator',
                                'mutation must reproduce failed iterator followed by unsafe admission')
                        require(any('directory iterator fails' in step for step in trace['trace']),
                                'mutation trace did not record iterator failure')
                        require(evidence['action'] == ('open' if name == 'early-admission' else 'offer'),
                                'mutation detected the wrong admission action')
                    else:
                        require(evidence['parent_state'] == 4 and evidence['action'] == 'open',
                                'queue-priority mutation must enter during parent finalization')
                    result['broken_implementations'][name] = trace
                else:
                    raise Violation('verification missed real-code mutation: ' + name)
            # Drop the scratch mutation, then recheck the unchanged real file.
            mutant_file.unlink()
            require(SOURCE.read_text() == source, 'production source changed during verification')
            result['restored_implementation'] = verify_implementation(load_source(SOURCE.read_text(), SOURCE), scratch)
        result['seconds'] = round(time.monotonic() - start, 3)
        result['python'] = sys.version.split()[0]
        result['source_sha256'] = hashlib.sha256(source.encode()).hexdigest()
        print(json.dumps(result, indent=2))
    signal.alarm(0)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Violation, ValueError, OSError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
