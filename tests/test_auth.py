import json
import os
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from synthetic_identity import synthetic_identity

from josh_room import auth, keyring
from josh_room.auth import (
    _request,
    ensure_runtime_session,
    logout_runtime_session,
    poll_oauth_session,
    runtime_session_state,
    start_oauth_session,
    wait_oauth_session,
)
from josh_room.config import DimensionRegistry

TEST_IDENTITY = synthetic_identity("synthetic")
NATIVE_IDENTITY = synthetic_identity("native")


def test_worker_request_identifies_josh_room_instead_of_python_urllib(monkeypatch):
    captured = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_request(request, timeout, **_kwargs):
        captured.append((request, timeout))
        return Response(b'{"status":"pending"}')

    monkeypatch.setenv("JOSH_ROOM_AUTH_URL", "https://auth.example.invalid")
    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    assert _request("/session/synthetic") == {"status": "pending"}
    assert captured[0][0].get_header("User-agent").startswith("Josh-Room/")


def test_worker_request_uses_explicit_auth_authority(monkeypatch):
    captured = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_request(request, timeout, **_kwargs):
        captured.append((request, timeout))
        return Response(b'{"status":"pending"}')

    monkeypatch.setenv("JOSH_ROOM_AUTH_URL", "https://auth.example.invalid")
    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    assert auth._request("/session/synthetic") == {"status": "pending"}
    assert captured[0][0].full_url == "https://auth.example.invalid/session/synthetic"


def test_worker_request_uses_official_authority_without_override(monkeypatch):
    captured = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_request(request, timeout, **_kwargs):
        captured.append((request, timeout))
        return Response(b'{"status":"pending"}')

    monkeypatch.delenv("JOSH_ROOM_AUTH_URL", raising=False)
    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    auth._request("/session/synthetic")

    assert captured[0][0].full_url == "https://josh-room-auth.joshua-yorko.workers.dev/session/synthetic"


def test_worker_request_passes_a_scoped_system_trust_context(monkeypatch):
    captured = {}

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_request(request, timeout, **kwargs):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["context"] = kwargs.get("context")
        return Response(b'{"status":"pending"}')

    sentinel = object()
    monkeypatch.setenv("JOSH_ROOM_AUTH_URL", "https://auth.example.invalid")
    monkeypatch.setattr(auth, "system_ssl_context", lambda: sentinel, raising=False)
    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    assert _request("/session/synthetic") == {"status": "pending"}
    assert captured["context"] is sentinel


def test_cancel_oauth_session_invalidates_worker_transaction(monkeypatch):
    captured = []

    def request(path, method="GET"):
        captured.append((path, method))
        return {"status": "canceled"}

    monkeypatch.setattr(auth, "_request", request)

    assert hasattr(auth, "cancel_oauth_session")
    assert auth.cancel_oauth_session("session-1") == {"status": "canceled"}
    assert captured == [("/session/session-1/cancel", "POST")]


