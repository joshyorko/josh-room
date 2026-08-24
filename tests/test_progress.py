import json
import os

from josh_room.progress import report_progress


def test_progress_is_silent_without_private_runtime_sink(tmp_path, monkeypatch):
    monkeypatch.delenv("JOSH_ROOM_PROGRESS_FILE", raising=False)
    report_progress("build", "Building portable haul")
    assert list(tmp_path.iterdir()) == []


def test_progress_appends_versioned_private_json_lines(tmp_path, monkeypatch):
    destination = tmp_path / "progress.jsonl"
    monkeypatch.setenv("JOSH_ROOM_PROGRESS_FILE", str(destination))

    report_progress("upload", "Uploading encrypted snapshot", current=4, total=10)
    report_progress("catalog", "Publishing encrypted catalog")

    records = [json.loads(line) for line in destination.read_text().splitlines()]
    assert records == [
        {
            "current": 4,
            "format_version": 1,
            "message": "Uploading encrypted snapshot",
            "stage": "upload",
            "total": 10,
        },
        {
            "format_version": 1,
            "message": "Publishing encrypted catalog",
            "stage": "catalog",
        },
    ]
    assert os.stat(destination).st_mode & 0o077 == 0


def test_progress_rejects_unbounded_or_multiline_display_text(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_PROGRESS_FILE", str(tmp_path / "progress.jsonl"))
    report_progress("build\nsecret", "x" * 400)
    record = json.loads((tmp_path / "progress.jsonl").read_text())
    assert record["stage"] == "build secret"
    assert len(record["message"]) == 240
