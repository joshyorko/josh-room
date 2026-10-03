import json
import os
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from josh_room import workspace_state
from josh_room.room_store_operations import scan_workspace_for_status
from josh_room.workspace_state import (
    context_status,
    local_status,
    write_stat_workspace_marker,
    write_workspace_marker,
)


def _write_v3(workspace, *, signature=None, policy=None, path_binding=None):
    scan = scan_workspace_for_status(workspace)
    return write_stat_workspace_marker(
        workspace,
        dimension_id="dimension-a",
        encryption_domain_id="domain-a",
        project_id="project-a",
        snapshot_id="snapshot-a",
        display_name="Project A",
        workspace_signature=signature or scan.signature,
        signature_algorithm=scan.signature_algorithm,
        capture_policy_sha256=policy or scan.capture_policy_sha256,
        path_binding=path_binding,
    )


def test_v3_context_is_path_bound_and_does_not_scan(tmp_path, monkeypatch):
    _write_v3(tmp_path)
    monkeypatch.setattr(
        "josh_room.room_store_operations.scan_workspace_for_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("context must not scan")),
    )
    result = context_status(tmp_path)
    assert result["state"] == "linked"
    assert result["format_version"] == 1
    assert result["snapshot_id"] == "snapshot-a"
    assert result["signature_algorithm"] == "josh-room-stat-v1"
    assert "workspace_signature" in result
    assert "capture_policy_sha256" in result


def test_v3_local_status_detects_metadata_and_policy_changes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "note.txt"
    file.write_text("same bytes")
    _write_v3(source)
    assert local_status(source)["state"] == "clean"

    original = file.stat().st_mtime_ns
    os.utime(file, ns=(original + 2_000_000, original + 2_000_000))
    changed = local_status(source)
    assert changed["state"] == "changed"
    assert changed["signature_algorithm"] == "josh-room-stat-v1"
    assert changed["signature_matches"] is False

    _write_v3(source)
    (source / ".josh-roomignore").write_text("ignored.tmp\n")
    policy_changed = local_status(source)
    assert policy_changed["state"] == "changed"
    assert policy_changed["policy_matches"] is False


def test_v3_copied_marker_is_context_invalid_and_status_changed(tmp_path):
    original = tmp_path / "original"
    copied = tmp_path / "copied"
    original.mkdir()
    copied.mkdir()
    _write_v3(original)
    shutil.copyfile(original / ".josh-room.json", copied / ".josh-room.json")
    assert context_status(copied)["state"] == "invalid"
    assert local_status(copied)["state"] == "changed"


def test_restored_tree_gets_a_fresh_signature_and_clean_marker(tmp_path):
    source = tmp_path / "source"
    staged = tmp_path / "staged"
    destination = tmp_path / "restored"
    source.mkdir()
    (source / "note.txt").write_text("restored bytes")
    _write_v3(source)
    shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".josh-room.json"))

    restored_scan = scan_workspace_for_status(staged)
    assert restored_scan.signature != json.loads((source / ".josh-room.json").read_text())["workspace_signature"]
    write_stat_workspace_marker(
        staged,
        dimension_id="dimension-a",
        encryption_domain_id="domain-a",
        project_id="project-a",
        snapshot_id="snapshot-b",
        display_name="Project A",
        workspace_signature=restored_scan.signature,
        signature_algorithm=restored_scan.signature_algorithm,
        capture_policy_sha256=restored_scan.capture_policy_sha256,
        path_binding=destination,
    )
    staged.rename(destination)
    status = local_status(destination)
    assert status["state"] == "clean"
    assert status["snapshot_id"] == "snapshot-b"


def test_v3_scan_fails_closed_for_dangerous_symlink_and_unknown_marker_version(tmp_path):
    target = tmp_path.parent / "outside"
    target.write_text("outside")
    (tmp_path / "unsafe-link").symlink_to(target)
    with pytest.raises(Exception, match="unsafe symlink|external symlink"):
        scan_workspace_for_status(tmp_path)

    marker = tmp_path / ".josh-room.json"
    marker.write_text(json.dumps({"format_version": 999, "x": "y"}))
    assert context_status(tmp_path)["state"] == "invalid"
    marker.write_text(json.dumps({"format_version": "9" * 5000}))
    assert context_status(tmp_path)["state"] == "invalid"


def test_v3_schema_requires_known_stat_algorithm_and_exact_fields():
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas/workspace-marker-v3.schema.json").read_text()
    )
    validator = Draft202012Validator(schema)
    marker = {
        "format_version": 3,
        "dimension_id": "dimension-a",
        "encryption_domain_id": "domain-a",
        "project_id": "project-a",
        "snapshot_id": "snapshot-a",
        "display_name": "Project A",
        "workspace_path_sha256": "a" * 64,
        "workspace_signature": "b" * 64,
        "signature_algorithm": "josh-room-stat-v1",
        "capture_policy_sha256": "c" * 64,
    }
    validator.validate(marker)
    with pytest.raises(ValidationError):
        validator.validate({**marker, "signature_algorithm": "future-stat-v2"})
    with pytest.raises(ValidationError):
        validator.validate({**marker, "private_repository": "synthetic"})


def test_v1_v2_local_status_keeps_full_content_fingerprint(tmp_path, monkeypatch):
    file = tmp_path / "note.txt"
    file.write_text("before")
    v2 = write_workspace_marker(
        tmp_path,
        dimension_id="dimension-a",
        project_id="project-a",
        display_name="Project A",
        snapshot_id="snapshot-a",
        workspace_fingerprint=workspace_state.workspace_fingerprint(tmp_path),
    )
    file.write_text("after")
    assert v2["format_version"] == 2
    assert local_status(tmp_path)["state"] == "changed"
    monkeypatch.setattr(
        "josh_room.room_store_operations.scan_workspace_for_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("legacy status uses v2 fingerprint")),
    )
    assert local_status(tmp_path)["state"] == "changed"
