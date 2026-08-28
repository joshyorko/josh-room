import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest

from josh_room import cli, keyring, minio
from josh_room import config as config_module
from josh_room.catalog import Catalog


def _connection(provider="minio", connection_id="minio-dev"):
    return {
        "display_name": "MinIO development",
        "provider": provider,
        "endpoint": "https://minio.example.invalid:9000",
        "credential_profile": "minio-dev-profile",
        "region": "us-east-1",
    }


def _dimension(connection_id="minio-dev", bucket="room-a"):
    return {
        "display_name": bucket.title(),
        "connection_id": connection_id,
        "bucket": bucket,
        "catalog_key": f"{bucket}.jroom.age",
    }


def test_modern_dimensions_reference_reusable_connections_and_legacy_records_migrate():
    assert hasattr(config_module, "ConnectionConfig")
    config = {
        "connections": {"minio-dev": _connection()},
        "dimensions": {
            "room-a": _dimension(),
            "room-b": _dimension(bucket="room-b"),
        },
        "r2": {
            "endpoint": "https://legacy-r2.example.invalid",
            "bucket": "legacy-room",
            "credential_profile": "legacy-r2-profile",
        },
    }

    connections = config_module.connection_configs(config)
    dimensions = config_module.dimension_configs(config)

    assert connections["minio-dev"].provider == "minio"
    assert dimensions["room-a"].connection_id == "minio-dev"
    assert dimensions["room-b"].connection_id == "minio-dev"
    assert dimensions["room-a"].endpoint == connections["minio-dev"].endpoint
    assert dimensions["room-a"].bucket == "room-a"
    assert dimensions["room-a"].to_private() == _dimension()
    assert dimensions["r2"].provider == "r2"
    assert dimensions["r2"].bucket == "legacy-room"