def test_cancel_oauth_session_treats_vanished_session_as_idempotent_cleanup(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("r2.json", "age.identity", "config.json", "session.json"):
        (runtime / name).write_text("stale")

    def vanished(*_args, **_kwargs):
        raise HTTPError("https://auth.example.invalid/session", 404, "gone", {}, None)

    monkeypatch.setattr(auth, "_request", vanished)
    assert auth.cancel_oauth_session("vanished") == {"status": "canceled", "stale": True}
    assert not any(path.exists() for path in runtime.iterdir())


def test_cancel_oauth_session_preserves_non_404_authority_failures_and_cleans_local_state(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (runtime / "session.json").write_text("stale")

    def failed(*_args, **_kwargs):
        raise HTTPError("https://auth.example.invalid/session", 503, "unavailable", {}, None)

    monkeypatch.setattr(auth, "_request", failed)
    with pytest.raises(HTTPError):
        auth.cancel_oauth_session("failed")
    assert not any(path.exists() for path in runtime.iterdir())


def test_cli_exposes_auth_cancel_for_native_cancellation(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        auth,
        "cancel_oauth_session",
        lambda session_id: calls.append(session_id) or {"status": "canceled"},
        raising=False,
    )
    monkeypatch.setattr("josh_room.cli.cancel_oauth_session", auth.cancel_oauth_session, raising=False)

    from josh_room import cli

    assert cli.main(["auth", "cancel", "session-1", "--json"]) == 0
    assert calls == ["session-1"]
    assert json.loads(capsys.readouterr().out) == {"ok": True, "status": "canceled"}


def test_oauth_session_writes_private_runtime_material_and_environment(tmp_path, monkeypatch):
    responses = iter(
        [
            {"sessionId": "one", "authorizationUrl": "https://example.invalid/auth"},
            {"status": "pending"},
            {
                "status": "authorized",
                "accessKeyId": "temporary-access",
                "secretAccessKey": "temporary-secret",
                "sessionToken": "temporary-session",
                "ageIdentity": TEST_IDENTITY,
                "ageRecipients": ["age1daily", "age1recovery"],
                "endpoint": "https://example.invalid",
                "bucket": "synthetic-room",
                "expiresIn": 21600,
            },
        ]
    )
    opened = []
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("josh_room.auth.webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("josh_room.auth.time.sleep", lambda _seconds: None)

    ensure_runtime_session()

    assert opened == ["https://example.invalid/auth"]
    credentials = Path(os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"])
    identity = Path(os.environ["JOSH_ROOM_IDENTITY"])
    config = Path(os.environ["JOSH_ROOM_RUNTIME_CONFIG"])
    assert credentials.stat().st_mode & 0o777 == 0o600
    assert identity.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_mode & 0o777 == 0o600
    assert json.loads(credentials.read_text())["session-token"] == "temporary-session"
    assert json.loads(config.read_text())["r2"]["bucket"] == "synthetic-room"
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


def test_next_cli_process_reuses_unexpired_room_session(tmp_path, monkeypatch):
    responses = iter(
        [
            {"sessionId": "one", "authorizationUrl": "https://example.invalid/auth"},
            {
                "status": "authorized",
                "accessKeyId": "temporary-access",
                "secretAccessKey": "temporary-secret",
                "sessionToken": "temporary-session",
                "ageIdentity": TEST_IDENTITY,
                "ageRecipients": ["age1daily", "age1recovery"],
                "endpoint": "https://example.invalid",
                "bucket": "synthetic-room",
                "expiresIn": 21600,
            },
        ]
    )
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("josh_room.auth.webbrowser.open", lambda _url: True)

    ensure_runtime_session()
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)

    monkeypatch.setattr(
        "josh_room.auth._request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OAuth opened twice")),
    )

    ensure_runtime_session()

    assert Path(os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"]).is_file()
    assert Path(os.environ["JOSH_ROOM_RUNTIME_CONFIG"]).is_file()
    assert Path(os.environ["JOSH_ROOM_IDENTITY"]).is_file()
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


def test_extension_oauth_boundary_returns_url_then_persists_only_authorized_runtime(tmp_path, monkeypatch):
    responses = iter([
        {"sessionId": "native-session", "authorizationUrl": "https://example.invalid/native-auth", "expiresIn": 600},
        {
            "status": "authorized",
            "accessKeyId": "temporary-access",
            "secretAccessKey": "temporary-secret",
            "sessionToken": "temporary-session",
            "ageIdentity": NATIVE_IDENTITY,
            "ageRecipients": ["age1daily", "age1recovery"],
            "endpoint": "https://example.invalid",
            "bucket": "native-room",
            "expiresIn": 21600,
        },
    ])
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))

    started = start_oauth_session()
    result = poll_oauth_session(started["session_id"], dimension_id="archive")

    assert started == {
        "session_id": "native-session",
        "authorization_url": "https://example.invalid/native-auth",
        "expires_in": 600,
    }
    assert result == {"status": "authorized"}
    assert runtime_session_state() == "connected"
    assert "temporary-secret" not in json.dumps(result)
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


