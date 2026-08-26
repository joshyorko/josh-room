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