def test_minio_connection_setup_reads_credentials_from_json_stdin_and_persists_only_keyring_reference(
    tmp_path, monkeypatch, capsys
):
    stored = []
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "store_keyring", lambda profile, values: stored.append((profile, values)))
    payload = {
        "endpoint": "http://minio.home.arpa:9000",
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert cli.main([
        "connections", "setup", "minio", "--connection", "home-minio",
        "--credential-profile", "home-profile", "--json",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    saved = json.loads((tmp_path / "config.json").read_text())
    assert report == {"connection": "home-minio", "ok": True, "stored": True}
    assert stored == [("home-profile", {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    })]
    serialized = json.dumps(saved)
    assert "synthetic-access" not in serialized
    assert "synthetic-secret" not in serialized
    assert saved["connections"]["home-minio"] == {
        "display_name": "MinIO",
        "provider": "minio",
        "endpoint": "http://minio.home.arpa:9000",
        "credential_profile": "home-profile",
        "region": "us-east-1",
    }


def test_extension_minio_connection_does_not_require_secret_service(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setattr(cli, "store_keyring", lambda *_args: pytest.fail("extension must use VS Code SecretStorage"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "endpoint": "http://minio.home.arpa:9000",
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    })))

    assert cli.main([
        "connections", "setup", "minio", "--connection", "extension-minio",
        "--credential-profile", "extension-profile", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["stored"] is True


def test_minio_connection_client_uses_arbitrary_endpoint_and_existing_keyring(monkeypatch):
    captured = {}
    def lookup(profile, **_kwargs):
        captured["lookup"] = profile
        return {"access-key-id": "synthetic-access", "secret-access-key": "synthetic-secret"}

    monkeypatch.setattr(minio, "lookup", lookup)
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: captured.update(kwargs) or object())

    connection = config_module.ConnectionConfig.from_private("home-minio", _connection())
    minio.client_for_connection(connection)

    assert captured["lookup"] == "minio-dev-profile"
    assert captured["endpoint_url"] == "https://minio.example.invalid:9000"
    assert captured["aws_access_key_id"] == "synthetic-access"
    assert captured["aws_secret_access_key"] == "synthetic-secret"


def test_minio_profile_never_consumes_unrelated_r2_runtime_credentials(tmp_path, monkeypatch):
    runtime = tmp_path / "r2.json"
    runtime.write_text(json.dumps({
        "access-key-id": "r2-runtime-access",
        "secret-access-key": "r2-runtime-secret",
        "session-token": "r2-runtime-session",
    }))
    runtime.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(runtime))
    monkeypatch.setattr(keyring, "available", lambda: True)

    def secret_tool(argv, **_kwargs):
        field = argv[-1]
        values = {
            "access-key-id": "minio-access",
            "secret-access-key": "minio-secret",
        }
        return SimpleNamespace(returncode=0 if field in values else 1, stdout=values.get(field, ""))

    monkeypatch.setattr(keyring.subprocess, "run", secret_tool)

    assert keyring.lookup("minio-profile", allow_runtime=False) == {
        "access-key-id": "minio-access",
        "secret-access-key": "minio-secret",
    }
    assert keyring.lookup("oauth-runtime", allow_runtime=True) == {
        "access-key-id": "r2-runtime-access",
        "secret-access-key": "r2-runtime-secret",
        "session-token": "r2-runtime-session",
    }


def test_disconnected_minio_bucket_connection_fails_closed_before_keyring(monkeypatch):
    connection = config_module.ConnectionConfig.from_private(
        "home-minio",
        {**_connection(), "auth_state": "disconnected"},
    )
    monkeypatch.setattr(minio, "lookup", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("disconnected connection must not read credentials")
    ))

    with pytest.raises(RuntimeError, match="disconnected"):
        minio.client_for_connection(connection)


def test_bucket_list_success_returns_accessible_names():
    class Client:
        def list_buckets(self):
            return {"Buckets": [{"Name": "room-a"}, {"Name": "room-b"}]}

    connection = config_module.ConnectionConfig.from_private("home-minio", _connection())
    assert minio.list_buckets(connection, client=Client()) == ["room-a", "room-b"]


def test_bucket_list_forbidden_is_structured_and_recoverable():
    from botocore.exceptions import ClientError

    class Client:
        def list_buckets(self):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListBuckets")

    connection = config_module.ConnectionConfig.from_private("home-minio", _connection())
    with pytest.raises(minio.BucketListForbidden) as failure:
        minio.list_buckets(connection, client=Client())

    assert failure.value.result == {
        "error_code": "bucket-list-forbidden",
        "recoverable": True,
        "connection_id": "home-minio",
        "provider": "minio",
    }


def test_bucket_create_requires_explicit_validated_name():
    calls = []

    class Client:
        def create_bucket(self, **kwargs):
            calls.append(kwargs)

    connection = config_module.ConnectionConfig.from_private("home-minio", _connection())
    assert minio.create_bucket(connection, "new-room", client=Client()) == "new-room"
    assert calls == [{"Bucket": "new-room"}]
    with pytest.raises(ValueError, match="bucket"):
        minio.create_bucket(connection, "../unsafe", client=Client())


def test_bucket_access_check_validates_manual_bucket_before_dimension_creation():
    calls = []

    class Client:
        def head_bucket(self, **kwargs):
            calls.append(kwargs)

    connection = config_module.ConnectionConfig.from_private("home-minio", _connection())
    assert minio.check_bucket_access(connection, "room-a", client=Client()) == "room-a"
    assert calls == [{"Bucket": "room-a"}]


def test_connections_and_dimensions_cli_show_reuse_and_bucket_ownership(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "connections": {"home-minio": _connection()},
        "dimensions": {"room-a": _dimension(connection_id="home-minio"), "room-b": _dimension(connection_id="home-minio", bucket="room-b")},
    }))

    assert cli.main(["connections", "list", "--json"]) == 0
    connections = json.loads(capsys.readouterr().out)
    assert connections["connections"] == [{
        "id": "home-minio",
        "display_name": "MinIO development",
        "provider": "minio",
        "endpoint": "https://minio.example.invalid:9000",
        "credential_profile": "minio-dev-profile",
        "auth_state": "configured",
    }]

    assert cli.main(["dimensions", "list", "--json"]) == 0
    dimensions = json.loads(capsys.readouterr().out)["dimensions"]
    assert {item["connection_id"] for item in dimensions} == {"home-minio"}
    assert {item["bucket"] for item in dimensions} == {"room-a", "room-b"}


