import hashlib
import json

import pytest

from josh_room.catalog import Catalog
from josh_room.cli import build_parser
from josh_room.config import DimensionConfig, DimensionRegistry, resolve_dimension
from josh_room.operations import link_workspace, repair_workspace
from josh_room.r2 import R2Config
from josh_room.workspace_state import (
    canonical_workspace_path_sha256,
    read_workspace_marker,
    workspace_fingerprint,
    write_workspace_marker,
)


def _dimension(dimension_id="archive", provider="r2"):
    return DimensionConfig(
        dimension_id=dimension_id,
        display_name="Archive",
        provider=provider,
        endpoint="https://archive.example.invalid",
        bucket="archive-bucket",
        credential_profile="archive-profile",
        catalog_key="archive-catalog.jroom.age",
    )


def _snapshot(snapshot_id="jat-1", fingerprint=None):
    payload = b"ciphertext"
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "snapshot_id": snapshot_id,
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": len(payload),
        "created_at": "2026-08-26T00:00:00+00:00",
        "workspace_fingerprint": fingerprint or "a" * 64,
    }


def test_named_dimension_registry_selects_explicit_dimension_and_preserves_legacy_defaults():
    config = {
        "default_dimension": "archive",
        "dimensions": {"archive": _dimension().to_private()},
        "r2": {
            "endpoint": "https://legacy.example.invalid",
            "bucket": "legacy",
            "credential_profile": "legacy-profile",
        },
    }

    registry = DimensionRegistry(config)

    assert registry.select("archive").bucket == "archive-bucket"
    assert registry.select().dimension_id == "archive"
    assert registry.select("r2").display_name == "Cloudflare R2"
    assert resolve_dimension(config, "r2").provider == "r2"


def test_dimension_registry_rejects_unknown_dimension():
    with pytest.raises(ValueError, match="Dimension.*missing"):
        DimensionRegistry({}).select("missing")


def test_dimension_rejects_endpoint_whitespace_and_control_characters():
    with pytest.raises(ValueError, match="endpoint"):
        DimensionConfig.from_private(
            "archive",
            {**_dimension().to_private(), "endpoint": "https://bad host"},
        )
    with pytest.raises(ValueError, match="endpoint"):
        DimensionConfig.from_private(
            "archive",
            {**_dimension().to_private(), "endpoint": "https://bad.example.invalid/\n"},
        )


def test_r2_config_routes_all_nonsecret_dimension_settings():
    config = R2Config.from_dimension(_dimension())

    assert config.endpoint == "https://archive.example.invalid"
    assert config.bucket == "archive-bucket"
    assert config.credential_profile == "archive-profile"
    assert config.catalog_key == "archive-catalog.jroom.age"
    assert config.dimension_id == "archive"


def test_v2_catalog_is_bound_to_dimension_and_v1_can_be_migrated():
    catalog = Catalog.empty(dimension_id="archive").add_snapshot(
        "room", "Room", _snapshot()
    )

    assert catalog.body["format_version"] == 2
    assert catalog.body["dimension_id"] == "archive"
    assert catalog.resolve_snapshot("room", "jat-1")["workspace_fingerprint"] == "a" * 64
    migrated = Catalog.from_body(
        Catalog.empty().add_snapshot(
            "room",
            "Room",
            {k: v for k, v in _snapshot().items() if k not in {"created_at", "workspace_fingerprint"}},
        ).body,
        dimension_id="archive",
    )
    assert migrated.body["format_version"] == 2
    assert migrated.body["dimension_id"] == "archive"


def test_cli_exposes_dimension_routing_and_local_status_without_auth():
    parser = build_parser()
    args = parser.parse_args(["dimensions", "list", "--json"])
    assert args.command == "dimensions"
    args = parser.parse_args(["snapshot", "create", "room", "--dimension", "archive"])
    assert args.dimension == "archive"
    args = parser.parse_args(["status", "--json"])
    assert args.command == "status"