def test_runtime_session_state_distinguishes_missing_and_expired_authority(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert runtime_session_state() == "missing"
    runtime.mkdir(parents=True)
    for name in ("r2.json", "age.identity", "config.json"):
        (runtime / name).write_text("synthetic")
    assert runtime_session_state() == "missing"
    (runtime / "session.json").write_text(json.dumps({"expires_at": 0}))

    assert runtime_session_state() == "expired"


def test_load_runtime_session_reuses_age_material_without_contacting_authority(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    (runtime / "age.identity").write_text(TEST_IDENTITY + "\n")
    (runtime / "age.identity").chmod(0o600)
    (runtime / "config.json").write_text(json.dumps({"age_recipients": ["age1daily", "age1recovery"]}))
    (runtime / "session.json").write_text(json.dumps({"expires_at": time.time() + 600}))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        monkeypatch.delenv(name, raising=False)

    assert hasattr(auth, "load_runtime_session")
    assert auth.load_runtime_session() is True
    assert os.environ["JOSH_ROOM_RUNTIME_CONFIG"] == str(runtime / "config.json")
    assert os.environ["JOSH_ROOM_IDENTITY"] == str(runtime / "age.identity")


def test_non_r2_snapshot_loads_existing_runtime_session_before_dispatch(monkeypatch, capsys):
    from josh_room import cli

    calls = []
    monkeypatch.setattr(cli, "_requires_oauth", lambda _args: False)
    monkeypatch.setattr(cli, "load_runtime_session", lambda: calls.append(True) or True, raising=False)
    monkeypatch.setattr(cli, "dispatch", lambda *_args: {"ok": True})

    assert cli.main(["snapshot", "create", "synthetic", "--backend", "minio", "--json"]) == 0
    capsys.readouterr()
    assert calls == [True]


def test_logout_runtime_session_clears_only_local_r2_session_material(tmp_path, monkeypatch):
    root = tmp_path / "runtime" / "josh-room" / "session"
    root.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("r2.json", "age.identity", "config.json", "session.json"):
        (root / name).write_text("local-session-material")
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(root / "r2.json"))
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", str(root / "config.json"))
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(root / "age.identity"))
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_PROFILE", "oauth-runtime")

    assert logout_runtime_session() == {"status": "logged_out"}
    assert not any((root / name).exists() for name in ("r2.json", "age.identity", "config.json", "session.json"))
    assert runtime_session_state() == "missing"
    assert all(os.environ.get(name) is None for name in (
        "JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG",
        "JOSH_ROOM_IDENTITY", "JOSH_ROOM_RUNTIME_PROFILE",
    ))


def test_expired_runtime_session_is_removed_before_a_new_login(tmp_path, monkeypatch):
    root = tmp_path / "runtime" / "josh-room" / "session"
    root.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name in ("r2.json", "age.identity", "config.json"):
        (root / name).write_text("stale-secret-material")
    (root / "session.json").write_text(json.dumps({"expires_at": 0}))

    assert runtime_session_state() == "expired"
    assert not any((root / name).exists() for name in ("r2.json", "age.identity", "config.json", "session.json"))


def test_canceled_oauth_removes_stale_runtime_material_so_next_login_is_unmasked(tmp_path, monkeypatch):
    root = tmp_path / "runtime" / "josh-room" / "session"
    root.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("josh_room.auth.webbrowser.open", lambda _url: True)
    for name in ("r2.json", "age.identity", "config.json"):
        (root / name).write_text("stale-secret-material")
    (root / "session.json").write_text(json.dumps({"expires_at": 0}))
    responses = iter([
        {"sessionId": "new-session", "authorizationUrl": "https://example.invalid/auth"},
        {"status": "canceled"},
    ])
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))

    with pytest.raises(RuntimeError, match="canceled"):
        ensure_runtime_session(timeout=1)

    assert runtime_session_state() == "missing"
    assert not any((root / name).exists() for name in ("r2.json", "age.identity", "config.json", "session.json"))


def test_oauth_runtime_overlay_updates_referenced_connection_without_corrupting_dimension(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "connections": {"cloud-r2": {
            "display_name": "Cloudflare R2",
            "provider": "r2",
            "endpoint": "https://old.example.invalid",
            "credential_profile": "old-profile",
            "auth_state": "disconnected",
        }},
        "dimensions": {"archive": {
            "display_name": "Archive",
            "connection_id": "cloud-r2",
            "bucket": "archive",
            "catalog_key": "archive.jroom.age",
        }},
    }))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    from josh_room.auth import _write_runtime

    _write_runtime({
        "accessKeyId": "temporary-access",
        "secretAccessKey": "temporary-secret",
        "sessionToken": "temporary-session",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "endpoint": "https://new.example.invalid",
        "bucket": "oauth-archive",
        "expiresIn": 600,
    }, dimension_id="archive")

    runtime_config = json.loads((tmp_path / "runtime" / "josh-room" / "session" / "config.json").read_text())
    assert runtime_config["connections"]["cloud-r2"]["auth_state"] == "configured"
    assert runtime_config["dimensions"]["archive"] == {
        "display_name": "Archive",
        "connection_id": "cloud-r2",
        "bucket": "archive",
        "catalog_key": "archive.jroom.age",
    }
    assert DimensionRegistry(runtime_config).select("archive").endpoint == "https://new.example.invalid"
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


