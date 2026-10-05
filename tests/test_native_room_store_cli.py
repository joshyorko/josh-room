from contextlib import contextmanager
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


def test_room_store_lifecycle_actions_have_explicit_cli_contracts(tmp_path):
    parser = build_parser()
    cases = [
        ["snapshot", "inspect", "demo", "--snapshot", "jat-one", "--dimension", "archive"],
        ["snapshot", "export", "demo", "--snapshot", "jat-one", "--output", str(tmp_path / "demo.haul"), "--dimension", "archive"],
        ["snapshot", "serve", "demo", "--snapshot", "jat-one", "--mode", "files", "--dimension", "archive"],
        ["snapshot", "extract", "demo", "hauler/app:latest", "--snapshot", "jat-one", "--destination", str(tmp_path / "extract"), "--dimension", "archive"],
        ["room-store", "verify", "--dimension", "archive", "--read-data-subset", "10%"],
        ["room-store", "optimize", "--dimension", "archive", "--confirm"],
        ["room-store", "reconcile", "--dimension", "archive"],
    ]

    parsed = [parser.parse_args(vector) for vector in cases]

    assert [args.snapshot_command for args in parsed[:4]] == ["inspect", "export", "serve", "extract"]
    assert [args.room_store_command for args in parsed[4:]] == ["verify", "optimize", "reconcile"]


def test_native_save_passes_local_image_selection_to_the_opened_store(tmp_path, monkeypatch):
    from josh_room import cli

    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: type("Dimension", (), {"provider": "minio"})())
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(cli, "_recipients", lambda: ["age1synthetic", "age1recovery"])
    calls = []
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "managed-jat")
    monkeypatch.setattr(cli, "save_room_store", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})

    args = build_parser().parse_args([
        "snapshot", "create", "demo", "--source", str(tmp_path),
        "--backend", "minio", "--image", "example/image:latest",
    ])
    args._selected_encryption_material = object()

    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    assert calls[0]["hauler_selection"]["images"] == ["example/image:latest"]
    assert calls[0]["hauler_selection"]["all_images"] is False
    assert calls[0]["jat_root"] == tmp_path / "managed-jat"


def test_native_preview_accepts_capture_selection_without_capturing(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: type("Dimension", (), {"provider": "minio"})())
    monkeypatch.setattr(cli, "preview_room_store", lambda *args, **kwargs: calls.append(kwargs) or {"ok": True})
    args = build_parser().parse_args([
        "snapshot", "preview", "demo", "--source", str(tmp_path),
        "--backend", "minio", "--all-images",
        "--hauler-file", str(tmp_path / "extra.txt"), "extra.txt",
        "--hauler-manifest", str(tmp_path / "manifest.yaml"),
        "--brew-archive", str(tmp_path / "brew.tar.zst"),
    ])
    args._selected_encryption_material = object()

    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    assert calls[0]["hauler_selection"] == {
        "images": [], "all_images": True,
        "manifests": [tmp_path / "manifest.yaml"],
        "files": [(tmp_path / "extra.txt", "extra.txt")],
    }
    assert calls[0]["homebrew_archive"] == tmp_path / "brew.tar.zst"


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


def test_r2_save_uses_native_bridge_and_leaves_material_resolution_to_bridge(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    dimension = type("Dimension", (), {"provider": "r2", "dimension_id": "cloud"})()
    monkeypatch.setattr(cli, "_room_identity", lambda _value: ("demo", "Demo"))
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(
        cli,
        "save_room_store",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "status": "saved"},
    )
    args = build_parser().parse_args([
        "snapshot", "create", "Demo", "--source", str(tmp_path),
        "--backend", "r2", "--dimension", "cloud",
    ])

    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    assert calls[0][0][1] is dimension
    assert calls[0][0][4] is None
    assert calls[0][1]["components"] == []


def test_snapshot_inspect_routes_directly_to_descriptor_view_without_export(tmp_path, monkeypatch):
    from josh_room import cli

    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    material = object()
    backend = object()
    catalog = type("Catalog", (), {
        "resolve_snapshot": lambda _self, _project, snapshot: {"snapshot_id": snapshot, "payload_kind": "room-store-v1"}
    })()
    record = catalog.resolve_snapshot("demo", "jat-one")
    descriptor = object()

    @contextmanager
    def context(*_args, **_kwargs):
        yield type("Context", (), {"selected_record": record, "selected_descriptor": descriptor})()

    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: catalog)
    monkeypatch.setattr(cli, "_open_room_store_context", context)
    monkeypatch.setattr(cli, "inspect_logical_jat", lambda _record, selected: {"logical_jat_id": "jat-one"} if selected is descriptor else pytest.fail("selected descriptor mismatch"))
    monkeypatch.setattr(cli, "export_logical_jat", lambda *_args, **_kwargs: pytest.fail("Inspect must not export"))
    args = build_parser().parse_args([
        "snapshot", "inspect", "demo", "--snapshot", "jat-one", "--dimension", "archive",
    ])
    args._selected_encryption_material = material

    result = cli.dispatch(args, tmp_path / "instance")

    assert result == {"ok": True, "logical_jat_id": "jat-one"}


