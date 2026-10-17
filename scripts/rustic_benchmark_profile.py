"""Opt-in observation of actual adapter calls; timings overlap and are not additive."""
from __future__ import annotations

import functools
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from josh_room import parent_inventory, room_store_operations, rustic_store

try:
    import resource
except ImportError:
    resource = None


def child_cpu() -> float | None:
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def process_status(pid: int | str) -> dict[str, int]:
    try:
        lines = Path(f'/proc/{pid}/status').read_text().splitlines()
        return {key: int(line.split()[1]) for line in lines
                for key in ('VmRSS', 'VmHWM', 'Threads') if line.startswith(key + ':')}
    except (OSError, ValueError, IndexError):
        return {}


class Profile:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.phase = None
        self.spans = {}
        self.python_peak = {}
        self.live = {}
        self.undo = []
        self.stop = threading.Event()
        self.thread = None

    def sample(self):
        while not self.stop.is_set():
            phase = self.phase
            if phase:
                status = process_status('self')
                if 'VmRSS' in status:
                    self.python_peak[phase] = max(self.python_peak.get(phase, 0), status['VmRSS'])
            for pid, record in list(self.live.items()):
                status = process_status(pid)
                for field in ('VmHWM', 'Threads'):
                    if field in status: record[field] = max(record.get(field, 0), status[field])
            self.stop.wait(0.02)

    def enter(self):
        if not self.enabled: return self
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        for module, name, label in (
            (room_store_operations, '_scan_workspace', 'shared.workspace_scan'),
            (room_store_operations, '_validate_snapshot_entries', 'shared.inventory_validation'),
            (parent_inventory, 'read_inventory', 'cache.read_inventory'),
            (parent_inventory, 'write_inventory', 'cache.write_inventory'),
            (rustic_store, 'read_inventory', 'cache.read_inventory'),
            (rustic_store, 'write_inventory', 'cache.write_inventory'),
            (rustic_store, 'parse_node_stream', 'rustic.bulk_parse_validate'),
            (rustic_store, 'parse_tree_node', 'rustic.native_node_validation'),
            (json, 'loads', 'python.json_decode'),
            (json, 'dumps', 'python.json_encode'),
        ):
            if hasattr(module, name): self.wrap(module, name, label)
        return self

    def close(self):
        for module, name, original in reversed(self.undo): setattr(module, name, original)
        self.undo.clear()
        self.stop.set()
        if self.thread: self.thread.join(timeout=1)

    @contextmanager
    def span(self, label):
        phase = self.phase
        if not self.enabled or phase is None:
            yield
            return
        started, cpu = time.perf_counter(), time.process_time()
        try: yield
        finally:
            record = self.spans.setdefault(phase, {}).setdefault(label, {'calls': 0, 'wall_seconds': 0., 'python_cpu_seconds': 0.})
            record['calls'] += 1
            record['wall_seconds'] += time.perf_counter() - started
            record['python_cpu_seconds'] += time.process_time() - cpu

    def wrap(self, owner, name, label):
        original = getattr(owner, name)
        @functools.wraps(original)
        def measured(*args, **kwargs):
            with self.span(label): return original(*args, **kwargs)
        setattr(owner, name, measured)
        self.undo.append((owner, name, original))

    def store(self, store):
        if not self.enabled: return
        for name, label in (
            ('open_existing', 'repository.open_existing'), ('initialize', 'repository.initialize'),
            ('_ensure_version', 'adapter.version'), ('_environment', 'adapter.environment'),
            ('_prime_masterkey', 'authentication.prime_masterkey'), ('backup', 'adapter.backup'),
            ('snapshot', 'repository.snapshot'), ('restore', 'adapter.native_restore'),
        ):
            if hasattr(store, name): self.wrap(store, name, label)
        # Measure iterator advances, rather than charging caller validation to I/O.
        original = store._read_lines
        def lines(*args, **kwargs):
            iterator = iter(original(*args, **kwargs))
            while True:
                with self.span('adapter.read_line'):
                    try: value = next(iterator)
                    except StopIteration: return
                yield value
        store._read_lines = lines
        self.undo.append((store, '_read_lines', original))

    def native_start(self, pid):
        if self.enabled: self.live[pid] = {}

    def native_finish(self, pid, *, spawn_seconds, native_seconds, cpu_before):
        if not self.enabled: return {}
        record = self.live.pop(pid, {})
        after = child_cpu()
        return {'popen_seconds': spawn_seconds, 'observed_process_seconds': native_seconds,
                'child_cpu_seconds': None if after is None or cpu_before is None else after - cpu_before,
                'observed_peak_rss_kib': record.get('VmHWM'), 'observed_peak_threads': record.get('Threads')}

    def result(self, label, *, wall, python_cpu, native):
        self.phase = None
        if not self.enabled: return None
        def rounded(value): return round(value, 6) if isinstance(value, float) else value
        groups = {}
        for verb, record in native:
            group = groups.setdefault(verb, {'calls': 0, 'popen_seconds': 0., 'observed_process_seconds': 0., 'child_cpu_seconds': 0., 'observed_peak_rss_kib': None, 'observed_peak_threads': None})
            group['calls'] += 1
            for key, value in record.items():
                if value is None: continue
                if key.startswith('observed_peak'):
                    group[key] = max(group[key] or 0, value)
                else: group[key] += value
        native_wall = sum(record.get('observed_process_seconds', 0) for _, record in native)
        return {'wall_seconds': rounded(wall), 'python_cpu_seconds': rounded(python_cpu),
                'wall_outside_observed_native_processes': rounded(max(0., wall - native_wall)),
                'observed_python_peak_rss_kib': self.python_peak.get(label),
                'native': {verb: {key: rounded(value) for key, value in values.items()} for verb, values in groups.items()},
                'overlapping_spans': {key: {field: rounded(value) for field, value in values.items()} for key, values in self.spans.get(label, {}).items()}}


CONTRACT = ('Flat spans overlap and are not additive. Observed native lifetime includes pipe backpressure and caller parsing before wait; it is not pure engine CPU. Popen time is measured separately. Native CPU uses RUSAGE_CHILDREN differences with sequential owned child commands. Linux RSS/threads are observed every20ms; peaks are lower-bound observations. No argv, environment, key material or native output is retained.')