def test_encryption_only_runtime_keeps_minio_config_and_discards_r2_material(tmp_path, monkeypatch, request):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    persisted = {
        "default_backend": "minio",
        "dimensions": {
            "backup": {
                "display_name": "Backup",
                "provider": "minio",
                "endpoint": "https://minio.example.invalid",
                "bucket": "backup",
                "credential_profile": "minio-profile",
            },
        },
    }
    (config_dir / "config.json").write_text(json.dumps(persisted))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    request.addfinalizer(auth._clear_runtime_session)

    auth._write_runtime({
        "purpose": "encryption",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "accessKeyId": "must-not-persist",
        "secretAccessKey": "must-not-persist",
        "sessionToken": "must-not-persist",
        "endpoint": "https://r2.example.invalid",
        "bucket": "r2-bucket",
        "expiresIn": 600,
    }, dimension_id="backup")

    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime_config = json.loads((runtime / "config.json").read_text())
    assert runtime_config == {**persisted, "age_recipients": ["age1daily", "age1recovery"]}
    assert not (runtime / "r2.json").exists()
    assert auth.runtime_session_state() == "connected"
    assert auth.r2_session_state() == "missing"
    assert auth.runtime_capabilities() == ("encryption",)
    assert os.environ.get("JOSH_ROOM_RUNTIME_CREDENTIALS") is None
    assert os.environ.get("JOSH_ROOM_RUNTIME_PROFILE") is None


def test_encryption_only_runtime_preserves_extension_minio_credential_broker(tmp_path, monkeypatch, request):
    broker = tmp_path / "extension-credentials.json"
    broker.write_text(json.dumps({
        "profiles": {
            "minio-profile": {
                "access-key-id": "synthetic-access",
                "secret-access-key": "synthetic-secret",
            },
        },
    }))
    broker.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(broker))
    monkeypatch.delenv("JOSH_ROOM_RUNTIME_PROFILE", raising=False)
    request.addfinalizer(auth._clear_runtime_session)

    auth._write_runtime({
        "purpose": "encryption",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "expiresIn": 600,
    }, dimension_id="backup")

    assert os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] == str(broker)
    assert keyring.lookup("minio-profile", allow_runtime=False) == {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    }
    assert auth.load_runtime_session() is True
    assert os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] == str(broker)
    auth._clear_runtime_session()
    assert os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] == str(broker)
    assert keyring.lookup("minio-profile", allow_runtime=False) == {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    }


def test_r2_runtime_keeps_minio_broker_separate_from_oauth_credentials(tmp_path, monkeypatch, request):
    broker = tmp_path / "extension-credentials.json"
    broker.write_text(json.dumps({
        "profiles": {
            "minio-profile": {
                "access-key-id": "synthetic-minio-access",
                "secret-access-key": "synthetic-minio-secret",
            },
        },
    }))
    broker.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_PROVIDER_CREDENTIALS", str(broker))
    monkeypatch.delenv("JOSH_ROOM_RUNTIME_CREDENTIALS", raising=False)
    monkeypatch.delenv("JOSH_ROOM_RUNTIME_PROFILE", raising=False)
    request.addfinalizer(auth._clear_runtime_session)

    auth._write_runtime({
        "purpose": "r2",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "accessKeyId": "synthetic-r2-access",
        "secretAccessKey": "synthetic-r2-secret",
        "sessionToken": "synthetic-r2-session",
        "endpoint": "https://r2.example.invalid",
        "bucket": "r2-bucket",
        "expiresIn": 600,
    })

    assert os.environ["JOSH_ROOM_PROVIDER_CREDENTIALS"] == str(broker)
    assert keyring.lookup("minio-profile", allow_runtime=False) == {
        "access-key-id": "synthetic-minio-access",
        "secret-access-key": "synthetic-minio-secret",
    }
    assert keyring.lookup("oauth-runtime", allow_runtime=True) == {
        "access-key-id": "synthetic-r2-access",
        "secret-access-key": "synthetic-r2-secret",
        "session-token": "synthetic-r2-session",
    }


