"""Fast negative admission tests; never contact providers or read live data."""

from dataclasses import replace

import pytest

from josh_room.composition_lifecycle import _child_view, _copy_paths, capture_plan
from josh_room.config import DimensionConfig


def fixture(tmp_path):
    source = tmp_path / "source"
    (source / "child/sub").mkdir(parents=True)
    (source / "child/sub/file.txt").write_text("approved synthetic bytes")
    dimension = DimensionConfig(
        "dimension",
        "Synthetic",
        "minio",
        "https://storage.example.invalid",
        "synthetic-bucket",
        "synthetic-profile",
        connection_id="connection",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    assignments = [
        {"path": ".", "outcome": "local-only"},
        {"path": "child", "outcome": "child"},
    ]
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    return source, dimension, assignments, preview


def test_late_child_file_never_enters_private_allowlist(tmp_path):
    source, _, _, preview = fixture(tmp_path)
    (source / "child/late.txt").write_text("not approved")
    with pytest.raises(ValueError, match="approved child inventory"):
        _child_view(source, "child", tmp_path / "view", preview["request"])
    assert not (tmp_path / "view").exists()


def test_payload_copy_refuses_substituted_ancestor_and_policy_symlink(tmp_path):
    source, _, _, _ = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("must not be copied")
    (source / "child/sub/file.txt").unlink()
    (source / "child/sub").rmdir()
    (source / "child/sub").symlink_to(outside, target_is_directory=True)
    target = tmp_path / "view"
    target.mkdir()
    with pytest.raises(OSError):
        _copy_paths(source / "child", target, {"sub/file.txt"})
    assert list(target.iterdir()) == []
    (source / "child/.josh-roomignore").symlink_to(outside / "file.txt")
    with pytest.raises(ValueError, match="regular file"):
        _copy_paths(source / "child", target, set())


def test_local_policy_at_child_root_is_supported_and_bound(tmp_path):
    source, dimension, assignments, _ = fixture(tmp_path)
    (source / "child/.josh-roomignore").write_text("generated/**\n")
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    assert (
        preview["request"]["child_evidence"]["child"]["capture_mode"] == "source-only"
    )
    changed = capture_plan(
        source,
        replace(dimension, bucket="other-bucket"),
        "1" * 32,
        "parent",
        assignments,
    )
    assert preview["approval_sha256"] != changed["approval_sha256"]


def test_copy_keeps_admitted_directory_when_original_path_is_replaced(tmp_path, monkeypatch):
    from josh_room import composition_lifecycle
    source, _, _, preview = fixture(tmp_path)
    original_copy = composition_lifecycle._copy_paths
    original = tmp_path / 'admitted-original'
    substituted = tmp_path / 'substituted'
    def replaced_path(*args, **kwargs):
        source.rename(original)
        (source / 'child/sub').mkdir(parents=True)
        (source / 'child/sub/file.txt').write_text('unapproved replacement payload')
        try:
            return original_copy(*args, **kwargs)
        finally:
            source.rename(substituted)
            original.rename(source)
    monkeypatch.setattr(composition_lifecycle, '_copy_paths', replaced_path)
    view = tmp_path / 'view'
    _child_view(source, 'child', view, preview['request'])
    assert (view / 'sub/file.txt').read_text() == 'approved synthetic bytes'


def test_capture_plan_runtime_refusal_is_bounded_json(tmp_path, monkeypatch, capsys):
    from josh_room import cli

    monkeypatch.setattr(
        cli,
        "dispatch",
        lambda *_: (_ for _ in ()).throw(RuntimeError("private-error-sentinel")),
    )
    code = cli.main(
        [
            "composition",
            "capture-plan",
            "--workspace",
            str(tmp_path),
            "--dimension",
            "dimension",
            "--namespace",
            "1" * 32,
            "--parent-room",
            "parent",
        ]
    )
    assert code != 0
    assert "private-error-sentinel" not in capsys.readouterr().out


def test_namespace_marker_fences_ordinary_save_without_guard_and_when_moved(tmp_path):
    import shutil

    from josh_room.composition import require_uncomposed_workspace
    from josh_room.room_store_operations import scan_workspace_for_status
    from josh_room.workspace_state import context_status, write_stat_workspace_marker

    root = tmp_path / "child"
    root.mkdir()
    (root / "file.txt").write_text("synthetic child")
    scan = scan_workspace_for_status(root)
    marker = write_stat_workspace_marker(
        root,
        dimension_id="dimension",
        project_id="child",
        snapshot_id="jat-child",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
        display_name="Child Room",
        workspace_signature=scan.signature,
        signature_algorithm=scan.signature_algorithm,
        capture_policy_sha256=scan.capture_policy_sha256,
        composition_namespace="1" * 32,
    )
    assert marker["format_version"] == 4
    assert context_status(root)["composition_namespace"] == "1" * 32
    with pytest.raises(ValueError, match="composed Save"):
        require_uncomposed_workspace(root)
    moved = tmp_path / "moved"
    shutil.move(root, moved)
    assert not context_status(moved)["ok"]
    with pytest.raises(ValueError, match="composed Save"):
        require_uncomposed_workspace(moved)
