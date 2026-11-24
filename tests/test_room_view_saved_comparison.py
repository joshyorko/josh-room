"""Pinned historical content, scoped live authority, and sanitization for Compare."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_recovery import file, synthetic

from josh_room.recovery_observation import observe
from josh_room.recovery_text import text_preview
from josh_room.room_store_lifecycle import RoomStoreLifecycleError
from josh_room.view.contract import ViewValidationError
from josh_room.view.room_provider import RoomWorkspaceProvider
from josh_room.view.saved_comparison import SavedComparison, _historical_file


def args(path, **fields):
    return SimpleNamespace(path=path, include_text=True, **{
        "expected_domain": None, "expected_workspace_snapshot": None,
        "expected_tree": None, "expected_ciphertext": None, **fields,
    })


def test_observe_enforces_all_expected_pin_fields_before_fetching_any_tree_or_file():
    node, blobs = file()
    context = synthetic([node], blobs).context
    row = context.selected_record
    expected = {"expected_tree": row["tree_id"], "expected_domain": row["encryption_domain_id"],
                    "expected_workspace_snapshot": row["workspace_snapshot_id"], "expected_ciphertext": row["ciphertext_sha256"]}
    result = observe(context, args("old.txt", **expected))
    assert result["file"]["sha256"] == hashlib.sha256(b"old\n").hexdigest()
    assert result["ciphertext_sha256"] == row["ciphertext_sha256"]
    context.store.snapshot = lambda *_: pytest.fail("snapshot query preceded pin validation")
    for field in expected:
        with pytest.raises(RoomStoreLifecycleError, match="Historical recovery"):
            observe(context, args("old.txt", **{**expected, field: "incorrect"}))


def test_saved_policy_refusal_precedes_content_fetch_and_digest_disclosure():
    node, blobs = file()
    ignore, policy_blob = file(".josh-roomignore", b"old.txt\n")
    context = synthetic([node, ignore], {**blobs, **policy_blob}).context
    read = context.store._capture
    def guarded(arguments):
        assert arguments[-1] not in blobs
        return read(arguments)
    context.store._capture = guarded
    with pytest.raises(RoomStoreLifecycleError) as error:
        observe(context, args("old.txt"))
    assert error.value.code == "recovery-observation-policy"


def test_file_absence_and_oversize_are_distinct_and_skip_unrelated_blobs():
    node, _blobs = file()
    node["size"] = 2 * 1024 * 1024
    context = synthetic([node], {}).context
    assert observe(context, args("missing.txt"))["file"] is None
    receipt = observe(context, args("old.txt"))
    assert receipt["file"] == {"type": "file", "size": node["size"], "sha256": None, "text_preview": {"state": "oversized"}}
    assert receipt["blob_bytes_read"] < 4096


@pytest.mark.parametrize("payload", [b"host=/home/private\n", b"api_key = synthetic\n", b"123456789012345"])
def test_saved_diff_applies_both_recovery_and_viewer_sanitizers(payload):
    envelope = text_preview("code.py", payload)
    value = {"type": "file", "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "text_preview": envelope}
    assert _historical_file("code.py", value)[1] is None


def test_digest_mismatch_never_reaches_browser():
    env = text_preview("a.txt", b"first")
    value = {"type": "file", "size": 5, "sha256": "a"*64, "text_preview": env}
    with pytest.raises(ViewValidationError):
        _historical_file("a.txt", value)


def test_selector_revalidates_selection_after_history_io(tmp_path, monkeypatch):
    target = tmp_path / "code.py"
    target.write_text("old\n")
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    try:
        page = adapter.listing("root")
        entry = next(node for node in page["entries"] if node["label"] == "code.py")
        comparison = SavedComparison(tmp_path / "broker")
        def drift(*_):
            target.write_text("new content\n")
            return {"points": [], "portable": 0}
        monkeypatch.setattr("josh_room.view.saved_comparison._query", drift)
        with pytest.raises(ViewValidationError) as error:
            comparison.query(adapter, entry["id"], page["generation"])
        assert error.value.code == "filesystem-stale"
    finally:
        adapter.close()


def test_custom_session_preview_limits_apply_to_saved_text():
    from josh_room.view.sanitizer import SanitizationPolicy
    value = b"version = 123\n"
    preview = text_preview("Makefile", value)
    file = {"type": "file", "size": len(value), "sha256": preview["sha256"], "text_preview": preview}
    assert _historical_file("tools/Makefile", file)[1] == value.decode()
    assert _historical_file("tools/Makefile", file, SanitizationPolicy(max_digit_run=2))[1] is None
    assert _historical_file("tools/Makefile", file, max_bytes=5)[1] is None



@pytest.mark.parametrize("include_previews,limit,digits", [(False, 512, 8), (True, 256, 8), (True, 512, 2)])
def test_comparison_is_digest_only_under_session_and_server_policy(tmp_path, monkeypatch, include_previews, limit, digits):
    from josh_room.view.sanitizer import SanitizationPolicy
    name = "code.py"
    saved = b"123" + b"x" * 260
    current = b"123" + b"y" * 260
    (tmp_path / name).write_bytes(current)
    provider = RoomWorkspaceProvider(tmp_path, include_previews=include_previews, max_preview_bytes=limit)
    service = SavedComparison(tmp_path / "broker", SanitizationPolicy(max_digit_run=digits))
    adapter = provider.open_filesystem()
    try:
        page = adapter.listing("root")
        entry = next(node for node in page["entries"] if node["label"] == name)
        preview = text_preview(name, saved)
        file = {"type": "file", "size": len(saved), "sha256": preview["sha256"], "text_preview": preview}
        monkeypatch.setattr("josh_room.view.saved_comparison._query", lambda *_: {
            "snapshot": "point-a", "date": "2026-11-23T00:00:00Z", "tree_id": "a" * 64,
            "workspace_snapshot_id": "b" * 64, "file": file,
        })
        started = datetime.now(UTC).replace(microsecond=0)
        receipt = service.query(adapter, entry["id"], page["generation"], "a" * 32)
        assert started <= datetime.fromisoformat(receipt["live_observed_at"]) <= datetime.now(UTC)
        assert receipt["path"] == name
        assert receipt["diff_status"] == (
            "preview-disabled" if not include_previews else "size-limit" if limit == 256 else "text-withheld"
        )
        assert receipt["change"] == "modified"
        assert receipt["saved"]["sha256"] == hashlib.sha256(saved).hexdigest()
        assert receipt["live"]["sha256"] == hashlib.sha256(current).hexdigest()
        assert receipt["unified_diff"] is None
    finally:
        adapter.close()


def test_comparison_does_not_disclose_a_redacted_path(tmp_path, monkeypatch):
    name = "record-123456789012345.py"
    (tmp_path / name).write_text("value = 4\n")
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    try:
        listing = adapter.listing("root")
        [entry] = listing["entries"]
        assert entry["label"] != name
        monkeypatch.setattr("josh_room.view.saved_comparison._query", lambda *_: {
            "snapshot": "point-a", "date": "2026-11-23T00:00:00Z", "tree_id": "a" * 64,
            "workspace_snapshot_id": "b" * 64, "file": None,
        })
        receipt = SavedComparison(tmp_path / "broker").query(adapter, entry["id"], listing["generation"], "a" * 32)
        assert receipt["path"] is None
        assert name not in str(receipt)
        assert receipt["change"] == "added"
    finally:
        adapter.close()


def test_native_view_launch_parser_accepts_broker_socket(tmp_path):
    from josh_room.cli import build_parser

    socket_path = tmp_path / "comparison.sock"
    options = build_parser().parse_args([
        "view", "serve", "--provider", "room", "--workspace", str(tmp_path),
        "--require-token", "--comparison-socket", str(socket_path), "--json",
    ])
    assert options.comparison_socket == socket_path