def test_snapshot_export_serve_and_extract_use_selected_descriptor_and_jat_root(tmp_path, monkeypatch):
    from josh_room import cli

    record = {"snapshot_id": "selected", "payload_kind": "room-store-v1"}
    descriptor = object()
    context = type("Context", (), {"selected_record": record, "selected_descriptor": descriptor})()
    calls = []

    @contextmanager
    def room_context(*_args, **kwargs):
        calls.append(("context", kwargs["project_id"], kwargs["snapshot_id"]))
        yield context

    monkeypatch.setattr(cli, "_open_room_store_context", room_context)
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "jat-root")
    monkeypatch.setattr(cli, "export_logical_jat", lambda selected, **kwargs: calls.append(("export", selected, kwargs)) or {"ok": True})
    monkeypatch.setattr(cli, "serve_logical_jat", lambda selected, **kwargs: calls.append(("serve", selected, kwargs)) or {"ok": True})
    monkeypatch.setattr(cli, "extract_logical_jat", lambda selected, reference, destination, **kwargs: calls.append(("extract", selected, reference, destination, kwargs)) or {"ok": True})

    cases = [
        (["snapshot", "export", "demo", "--snapshot", "selected", "--output", str(tmp_path / "demo.haul")], "export"),
        (["snapshot", "serve", "demo", "--snapshot", "selected", "--mode", "files"], "serve"),
        (["snapshot", "extract", "demo", "hauler/app:latest", "--snapshot", "selected", "--destination", str(tmp_path / "extract")], "extract"),
    ]
    for vector, operation in cases:
        args = build_parser().parse_args(vector)
        assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
        assert calls[-2][0:3] == ("context", "demo", "selected")
        call = calls[-1]
        assert call[0] == operation
        assert call[1] is context
        assert call[-1]["jat_root"] == tmp_path / "jat-root"


@pytest.mark.parametrize("remove_snapshot", [False, True])
def test_native_room_removal_publishes_catalog_cas_before_fresh_reachability_check(tmp_path, monkeypatch, remove_snapshot):
    from josh_room import cli

    dimension = type("Dimension", (), {"provider": "minio", "dimension_id": "archive"})()
    catalog = type("Catalog", (), {
        "body": {"projects": {"demo": {"snapshots": {
            "one": {"payload_kind": "room-store-v1"},
            "two": {"payload_kind": "room-store-v1"},
        }}}},
        "resolve_snapshot": lambda self, project, snapshot: {
            "snapshot_id": snapshot, **self.body["projects"][project]["snapshots"][snapshot],
        },
    })()
    if remove_snapshot:
        del catalog.body["projects"]["demo"]["snapshots"]["two"]
    events = []

    @contextmanager
    def room_context(_args, _instance, *, project_id=None, snapshot_id="latest", writable=False):
        if project_id is not None and project_id not in catalog.body["projects"]:
            raise ValueError("removed Room is unavailable")
        events.append(("context", project_id, snapshot_id, writable))
        yield object()

    def remove_records(_context, identities):
        events.append(("cas", identities))
        del catalog.body["projects"]["demo"]
        return "pending"

    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: object())
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: catalog)
    monkeypatch.setattr(cli, "_open_room_store_context", room_context)
    monkeypatch.setattr(cli, "remove_logical_catalog_records", remove_records)
    monkeypatch.setattr(cli, "complete_logical_catalog_removal", lambda _context, pending: events.append(("recheck", pending)) or {"ok": True})

    vector = ["snapshots", "remove", "demo", "one"] if remove_snapshot else ["rooms", "remove", "demo"]
    args = build_parser().parse_args([*vector, "--backend", "minio"])

    assert cli.dispatch(args, tmp_path / "instance") == {"ok": True}
    assert events == [
        ("context", "demo", "one" if remove_snapshot else "latest", True),
        ("cas", [("demo", "one")] if remove_snapshot else [("demo", "one"), ("demo", "two")]),
        ("context", None, "latest", False),
        ("recheck", "pending"),
    ]