def test_bucket_cli_uses_connection_and_returns_recoverable_forbidden(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"connections": {"home-minio": _connection()}}))
    monkeypatch.setattr(cli, "list_minio_buckets", lambda _connection: (_ for _ in ()).throw(
        minio.BucketListForbidden("denied")
    ))

    assert cli.main(["buckets", "list", "--connection", "home-minio", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error_code"] == "bucket-list-forbidden"
    assert report["recoverable"] is True


def test_minio_disconnect_reconnect_transition_blocks_then_restores_operations(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "connections": {"home-minio": _connection()},
    }))

    assert cli.main([
        "provider", "connection", "disconnect", "--connection", "home-minio", "--json",
    ]) == 0
    capsys.readouterr()
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["connections"]["home-minio"]["auth_state"] == "disconnected"

    assert cli.main([
        "provider", "bucket", "list", "--connection", "home-minio", "--json",
    ]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert "disconnected" in blocked["error"]

    stored = []
    monkeypatch.setattr(cli, "store_keyring", lambda profile, values: stored.append((profile, values)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    })))
    assert cli.main([
        "provider", "connection", "reconnect", "--connection", "home-minio",
        "--json",
    ]) == 0
    capsys.readouterr()
    assert stored
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["connections"]["home-minio"]["auth_state"] == "configured"

    monkeypatch.setattr(cli, "list_minio_buckets", lambda _connection: ["room-a"])
    assert cli.main([
        "provider", "bucket", "list", "--connection", "home-minio", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["buckets"] == ["room-a"]


def test_durable_dimension_mutation_does_not_promote_runtime_overlay(tmp_path, monkeypatch, capsys):
    persisted = {
        "connections": {"home-minio": _connection()},
        "dimensions": {"room-a": _dimension(connection_id="home-minio")},
    }
    runtime = {
        **persisted,
        "r2": {
            "endpoint": "https://runtime.example.invalid",
            "bucket": "runtime-room",
            "credential_profile": "oauth-runtime",
        },
        "dimensions": {
            **persisted["dimensions"],
            "r2": {
                "display_name": "Cloudflare R2",
                "provider": "r2",
                "endpoint": "https://runtime.example.invalid",
                "bucket": "runtime-room",
                "credential_profile": "oauth-runtime",
            },
        },
    }
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps(persisted))
    runtime_path = tmp_path / "runtime-config.json"
    runtime_path.write_text(json.dumps(runtime))
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", str(runtime_path))

    assert cli.main([
        "dimensions", "update", "room-a", "--display-name", "Room A updated", "--json",
    ]) == 0
    capsys.readouterr()
    saved = json.loads((tmp_path / "config.json").read_text())
    assert "r2" not in saved.get("dimensions", {})
    assert "r2" not in saved
    assert saved["dimensions"]["room-a"]["display_name"] == "Room A updated"


def test_r2_connection_keeps_oauth_first_and_explicit_dimension_routing(tmp_path, monkeypatch):
    config = {
        "default_dimension": "archive",
        "connections": {"cloud-r2": {
            "display_name": "Cloudflare R2",
            "provider": "r2",
            "endpoint": "https://r2.example.invalid",
            "credential_profile": "oauth-runtime",
        }},
        "dimensions": {"archive": {
            "display_name": "Archive",
            "connection_id": "cloud-r2",
            "bucket": "archive",
            "catalog_key": "archive.jroom.age",
        }},
    }
    monkeypatch.setattr(cli, "private_config", lambda: config)
    args = cli.build_parser().parse_args(["projects", "list", "--dimension", "archive"])
    calls = []
    monkeypatch.setattr(cli, "_backend", lambda name, _instance, dimension=None: calls.append((name, dimension)) or object())

    assert cli._requires_oauth(args) is True
    cli._backend_for_args(args, tmp_path)
    assert calls == [("r2", "archive")]


def test_room_catalogs_are_independent_and_jat_history_is_immutable():
    digest = hashlib.sha256(b"jat").hexdigest()
    first = {
        "snapshot_id": "jat-1",
        "created_at": "2026-08-27T00:00:00+00:00",
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": 3,
        "workspace_fingerprint": "a" * 64,
    }
    second = {**first, "snapshot_id": "jat-2", "created_at": "2026-08-27T00:01:00+00:00"}
    archive = Catalog.empty("archive").add_snapshot("room", "Room", first)
    backup = Catalog.empty("backup").add_snapshot("room", "Room", second)

    assert archive.dimension_id == "archive"
    assert backup.dimension_id == "backup"
    assert list(archive.body["projects"]["room"]["snapshots"]) == ["jat-1"]
    assert archive.resolve_snapshot("room", "jat-1")["snapshot_id"] == "jat-1"
    assert backup.resolve_snapshot("room", "jat-2")["snapshot_id"] == "jat-2"


def test_native_provider_cli_boundary_connects_minio_buckets_dimension_and_routes_r2_explicitly(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    stored = []
    monkeypatch.setattr(cli, "store_keyring", lambda profile, values: stored.append((profile, values)))
    monkeypatch.setattr(cli, "list_minio_buckets", lambda connection: ["existing-room"])
    monkeypatch.setattr(cli, "create_minio_bucket", lambda connection, bucket: bucket)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    })))

    assert cli.main([
        "provider", "connection", "create", "--provider", "minio",
        "--endpoint", "http://minio.home.arpa:9000", "--json",
    ]) == 0
    connected = json.loads(capsys.readouterr().out)
    connection_id = connected["connection"]["id"]
    assert connection_id.startswith("minio-")

    assert cli.main(["provider", "bucket", "list", "--connection", connection_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["buckets"] == ["existing-room"]
    assert cli.main([
        "provider", "bucket", "create", "--connection", connection_id,
        "--bucket", "created-room", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["bucket"] == "created-room"
    assert stored[0][1]["secret-access-key"] == "synthetic-secret"

    assert cli.main([
        "dimensions", "add", "minio-room", "--display-name", "MinIO Room",
        "--connection", connection_id, "--bucket", "created-room", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["updated"] is True
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["dimensions"]["minio-room"] == {
        "display_name": "MinIO Room",
        "connection_id": connection_id,
        "bucket": "created-room",
    }

    saved["connections"]["cloud-r2"] = {
        "display_name": "Cloudflare R2",
        "provider": "r2",
        "endpoint": "https://r2.example.invalid",
        "credential_profile": "r2-profile",
    }
    saved["dimensions"]["archive"] = {
        "display_name": "Archive",
        "connection_id": "cloud-r2",
        "bucket": "archive",
    }
    (tmp_path / "config.json").write_text(json.dumps(saved))
    monkeypatch.setattr(cli, "private_config", lambda: saved)
    r2_args = cli.build_parser().parse_args(["projects", "list", "--dimension", "archive"])
    calls = []
    monkeypatch.setattr(cli, "_backend", lambda name, instance, dimension=None: calls.append((name, dimension)) or object())
    assert cli._requires_oauth(r2_args) is True
    cli._backend_for_args(r2_args, tmp_path)
    assert calls == [("r2", "archive")]


def test_dimension_hierarchy_reads_one_catalog_and_returns_rooms_with_jats(tmp_path, monkeypatch, capsys):
    config = {
        "connections": {"home-minio": _connection()},
        "dimensions": {"room-dimension": _dimension(connection_id="home-minio", bucket="room-bucket")},
    }
    catalog = Catalog.empty("room-dimension")
    digest = hashlib.sha256(b"jat").hexdigest()
    catalog = catalog.add_snapshot("room", "Room", {
        "snapshot_id": "jat-1",
        "created_at": "2026-08-27T00:00:00+00:00",
        "object_key": f"objects/sha256/{digest}",
        "ciphertext_sha256": digest,
        "ciphertext_size": 3,
        "workspace_fingerprint": "a" * 64,
    })
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps(config))
    calls = []
    monkeypatch.setattr(cli, "_backend", lambda name, instance, dimension=None: object())
    monkeypatch.setattr(cli, "load_catalog", lambda instance, backend=None: calls.append(backend) or catalog)

    assert cli.main([
        "dimensions", "list", "--dimension", "room-dimension", "--with-hierarchy", "--json",
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert result["dimensions"][0]["rooms"] == [{
        "id": "room",
        "display_name": "Room",
        "latest": "jat-1",
        "jats": [catalog.body["projects"]["room"]["snapshots"]["jat-1"]],
    }]


def test_canonical_provider_command_vectors_and_parser_contract_are_exact():
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "legacy compatibility alias for provider connection" in help_text
    assert "legacy compatibility alias for provider bucket" in help_text
    vectors = [
        (["provider", "connection", "create", "--provider", "minio", "--endpoint", "http://minio.example"],
         {"command": "provider", "provider_command": "connection", "provider_connection_command": "create", "provider": "minio", "endpoint": "http://minio.example"}),
        (["provider", "connection", "list"],
         {"command": "provider", "provider_command": "connection", "provider_connection_command": "list"}),
        (["provider", "connection", "reconnect", "--connection", "home", "--endpoint", "http://new.example"],
         {"command": "provider", "provider_command": "connection", "provider_connection_command": "reconnect", "connection": "home", "endpoint": "http://new.example"}),
        (["provider", "connection", "disconnect", "--connection", "home"],
         {"command": "provider", "provider_command": "connection", "provider_connection_command": "disconnect", "connection": "home"}),
        (["provider", "bucket", "list", "--connection", "home"],
         {"command": "provider", "provider_command": "bucket", "provider_bucket_command": "list", "connection": "home"}),
        (["provider", "bucket", "create", "--connection", "home", "--bucket", "room"],
         {"command": "provider", "provider_command": "bucket", "provider_bucket_command": "create", "connection": "home", "bucket": "room"}),
        (["provider", "bucket", "check", "--connection", "home", "--bucket", "room"],
         {"command": "provider", "provider_command": "bucket", "provider_bucket_command": "check", "connection": "home", "bucket": "room"}),
        (["auth", "wait", "session-one", "--dimension", "archive"],
         {"command": "auth", "auth_command": "wait", "session_id": "session-one", "dimension": "archive"}),
        (["dimensions", "list", "--dimension", "archive", "--with-hierarchy"],
         {"command": "dimensions", "dimension_command": "list", "dimension": "archive", "with_hierarchy": True}),
    ]
    for argv, expected in vectors:
        parsed = vars(parser.parse_args(argv))
        assert {key: parsed[key] for key in expected} == expected