def test_r2_logout_downgrades_to_encryption_and_preserves_minio(tmp_path, monkeypatch, request):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    persisted = {
        "connections": {
            "minio": {
                "display_name": "MinIO",
                "provider": "minio",
                "endpoint": "https://minio.example.invalid",
                "credential_profile": "minio-profile",
            },
        },
        "dimensions": {
            "backup": {
                "display_name": "Backup",
                "connection_id": "minio",
                "bucket": "backup",
            },
        },
    }
    (config_dir / "config.json").write_text(json.dumps(persisted))
    broker = tmp_path / "extension-credentials.json"
    broker.write_text(json.dumps({
        "profiles": {
            "minio-profile": {
                "access-key-id": "synthetic-minio-access",
                "secret-access-key": "synthetic-minio-secret",
            },
        },
    }))
    broker.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_PROVIDER_CREDENTIALS", str(broker))
    request.addfinalizer(auth._clear_runtime_session)

    auth._write_runtime({
        "purpose": "r2",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "accessKeyId": "synthetic-r2-access",
        "secretAccessKey": "synthetic-r2-secret",
        "sessionToken": "synthetic-r2-session",
        "endpoint": "https://r2.example.invalid",
        "bucket": "r2-bucket",
        "expiresIn": 600,
    })

    assert auth.logout_runtime_session(purpose="r2") == {
        "status": "logged_out",
        "encryption_preserved": True,
    }
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    assert not (runtime / "r2.json").exists()
    assert (runtime / "age.identity").is_file()
    assert auth.runtime_session_state() == "connected"
    assert auth.runtime_capabilities() == ("encryption",)
    assert auth.r2_session_state() == "missing"
    runtime_config = json.loads((runtime / "config.json").read_text())
    assert runtime_config == {**persisted, "age_recipients": ["age1daily", "age1recovery"]}
    assert keyring.lookup("minio-profile", allow_runtime=False)["access-key-id"] == "synthetic-minio-access"


def test_r2_logout_write_failure_recovers_encryption_session(tmp_path, monkeypatch, request):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    request.addfinalizer(auth._clear_runtime_session)
    auth._write_runtime({
        "purpose": "r2",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "accessKeyId": "synthetic-r2-access",
        "secretAccessKey": "synthetic-r2-secret",
        "sessionToken": "synthetic-r2-session",
        "endpoint": "https://r2.example.invalid",
        "bucket": "r2-bucket",
        "expiresIn": 600,
    })
    original_replace = auth.os.replace
    failed = False

    def fail_metadata_replace(source, destination):
        nonlocal failed
        if not failed and Path(destination).name == "session.json":
            failed = True
            raise OSError("synthetic metadata replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(auth.os, "replace", fail_metadata_replace)
    with pytest.raises(OSError, match="synthetic metadata replace failure"):
        auth.logout_runtime_session(purpose="r2")
    monkeypatch.setattr(auth.os, "replace", original_replace)

    assert auth.runtime_session_state() == "connected"
    assert auth.runtime_capabilities() == ("encryption",)
    assert auth.r2_session_state() == "missing"


def test_r2_logout_interruption_after_credential_removal_recovers(tmp_path, monkeypatch, request):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    request.addfinalizer(auth._clear_runtime_session)
    auth._write_runtime({
        "purpose": "r2",
        "ageIdentity": TEST_IDENTITY,
        "ageRecipients": ["age1daily", "age1recovery"],
        "accessKeyId": "synthetic-r2-access",
        "secretAccessKey": "synthetic-r2-secret",
        "sessionToken": "synthetic-r2-session",
        "endpoint": "https://r2.example.invalid",
        "bucket": "r2-bucket",
        "expiresIn": 600,
    })
    original_unlink = Path.unlink

    def fail_marker_unlink(path, *args, **kwargs):
        if path.name == "r2-logout.json":
            raise OSError("synthetic interruption")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_marker_unlink)
    with pytest.raises(OSError, match="synthetic interruption"):
        auth.logout_runtime_session(purpose="r2")
    monkeypatch.setattr(Path, "unlink", original_unlink)

    assert auth.runtime_session_state() == "connected"
    assert auth.runtime_capabilities() == ("encryption",)
    assert auth.r2_session_state() == "missing"


