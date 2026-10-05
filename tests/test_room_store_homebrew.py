import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import room_store_homebrew as component


def test_unchanged_saved_brew_reuses_reference_without_native_validation(tmp_path):
    archive = tmp_path / "brew.tar.zst"
    archive.write_bytes(b"already verified saved recovery bytes")
    prior = {
        "kind": "homebrew-recovery", "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size": archive.stat().st_size, "member_basename": "homebrew-recovery.tar.zst",
        "snapshot": {"repository_id": "a" * 64, "repository_format": 2, "snapshot_id": "b" * 64, "tree_id": "c" * 64},
    }
    def unavailable():
        pytest.fail("unchanged verified bytes must not wake JAT or restic")

    assert component.capture_homebrew_component(
        archive=archive, prior_component=prior, repository_id="a" * 64,
        repository_format=2, restic=None, validator=unavailable,
    ) == prior


def test_changed_brew_is_validated_before_capture_and_corroborates_owned_bytes(tmp_path):
    archive = tmp_path / "brew.tar.zst"
    archive.write_bytes(b"synthetic saved recovery bytes")
    calls = []
    class Validator:
        def validate_brew_archive(self, owned):
            assert Path(owned).read_bytes() == archive.read_bytes()
            calls.append("validated")
    class Store:
        def backup(self, stage, **kwargs):
            assert calls == ["validated"]
            assert (Path(stage) / "homebrew-recovery.tar.zst").read_bytes() == archive.read_bytes()
            calls.append("backed-up")
            return SimpleNamespace(snapshot_id="b" * 64)
        def snapshot(self, _snapshot):
            return SimpleNamespace(tree_id="c" * 64)

    result = component.capture_homebrew_component(
        archive=archive, prior_component=None, repository_id="a" * 64,
        repository_format=2, restic=Store(), validator=lambda: Validator(),
    )

    assert calls == ["validated", "backed-up"]
    assert result["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert result["snapshot"]["tree_id"] == "c" * 64


def test_brew_input_limit_rejects_before_native_work(tmp_path, monkeypatch):
    archive = tmp_path / "oversized.tar.zst"
    archive.write_bytes(b"too large")
    monkeypatch.setattr(component, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(component.RoomStoreHomebrewError, match="bounded regular"):
        component.capture_homebrew_component(
            archive=archive, prior_component=None, repository_id="a" * 64,
            repository_format=2, restic=None, validator=lambda: pytest.fail("native work"),
        )
