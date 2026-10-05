import json
import os
import stat
from types import SimpleNamespace

from josh_room import cli, workspace_state
from josh_room.workspace_state import (
    canonical_workspace_path_sha256,
    context_status,
)


def _marker(workspace, **updates):
    marker = {
        "format_version": 2,
        "dimension_id": "dimension-a",
        "project_id": "project-a",
        "display_name": "Project A",
        "snapshot_id": "snapshot-a",
        "workspace_fingerprint": "a" * 64,
        "workspace_path_sha256": canonical_workspace_path_sha256(workspace),
    }
    marker.update(updates)
    return marker


def test_context_reports_missing_marker_as_unlinked(tmp_path):
    result = context_status(tmp_path)
    assert result == {
        "format_version": 1,
        "ok": True,
        "state": "unlinked",
        "linked": False,
        "path_matches": False,
    }


def test_status_include_context_is_versioned_and_keeps_changed_receipt(tmp_path, monkeypatch):
    (tmp_path / "file.txt").write_text("before")
    marker = _marker(tmp_path, workspace_fingerprint=workspace_state.workspace_fingerprint(tmp_path))
    (tmp_path / ".josh-room.json").write_text(json.dumps(marker))
    (tmp_path / "file.txt").write_text("changed")
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: (_ for _ in ()).throw(AssertionError("must not create provider")))
    args = cli.build_parser().parse_args(["status", "--workspace", str(tmp_path), "--include-context", "--json"])
    result = cli.dispatch(args, tmp_path / "unused")
    assert result["format_version"] == 1
    assert result["state"] == "changed"
    assert result["ok"] is False
    assert result["context"]["state"] == "linked"
    assert result["context"]["path_matches"] is True
    assert result["project_id"] == result["context"]["project_id"]
    assert result["snapshot_id"] == result["context"]["snapshot_id"]


def test_context_reports_valid_v2_marker_without_fingerprinting(tmp_path, monkeypatch):
    (tmp_path / ".josh-room.json").write_text(json.dumps(_marker(tmp_path)))
    monkeypatch.setattr(
        "josh_room.workspace_state.workspace_fingerprint",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not fingerprint")),
    )
    assert context_status(tmp_path) == {
        "format_version": 1,
        "ok": True,
        "state": "linked",
        "linked": True,
        "path_matches": True,
        "dimension_id": "dimension-a",
        "project_id": "project-a",
        "display_name": "Project A",
        "snapshot_id": "snapshot-a",
    }


def test_context_rejects_copied_marker_without_exposing_path_or_hash(tmp_path):
    original = tmp_path / "original"
    copied = tmp_path / "copied"
    original.mkdir()
    copied.mkdir()
    (copied / ".josh-room.json").write_text(json.dumps(_marker(original)))
    result = context_status(copied)
    assert result["state"] == "invalid"
    assert result["ok"] is False
    assert result["linked"] is False
    assert result["path_matches"] is False
    assert not ({"workspace_path_sha256", "workspace_fingerprint", "error", "path"} & result.keys())


def test_context_rejects_malformed_nonobject_marker(tmp_path):
    (tmp_path / ".josh-room.json").write_text(json.dumps(["not", "an", "object"]))
    result = context_status(tmp_path)
    assert result["state"] == "invalid"
    assert result["ok"] is False
    assert result["linked"] is False


def test_context_rejects_oversized_marker(tmp_path):
    (tmp_path / ".josh-room.json").write_text(" " * (64 * 1024 + 1))
    result = context_status(tmp_path)
    assert result["state"] == "invalid"
    assert result["ok"] is False


def test_context_rejects_symlinked_marker_without_reading_target(tmp_path, monkeypatch):
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_marker(tmp_path)))
    (tmp_path / ".josh-room.json").symlink_to(target)
    result = context_status(tmp_path)
    assert result["state"] == "invalid"
    assert result["ok"] is False


def test_context_rejects_marker_replaced_with_special_file_after_lstat(tmp_path, monkeypatch):
    marker_path = tmp_path / ".josh-room.json"
    marker_path.write_text(json.dumps(_marker(tmp_path)))
    real_open = os.open
    opened_flags = []

    def racing_open(path, flags):
        opened_flags.append(flags)
        return real_open(path, flags)

    monkeypatch.setattr(workspace_state.os, "open", racing_open)
    monkeypatch.setattr(
        workspace_state.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFIFO | 0o600, st_dev=1, st_ino=2, st_size=0),
    )
    monkeypatch.setattr(workspace_state.os, "read", lambda *_args: (_ for _ in ()).throw(AssertionError("special file read")))

    result = context_status(tmp_path)
    assert result["state"] == "invalid"
    assert result["ok"] is False
    assert opened_flags[0] & os.O_NONBLOCK


def test_context_rejects_marker_replaced_with_different_inode_after_lstat(tmp_path, monkeypatch):
    marker_path = tmp_path / ".josh-room.json"
    marker_path.write_text(json.dumps(_marker(tmp_path)))
    metadata = marker_path.stat()
    real_open = os.open
    monkeypatch.setattr(workspace_state.os, "open", lambda path, flags: real_open(path, flags | os.O_NONBLOCK))
    monkeypatch.setattr(
        workspace_state.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino + 1,
            st_size=metadata.st_size,
        ),
    )
    monkeypatch.setattr(workspace_state.os, "read", lambda *_args: (_ for _ in ()).throw(AssertionError("replaced file read")))

    result = context_status(tmp_path)
    assert result["state"] == "invalid"
    assert result["ok"] is False


def test_context_rejects_legacy_and_unsupported_marker_versions(tmp_path):
    marker_path = tmp_path / ".josh-room.json"
    for marker in (
        {"format_version": 1, "project_id": "project-a", "display_name": "Project A"},
        {"format_version": 3},
    ):
        marker_path.write_text(json.dumps(marker))
        result = context_status(tmp_path)
        assert result["state"] == "invalid"
        assert result["ok"] is False


def test_context_cli_skips_global_runtime_and_result_file_setup(tmp_path, monkeypatch, capsys):
    (tmp_path / ".josh-room.json").write_text(json.dumps(_marker(tmp_path)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("context must remain offline and instance-free")

    monkeypatch.setattr(cli, "initialize_system_trust", forbidden)
    monkeypatch.setattr(cli, "_instance_root", forbidden)
    monkeypatch.setattr(cli, "_identity_environment", forbidden)
    monkeypatch.setattr(cli, "load_runtime_session", forbidden)
    monkeypatch.setattr(cli, "ensure_runtime_session", forbidden)
    monkeypatch.setattr(cli, "resolve_encryption_material", forbidden)
    monkeypatch.setattr(cli, "load_catalog", forbidden)
    monkeypatch.setattr(cli, "_backend", forbidden)
    monkeypatch.setattr(cli._r2, "R2Backend", forbidden)
    monkeypatch.setattr(cli, "_write_runtime_result", forbidden)
    assert cli.main(["context", "--workspace", str(tmp_path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "linked"
    assert output["linked"] is True
