import hashlib
import inspect
import os
import resource
from pathlib import Path

import pytest

from josh_room.envelope import build_envelope_file, read_envelope_file
from josh_room.operations import create_snapshot, hydrate


def test_snapshot_operations_do_not_materialize_snapshot_sized_bytes():
    body = inspect.getsource(create_snapshot) + inspect.getsource(hydrate)
    for forbidden in (
        ".read_bytes(",
        ".write_bytes(",
        "build_envelope(",
        "read_envelope(",
        "encrypt(",
        "decrypt(",
        ".put(ciphertext",
        ".get(snapshot",
    ):
        assert forbidden not in body


@pytest.mark.skipif(os.environ.get("JOSH_ROOM_LARGE_STREAM_TEST") != "1", reason="large streaming acceptance is opt-in")
def test_generated_one_gib_envelope_has_bounded_peak_memory(tmp_path):
    size = 1024 * 1024 * 1024 + 1
    payload = tmp_path / "large.haul.tar.zst"
    with payload.open("wb") as handle:
        handle.truncate(size)
        handle.seek(size - 1)
        handle.write(b"x")
    digest = _digest(payload)
    manifest = {
        "format_version": 1,
        "project_id": "large-synthetic",
        "snapshot_id": "large-event",
        "created_at": "2026-08-24T00:00:00Z",
        "payload": {"format": "jat-hauler", "sha256": digest, "size": size, "producer_version": "synthetic"},
        "source": {},
    }
    envelope = tmp_path / "large.jroom"
    restored = tmp_path / "restored.haul.tar.zst"
    before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    build_envelope_file(manifest, payload, envelope)
    assert read_envelope_file(envelope, restored) == manifest
    after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert restored.stat().st_size == size
    assert _digest(restored) == digest
    assert after_kib - before_kib < 128 * 1024


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
