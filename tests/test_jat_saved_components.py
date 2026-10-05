from __future__ import annotations

import json
from pathlib import Path

import pytest

from josh_room.jat import run_build


def _request_from_fake_run(monkeypatch, tmp_path):
    captured = {}

    def fake_run(argv, _timeout, **_kwargs):
        captured["request"] = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
        result = tmp_path / "output" / "result.json"
        result.parent.mkdir(exist_ok=True)
        result.write_text('{"operation":"build","success":true,"exit_status":0}')
        return 0, ""

    monkeypatch.setattr("josh_room.jat._run", fake_run)
    return captured


def test_run_build_passes_saved_component_archives_and_metadata(monkeypatch, tmp_path):
    captured = _request_from_fake_run(monkeypatch, tmp_path)
    run_build(
        tmp_path,
        tmp_path / "workspace",
        tmp_path / "capsule.haul.tar.zst",
        rcc_archive=tmp_path / "rcc-environment.rcca",
        rcc_metadata=tmp_path / "rcc-environment-metadata.json",
        brew_archive=tmp_path / "homebrew-recovery.tar.zst",
        hauler_archive=tmp_path / "hauler-content.tar.zst",
    )

    assert captured["request"] == {
        "folder": str(tmp_path / "workspace"),
        "output": str(tmp_path / "capsule.haul.tar.zst"),
        "images": [],
        "all_images": False,
        "rcc_archive": str(tmp_path / "rcc-environment.rcca"),
        "rcc_metadata": str(tmp_path / "rcc-environment-metadata.json"),
        "brew_archive": str(tmp_path / "homebrew-recovery.tar.zst"),
        "hauler_archive": str(tmp_path / "hauler-content.tar.zst"),
    }


def test_run_build_omits_saved_component_fields_when_not_requested(
    monkeypatch, tmp_path
):
    captured = _request_from_fake_run(monkeypatch, tmp_path)

    run_build(tmp_path, tmp_path / "workspace", tmp_path / "capsule.haul.tar.zst")

    assert set(captured["request"]) == {"folder", "output", "images", "all_images"}


def test_run_build_requires_rcc_archive_and_metadata_together(tmp_path):
    with pytest.raises(ValueError, match="together"):
        run_build(
            tmp_path,
            tmp_path / "workspace",
            tmp_path / "capsule.haul.tar.zst",
            rcc_archive=tmp_path / "rcc-environment.rcca",
        )
