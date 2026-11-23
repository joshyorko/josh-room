"""Measure local live listing and selected preview on disposable synthetic trees.

Run from the checkout with PYTHONPATH=src python scripts/measure_room_view_discovery.py.
Times include query validation, sanitization and JSON encoding on the calling thread.
"""

from __future__ import annotations

import json
import platform
import resource
import statistics
import tempfile
import time
import tracemalloc
from contextlib import closing
from datetime import date
from pathlib import Path

from josh_room.view.room_provider import RoomWorkspaceProvider
from josh_room.view.sanitizer import DEFAULT_SANITIZATION_POLICY
from josh_room.view.server import _filesystem_result


class MeasuredProvider(RoomWorkspaceProvider):
    content_reads = 0
    content_bytes = 0

    def _read_regular_file_bytes(self, *args, **kwargs):
        result = super()._read_regular_file_bytes(*args, **kwargs)
        self.content_reads += 1
        self.content_bytes += len(result or b"")
        return result


def measure(root, siblings):
    for n in range(siblings):
        (root / f"group-{n:05}").mkdir()
    (root / ".venv").mkdir()
    parts = ["src", "josh_room", "view", "a", "b", "c", "d", "e"]
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True)
    for n in range(33):
        (folder / f"early-{n:02}.txt").write_bytes(b"a" * 16384)
    target = b"print('sample!')\n"
    (folder / "sample.py").write_bytes(target)

    provider = MeasuredProvider(root)
    times = []
    largest_page = largest_response = most_examined = transient = 0
    entries = omitted = 0
    tracemalloc.start()
    with closing(provider.open_filesystem()) as adapter:
        def query(operation, **params):
            nonlocal largest_response, transient
            args = {k: [value] for k, value in params.items() if value is not None}
            before = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            start = time.perf_counter()
            data = _filesystem_result(adapter, f"/fs/{operation}", args, DEFAULT_SANITIZATION_POLICY)
            duration = (time.perf_counter() - start) * 1000
            transient = max(transient, tracemalloc.get_traced_memory()[1] - before)
            largest_response = max(largest_response, len(data))
            return json.loads(data), duration, len(data)

        object_id, generation = "root", None
        for label in [*parts, "sample.py"]:
            cursor, next_id, previous = None, None, 0
            while True:
                page, elapsed, _ = query("list", id=object_id, generation=generation, cursor=cursor)
                times.append(elapsed)
                generation = page["generation"]
                rows = page["entries"]
                most_examined = max(most_examined, page["examined"] - previous)
                previous = page["examined"]
                largest_page = max(largest_page, len(rows))
                entries += len(rows)
                for row in rows:
                    if row["label"] == label:
                        next_id = row["id"]
                cursor = page["cursor"]
                if not cursor:
                    assert page["coverage"] == "complete", page["coverage"]
                    omitted += page["omitted"]
                    break
            assert next_id, f"not discovered: {label}"
            object_id = next_id
        assert entries == siblings + len(parts) + 34 and omitted == 1
        assert provider.content_reads == 0
        preview, selected_ms, selected_response = query("preview", id=object_id, generation=generation)
        assert preview["node"]["previews"][0]["body"] == target.decode()
        assert provider.content_reads == 1 and provider.content_bytes == len(target)
        assert most_examined <= 128 and largest_page <= 48 and largest_response <= 1024 * 1024
    tracemalloc.stop()
    return {
        "synthetic_unrelated_directories": siblings, "target_directory_depth": len(parts),
        "unrelated_text_bytes": 33 * 16384, "entries_listed": entries, "excluded_entries": omitted,
        "listing_requests": len(times), "listing_ms_total": round(sum(times), 1),
        "listing_ms_median": round(statistics.median(times), 2), "listing_ms_max": round(max(times), 2),
        "max_raw_siblings_examined_per_request": most_examined, "max_emitted_per_request": largest_page,
        "max_encoded_response_bytes": largest_response, "max_transient_traced_python_bytes": transient,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "listing_content_bytes": 0, "selected_file_bytes_read": provider.content_bytes,
        "selected_preview_ms": round(selected_ms, 2), "selected_response_bytes": selected_response,
    }


if __name__ == "__main__":
    print(json.dumps({"platform": platform.system(), "machine": platform.machine(),
                      "python": platform.python_version(), "tracemalloc": True}), flush=True)
    for count in (1000, 10000):
        prefix = f"codex-room-view-discovery-{date.today()}-"
        with tempfile.TemporaryDirectory(prefix=prefix) as synthetic:
            print(json.dumps(measure(Path(synthetic), count), sort_keys=True), flush=True)