@pytest.mark.parametrize(
    ("destination_dimension", "expected_operation"),
    [("archive", "same"), ("cloud", "cross")],
)
def test_native_copy_routes_to_typed_same_or_cross_dimension_operation(
    tmp_path, monkeypatch, destination_dimension, expected_operation
):
    from josh_room import cli

    record = {"snapshot_id": "selected", "payload_kind": "room-store-v1"}
    catalog = type("Catalog", (), {"resolve_snapshot": lambda _self, _room, _snapshot: record})()
    dimensions = {
        name: type("Dimension", (), {"dimension_id": name, "provider": "r2"})()
        for name in ("archive", "cloud")
    }
    contexts = []
    operations = []

    class Registry:
        def __init__(self, _config):
            pass

        def select(self, name):
            return dimensions[name]

    @contextmanager
    def room_context(_args, _instance, **kwargs):
        context = type("Context", (), {"dimension_id": kwargs["dimension_id"]})()
        contexts.append((context, kwargs))
        yield context

    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "DimensionRegistry", Registry)
    monkeypatch.setattr(cli, "_backend", lambda *_args: object())
    monkeypatch.setattr(cli, "_identity", lambda: tmp_path / "identity")
    monkeypatch.setattr(cli, "_read_remote_catalog", lambda *_args, **_kwargs: (catalog, "etag"))
    monkeypatch.setattr(cli, "_room_identity", lambda room: (room, room.title()))
    monkeypatch.setattr(cli, "_open_room_store_context", room_context)
    monkeypatch.setattr(cli, "copy_logical_jat_as_new", lambda *args: operations.append(("same", args)) or {"ok": True})
    monkeypatch.setattr(cli, "copy_logical_jat_to_dimension", lambda *args: operations.append(("cross", args)) or {"ok": True})
    monkeypatch.setattr(cli, "copy_snapshot_stream", lambda *_args, **_kwargs: pytest.fail("native Copy must use a logical operation"))

    args = build_parser().parse_args([
        "snapshot", "copy", "demo", "--source-dimension", "archive",
        "--snapshot", "selected", "--destination-dimension", destination_dimension,
        "--destination-room", "new-room",
    ])

    assert cli.dispatch(args, tmp_path / "instance") == {"ok": True}
    assert [call[1].get("snapshot_id", "latest") for call in contexts] == ["selected", "latest"]
    assert operations[0][0] == expected_operation
    assert operations[0][1][0] is contexts[0][0]
    assert operations[0][1][1] is contexts[1][0]


def test_room_store_verify_cli_passes_only_selected_read_scope(tmp_path, monkeypatch):
    from josh_room import cli

    calls = []
    dimension = type("Dimension", (), {"provider": "r2", "dimension_id": "cloud"})()
    store = type("Store", (), {
        "check": lambda _self, **kwargs: calls.append(kwargs) or type(
            "Check", (), {"read_data": kwargs["read_data"], "read_data_subset": kwargs["read_data_subset"]}
        )(),
    })()

    @contextmanager
    def context(*_args, **_kwargs):
        yield type("Context", (), {"store": store})()

    monkeypatch.setattr(cli, "private_config", dict)
    monkeypatch.setattr(cli, "DimensionRegistry", lambda _config: type("Registry", (), {"select": lambda _self, _name: dimension})())
    monkeypatch.setattr(cli, "_open_room_store_context", context)
    args = build_parser().parse_args([
        "room-store", "verify", "--dimension", "cloud", "--read-data-subset", "10%",
    ])

    result = cli.dispatch(args, tmp_path / "instance")

    assert result["status"] == "verified"
    assert calls == [{"read_data": False, "read_data_subset": "10%"}]


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
            if self.payload_kind is None:
                return {"snapshot_id": _snapshot}
            return {"snapshot_id": _snapshot, "payload_kind": self.payload_kind}

    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(tmp_path / "identity"))
    monkeypatch.setattr(cli, "_backend_for_args", lambda *_args: backend)
    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(cli, "_jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: Catalog("room-store-v1"))
    @contextmanager
    def room_context(*_args, **_kwargs):
        yield type(
            "RoomStoreContext",
            (),
            {
                "selected_record": {"snapshot_id": "selected", "payload_kind": "room-store-v1"},
                "material": material,
                "authority_session": None,
            },
        )()

    monkeypatch.setattr(cli, "_open_room_store_context", room_context)
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
    assert calls[-1][2]["jat_root"] == tmp_path / "jat"

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