def test_fingerprint_is_deterministic_and_excludes_bookkeeping(tmp_path):
    (tmp_path / "file.txt").write_text("one")
    first = workspace_fingerprint(tmp_path)
    (tmp_path / ".josh-room.json").write_text('{"format_version": 2}')
    assert workspace_fingerprint(tmp_path) == first
    (tmp_path / "file.txt").write_text("two")
    assert workspace_fingerprint(tmp_path) != first


def test_marker_v2_round_trip_binds_dimension_room_jat_and_path(tmp_path):
    marker = write_workspace_marker(
        tmp_path,
        dimension_id="archive",
        project_id="room",
        display_name="Room",
        snapshot_id="jat-1",
        workspace_fingerprint="a" * 64,
    )

    assert marker["format_version"] == 2
    assert marker["workspace_path_sha256"] == canonical_workspace_path_sha256(tmp_path)
    assert read_workspace_marker(tmp_path) == marker


def test_link_requires_catalog_and_object_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = {
        "format_version": 2,
        "dimension_id": "archive",
        "project_id": "room",
        "display_name": "Room",
        "snapshot_id": "jat-1",
        "workspace_fingerprint": "a" * 64,
        "workspace_path_sha256": canonical_workspace_path_sha256(workspace),
    }
    (workspace / ".josh-room.json").write_text(json.dumps(marker))
    with pytest.raises(ValueError, match="object evidence"):
        link_workspace(workspace, Catalog.empty(dimension_id="archive"), object_evidence=None)


def test_link_recovers_missing_marker_from_explicit_catalog_and_disk_evidence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Room")
    fingerprint = workspace_fingerprint(workspace)
    snapshot = _snapshot(fingerprint=fingerprint)
    catalog = Catalog.empty(dimension_id="archive").add_snapshot("room", "Room", snapshot)
    evidence = {"project_id": "room", **snapshot}

    result = link_workspace(
        workspace,
        catalog,
        evidence,
        project_id="room",
        snapshot_id=snapshot["snapshot_id"],
        dimension_id="archive",
    )

    assert result["ok"] is True
    marker = read_workspace_marker(workspace)
    assert marker["dimension_id"] == "archive"
    assert marker["project_id"] == "room"
    assert marker["snapshot_id"] == snapshot["snapshot_id"]


