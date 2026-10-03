from typing import ClassVar

import pytest

from josh_room.cli import build_parser


def test_snapshot_preview_and_deletion_confirmation_have_explicit_cli_forms(tmp_path):
    parser = build_parser()

    preview = parser.parse_args([
        "snapshot", "preview", "demo", "--source", str(tmp_path),
        "--backend", "minio", "--dimension", "archive",
    ])
    save = parser.parse_args([
        "snapshot", "create", "demo", "--source", str(tmp_path),
        "--backend", "minio", "--dimension", "archive",
        "--confirm-deletion", "token-synthetic",
    ])

    assert preview.snapshot_command == "preview"
    assert preview.project == "demo"
    from josh_room.cli import _requires_oauth

    assert _requires_oauth(preview) is False
    assert save.confirm_deletion == "token-synthetic"


def test_native_save_fails_closed_when_image_capture_is_requested(tmp_path, monkeypatch):
    from josh_room import cli

    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: type("Dimension", (), {"provider": "minio"})())
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(cli, "_recipients", lambda: ["age1synthetic", "age1recovery"])
    monkeypatch.setattr(cli, "save_room_store", lambda *_args, **_kwargs: pytest.fail("capture-dependent Save must not reach the bridge"))

    args = build_parser().parse_args([
        "snapshot", "create", "demo", "--source", str(tmp_path),
        "--backend", "minio", "--image", "example/image:latest",
    ])
    args._selected_encryption_material = object()

    with pytest.raises(ValueError, match="image capture is unavailable"):
        cli.dispatch(args, tmp_path / "instance")


def test_native_save_sends_rcc_workspace_to_component_resolver(tmp_path, monkeypatch):
    from josh_room import cli

    (tmp_path / "robot.yaml").write_text("tasks:\n  Build:\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: type("Dimension", (), {"provider": "minio"})())
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(
        cli,
        "save_room_store",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "snapshot_id": "logical-one"},
    )

    args = build_parser().parse_args([
        "snapshot", "create", "demo", "--source", str(tmp_path), "--backend", "minio",
    ])
    args._selected_encryption_material = object()

    assert cli.dispatch(args, tmp_path / "instance")["snapshot_id"] == "logical-one"
    assert calls[0][1]["components"] == []
    assert calls[0][1]["rcc_runtime"] is None


def test_minio_save_uses_room_store_bridge_without_jat_inputs(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    material = object()
    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    backend = object()
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(
        cli,
        "save_room_store",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "snapshot_id": "logical-one", "status": "saved"},
    )

    args = build_parser().parse_args([
        "snapshot", "create", "Demo", "--source", str(tmp_path),
        "--backend", "minio", "--dimension", "archive",
    ])
    args._selected_encryption_material = material

    result = cli.dispatch(args, tmp_path / "instance")

    assert result["ok"] is True
    assert calls
    call_args, call_kwargs = calls[0]
    assert call_args[:5] == (tmp_path / "instance", dimension, "demo", tmp_path, material)
    assert call_kwargs["components"] == []
    assert call_kwargs["confirmation_token"] is None


def test_minio_snapshot_preview_calls_read_only_bridge(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    material = object()
    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(
        cli,
        "preview_room_store",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "deleted_paths": []},
    )
    monkeypatch.setattr(cli, "save_room_store", lambda *_args, **_kwargs: pytest.fail("preview must not save"))

    args = build_parser().parse_args([
        "snapshot", "preview", "Demo", "--source", str(tmp_path),
        "--backend", "minio", "--dimension", "archive",
    ])
    args._selected_encryption_material = material

    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    call_args, call_kwargs = calls[0]
    assert call_args[:5] == (tmp_path / "instance", dimension, "demo", tmp_path, material)
    assert call_kwargs["components"] == []


def test_minio_preview_marks_rcc_capture_pending_without_running_it(tmp_path, monkeypatch):
    from josh_room import cli

    (tmp_path / "robot.yaml").write_text("tasks:\n  Build:\n", encoding="utf-8")
    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "preview_room_store", lambda *_args, **_kwargs: {"ok": True, "deleted_paths": []})

    args = build_parser().parse_args([
        "snapshot", "preview", "Demo", "--source", str(tmp_path),
        "--backend", "minio", "--dimension", "archive",
    ])
    args._selected_encryption_material = object()

    assert cli.dispatch(args, tmp_path / "instance")["rcc_capture_pending"] is True