def test_permissive_runtime_identity_is_cleared(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    identity = runtime / "age.identity"
    identity.write_text(TEST_IDENTITY + "\n")
    identity.chmod(0o644)
    (runtime / "config.json").write_text(json.dumps({"age_recipients": ["age1daily", "age1recovery"]}))
    (runtime / "session.json").write_text(json.dumps({"expires_at": time.time() + 600, "capabilities": ["encryption"]}))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert auth.runtime_session_state() == "missing"
    assert not any(runtime.iterdir())


@pytest.mark.parametrize("config_body", [None, [], "malformed"])
def test_malformed_runtime_config_is_cleared_fail_closed(tmp_path, monkeypatch, config_body):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    (runtime / "age.identity").write_text(TEST_IDENTITY + "\n")
    (runtime / "age.identity").chmod(0o600)
    (runtime / "config.json").write_text(json.dumps(config_body))
    (runtime / "session.json").write_text(json.dumps({"expires_at": time.time() + 600, "capabilities": ["encryption"]}))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert auth.runtime_session_state() == "missing"
    assert not runtime.exists() or not any(runtime.iterdir())


def test_malformed_runtime_identity_is_cleared_fail_closed(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime" / "josh-room" / "session"
    runtime.mkdir(parents=True)
    (runtime / "age.identity").write_text("not-an-age-identity\n")
    (runtime / "age.identity").chmod(0o600)
    (runtime / "config.json").write_text(json.dumps({"age_recipients": ["age1daily", "age1recovery"]}))
    (runtime / "session.json").write_text(json.dumps({"expires_at": time.time() + 600, "capabilities": ["encryption"]}))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    assert auth.runtime_session_state() == "missing"
    assert not runtime.exists() or not any(runtime.iterdir())


def test_wait_oauth_session_polls_until_authorized_in_one_long_lived_operation(monkeypatch):
    responses = iter([{"status": "pending"}, {"status": "authorized"}])
    writes = []
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("josh_room.auth._write_runtime", lambda session, dimension_id=None: writes.append((session, dimension_id)))
    monkeypatch.setattr("josh_room.auth.time.sleep", lambda _seconds: None)

    assert wait_oauth_session("session-one", timeout=10, poll_interval=0, dimension_id="archive") == {
        "status": "authorized"
    }
    assert writes == [({"status": "authorized"}, "archive")]


def test_wait_oauth_session_reports_browser_wait_and_validation_elapsed_without_extra_processes(monkeypatch):
    responses = iter([{"status": "pending"}, {"status": "authorized"}])
    events = []
    clock = [100.0]
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("josh_room.auth._write_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("josh_room.auth.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("josh_room.auth.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr("josh_room.auth.report_progress", lambda stage, message: events.append((stage, message)))

    assert wait_oauth_session("session-one", timeout=10, poll_interval=2, dimension_id="archive") == {"status": "authorized"}
    assert events == [
        ("auth", "Waiting for browser approval (0s elapsed)"),
        ("auth", "Validating Cloudflare session (2s elapsed)"),
    ]


def test_wait_oauth_session_reports_encryption_authorization_without_cloudflare_wording(monkeypatch):
    responses = iter([{"status": "pending"}, {"status": "authorized"}])
    events = []
    clock = [100.0]
    monkeypatch.setattr("josh_room.auth._request", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("josh_room.auth._write_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("josh_room.auth.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("josh_room.auth.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr("josh_room.auth.report_progress", lambda stage, message: events.append((stage, message)))

    assert wait_oauth_session(
        "session-one", timeout=10, poll_interval=2, dimension_id="backup", purpose="encryption"
    ) == {"status": "authorized"}
    assert events == [
        ("auth", "Waiting for browser approval (0s elapsed)"),
        ("auth", "Validating Josh Room encryption authorization (2s elapsed)"),
    ]


def test_extension_runtime_credentials_support_profile_scoped_secretstorage_handoff(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({
        "profiles": {
            "minio-home": {
                "access-key-id": "synthetic-access",
                "secret-access-key": "synthetic-secret",
            },
        },
    }))
    credentials.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(credentials))

    assert keyring.lookup("minio-home", allow_runtime=False) == {
        "access-key-id": "synthetic-access",
        "secret-access-key": "synthetic-secret",
    }
    with pytest.raises(TypeError, match="profile is unavailable"):
        keyring.lookup("other-profile", allow_runtime=False)