def test_repair_replaces_stale_marker_only_after_disk_and_object_corroboration(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("Room")
    fingerprint = workspace_fingerprint(workspace)
    stale = write_workspace_marker(
        workspace,
        dimension_id="archive",
        project_id="old-room",
        display_name="Old Room",
        snapshot_id="old-snapshot",
        workspace_fingerprint="b" * 64,
    )
    snapshot = _snapshot(fingerprint=fingerprint)
    catalog = Catalog.empty(dimension_id="archive").add_snapshot("room", "Room", snapshot)
    evidence = {"project_id": "room", **snapshot}

    result = repair_workspace(
        workspace,
        catalog,
        evidence,
        project_id="room",
        snapshot_id=snapshot["snapshot_id"],
        dimension_id="archive",
    )

    assert result["project_id"] == "room"
    assert read_workspace_marker(workspace)["project_id"] == "room"
    assert read_workspace_marker(workspace)["workspace_fingerprint"] == fingerprint
    assert stale["project_id"] == "old-room"


def test_repair_rejects_disk_that_does_not_match_catalog_snapshot(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("changed")
    snapshot = _snapshot(fingerprint="a" * 64)
    catalog = Catalog.empty(dimension_id="archive").add_snapshot("room", "Room", snapshot)
    evidence = {"project_id": "room", **snapshot}

    with pytest.raises(ValueError, match="workspace fingerprint"):
        repair_workspace(
            workspace,
            catalog,
            evidence,
            project_id="room",
            snapshot_id=snapshot["snapshot_id"],
            dimension_id="archive",
        )


def test_repair_does_not_trust_stale_ledger_and_requires_corroboration(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="catalog"):
        repair_workspace(
            workspace,
            Catalog.empty(dimension_id="archive"),
            object_evidence={
                "snapshot_id": "stale",
                "ciphertext_sha256": "c" * 64,
                "ciphertext_size": 1,
            },
        )


def test_copy_snapshot_reuses_verified_ciphertext_and_creates_new_logical_jat():
    from josh_room.operations import copy_snapshot

    payload = b"ciphertext"
    digest = hashlib.sha256(payload).hexdigest()
    source_snapshot = _snapshot()
    source_catalog = Catalog.empty(dimension_id="archive").add_snapshot("room", "Room", source_snapshot)
    destination_catalog = Catalog.empty(dimension_id="backup")

    class Store:
        def __init__(self):
            self.puts = []

        def get_bytes(self, key, expected_digest=None, expected_size=None):
            assert key == source_snapshot["object_key"]
            assert expected_digest == digest
            assert expected_size == len(payload)
            return payload

        def put_bytes(self, key, body):
            self.puts.append((key, body))
            return type("Ref", (), {"key": key, "sha256": digest, "size": len(body)})()

    source_store = Store()
    destination_store = Store()
    result = copy_snapshot(source_catalog, destination_catalog, source_store, destination_store, "room")

    assert result["snapshot_id"] != source_snapshot["snapshot_id"]
    assert result["object_key"] == source_snapshot["object_key"]
    assert destination_store.puts == [(source_snapshot["object_key"], payload)]
    assert result["catalog"].body["projects"]["room"]["latest"] == result["snapshot_id"]


def test_snapshot_copy_parser_carries_source_destination_dimensions_and_rooms():
    parser = build_parser()
    args = parser.parse_args([
        "snapshot", "copy", "source-room",
        "--source-dimension", "archive",
        "--destination-dimension", "backup",
        "--destination-room", "restored-room",
        "--snapshot", "latest", "--json",
    ])
    assert args.snapshot_command == "copy"
    assert args.project == "source-room"
    assert args.source_dimension == "archive"
    assert args.destination_dimension == "backup"
    assert args.destination_project == "restored-room"
    assert args.snapshot == "latest"


def test_copy_snapshot_stream_downloads_once_puts_once_and_returns_new_jat(tmp_path):
    from josh_room.operations import copy_snapshot_stream

    source = _snapshot()
    source_catalog = Catalog.empty(dimension_id="archive").add_snapshot("source-room", "Source", source)
    destination_catalog = Catalog.empty(dimension_id="backup")
    payload = b"ciphertext"
    calls = []

    class Store:
        def download_file(self, key, destination, digest, size):
            calls.append(("download", key, digest, size))
            destination.write_bytes(payload)

        def put_file(self, key, path):
            calls.append(("put", key, path.read_bytes()))
            return type("Ref", (), {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})()

    result = copy_snapshot_stream(
        tmp_path / "instance", source_catalog, destination_catalog, Store(), Store(),
        "source-room", "restored-room", "latest", ["age1daily", "age1recovery"],
    )

    assert [call[0] for call in calls] == ["download", "put"]
    assert result["project_id"] == "restored-room"
    assert result["snapshot_id"] != source["snapshot_id"]
    assert result["catalog"].body["projects"]["restored-room"]["latest"] == result["snapshot_id"]


def test_copy_snapshot_stream_records_scrubbed_orphan_on_destination_catalog_conflict(tmp_path, monkeypatch):
    from josh_room.operations import copy_snapshot_stream

    source = _snapshot()
    source_catalog = Catalog.empty(dimension_id="archive").add_snapshot("source-room", "Source", source)
    destination_catalog = Catalog.empty(dimension_id="backup")
    payload = b"ciphertext"
    orphan_receipts = []

    class Store:
        def download_file(self, _key, destination, _digest, _size):
            destination.write_bytes(payload)

        def put_file(self, key, _path):
            return type("Ref", (), {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})()

        def conditional_catalog_put(self, _body, _etag):
            raise RuntimeError("catalog conflict")

        def record_orphan(self, ref):
            orphan_receipts.append({"object_key": ref.key, "sha256": ref.sha256, "size": ref.size})

    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"encrypted-catalog")
    with pytest.raises(RuntimeError, match="catalog conflict"):
        copy_snapshot_stream(
            tmp_path / "instance", source_catalog, destination_catalog, Store(), Store(),
            "source-room", "restored-room", "latest", ["age1daily", "age1recovery"],
        )
    assert orphan_receipts and set(orphan_receipts[0]) == {"object_key", "sha256", "size"}
    assert "secret" not in json.dumps(orphan_receipts)


def test_copy_snapshot_stream_records_orphan_when_destination_catalog_validation_fails(tmp_path):
    from josh_room.operations import copy_snapshot_stream

    source = _snapshot()
    source_catalog = Catalog.empty(dimension_id="archive").add_snapshot("source-room", "Source", source)
    payload = b"ciphertext"
    orphan_receipts = []

    class SourceStore:
        def download_file(self, _key, destination, _digest, _size):
            destination.write_bytes(payload)

    class DestinationStore:
        def put_file(self, key, _path):
            return type("Ref", (), {"key": key, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})()

        def record_orphan(self, ref):
            orphan_receipts.append({"object_key": ref.key, "sha256": ref.sha256, "size": ref.size})

    class BrokenCatalog:
        def __init__(self):
            self.body = {"format_version": 2}

        def add_snapshot(self, *_args):
            raise ValueError("destination catalog validation failed")

    with pytest.raises(ValueError, match="catalog validation"):
        copy_snapshot_stream(
            tmp_path / "instance", source_catalog, BrokenCatalog(), SourceStore(), DestinationStore(),
            "source-room", "restored-room", "latest", ["age1daily", "age1recovery"],
        )
    assert orphan_receipts == [{"object_key": source["object_key"], "sha256": source["ciphertext_sha256"], "size": source["ciphertext_size"]}]


def test_copy_parser_accepts_source_folder_without_explicit_source_room_or_jat(tmp_path):
    args = build_parser().parse_args([
        "snapshot", "copy",
        "--source-folder", str(tmp_path),
        "--destination-dimension", "backup",
        "--destination-room", "restored-room",
    ])
    assert args.project is None
    assert args.source_folder == tmp_path
    assert args.source_dimension is None
    assert args.snapshot == "latest"


def test_v2_snapshot_records_origin_project_for_cross_room_hydration():
    catalog = Catalog.empty(dimension_id="archive").add_snapshot("source-room", "Source", _snapshot())
    saved = catalog.resolve_snapshot("source-room", "jat-1")
    assert saved["origin_project_id"] == "source-room"


def test_manifest_binding_uses_origin_project_after_copy():
    from josh_room.operations import _manifest_matches_snapshot

    assert _manifest_matches_snapshot({"project_id": "source-room"}, "restored-room", {"origin_project_id": "source-room"})
    assert not _manifest_matches_snapshot({"project_id": "other-room"}, "restored-room", {"origin_project_id": "source-room"})


def test_hydrate_stage_marker_binds_final_destination_path(tmp_path):
    from josh_room.operations import _write_room_marker

    stage = tmp_path / "stage"
    destination = tmp_path / "final-room"
    fingerprint = "d" * 64
    _write_room_marker(
        stage, "room", "Room", dimension_id="archive", snapshot_id="jat-1",
        workspace_fp=fingerprint, path_binding=destination,
    )
    marker = read_workspace_marker(stage)

    assert marker["workspace_path_sha256"] == canonical_workspace_path_sha256(destination)
    assert marker["workspace_fingerprint"] == fingerprint