def test_unknown_catalog_payload_kind_fails_closed_before_hydration(tmp_path, monkeypatch):
    from josh_room import cli

    class Catalog:
        body: ClassVar[dict] = {"projects": {"demo": {"display_name": "Demo"}}}

        def resolve_snapshot(self, _project, _snapshot):
            return {"payload_kind": "future-kind"}

    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(tmp_path / "identity"))
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: type("Dimension", (), {"provider": "minio"})())
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: Catalog())
    monkeypatch.setattr(cli, "hydrate", lambda *_args, **_kwargs: pytest.fail("unknown payload must fail closed"))
    monkeypatch.setattr(cli, "hydrate_room_store", lambda *_args, **_kwargs: pytest.fail("unknown payload must fail closed"))
    args = build_parser().parse_args([
        "hydrate", "demo", "--destination", str(tmp_path / "destination"),
        "--backend", "minio", "--dimension", "archive",
    ])
    args._selected_encryption_material = object()

    with pytest.raises(ValueError, match="unsupported snapshot payload kind"):
        cli.hydrate_command(args, tmp_path / "instance")


def test_native_hydration_routes_room_store_payload_and_legacy_routes_jat(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    material = object()
    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    backend = object()

    class Catalog:
        body: ClassVar[dict] = {"projects": {"demo": {"display_name": "Demo"}}}

        def __init__(self, payload_kind):
            self.payload_kind = payload_kind

        def resolve_snapshot(self, _project, _snapshot):
            return {} if self.payload_kind is None else {"payload_kind": self.payload_kind}

    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(tmp_path / "identity"))
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: Catalog("room-store-v1"))
    monkeypatch.setattr(
        cli,
        "hydrate_room_store",
        lambda *args, **kwargs: calls.append(("room-store", args, kwargs)) or {"destination": str(args[3]), "snapshot_id": "logical-one"},
    )
    monkeypatch.setattr(
        cli,
        "hydrate",
        lambda *args, **kwargs: calls.append(("legacy", args, kwargs)) or {"destination": str(args[2]), "snapshot_id": "legacy-one"},
    )

    args = build_parser().parse_args([
        "hydrate", "demo", "--snapshot", "selected", "--destination", str(tmp_path / "destination"),
        "--backend", "minio", "--dimension", "archive",
    ])
    args._selected_encryption_material = material
    result = cli.hydrate_command(args, tmp_path / "instance", backend)

    assert result["snapshot_id"] == "logical-one"
    assert calls[-1][0] == "room-store"
    assert calls[-1][2]["snapshot_id"] == "selected"

    monkeypatch.setattr(cli, "load_catalog", lambda *_args: Catalog(None))
    result = cli.hydrate_command(args, tmp_path / "instance", backend)

    assert result["snapshot_id"] == "legacy-one"
    assert calls[-1][0] == "legacy"


def test_enter_preserves_selected_dimension_and_material_for_native_hydration(tmp_path, monkeypatch):
    from josh_room import cli

    material = object()
    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    backend = object()
    captured = {}
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(cli, "_workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        cli,
        "hydrate_command",
        lambda args, *_rest: captured.update(vars(args)) or {"ok": True, "destination": str(tmp_path / "demo")},
    )
    args = build_parser().parse_args([
        "enter", "demo", "--ide", "terminal", "--dimension", "archive",
    ])
    args._selected_encryption_material = material

    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    assert captured["dimension"] == "archive"
    assert captured["_selected_encryption_material"] is material


def test_native_noop_save_has_explicit_human_receipt(capsys):
    from josh_room.cli import emit

    emit({"ok": True, "status": "already-saved", "data_added_bytes": 0}, False)

    assert capsys.readouterr().out.strip() == "Already saved — 0 bytes uploaded"


def test_preview_prints_the_deletion_confirmation_token(capsys):
    from josh_room.cli import emit

    emit({
        "ok": True,
        "current_entry_count": 4,
        "scanned_bytes": 10,
        "deleted_paths": ["file.txt"],
        "deletion_confirmation_token": "token-synthetic",
    }, False)

    output = capsys.readouterr().out
    assert "--confirm-deletion token-synthetic" in output


def test_save_confirmation_error_returns_a_retryable_human_command(capsys):
    from josh_room.cli import emit

    emit({
        "ok": False,
        "requires_confirmation": True,
        "confirmation_token": "token-synthetic",
        "error": "deletion-confirmation-required",
    }, False)

    output = capsys.readouterr().out
    assert "snapshot preview" in output
    assert "--confirm-deletion token-synthetic" in output


def test_json_confirmation_result_keeps_the_public_confirmation_token(capsys):
    import json

    from josh_room.cli import emit

    emit({
        "ok": False,
        "requires_confirmation": True,
        "confirmation_token": "token-synthetic",
        "error": "deletion-confirmation-required",
    }, True)

    result = json.loads(capsys.readouterr().out)
    assert result["confirmation_token"] == "token-synthetic"
