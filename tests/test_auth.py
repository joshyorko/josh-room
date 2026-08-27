import json
import os
from io import BytesIO
from pathlib import Path

import pytest

import josh_room.auth as auth
from josh_room.auth import (
    _request,
    ensure_runtime_session,
    poll_oauth_session,
    runtime_session_state,
    start_oauth_session,
    wait_oauth_session,
)
from josh_room.config import DimensionRegistry


def test_worker_request_identifies_josh_room_instead_of_python_urllib(monkeypatch):
    captured = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def open_request(request, timeout):
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

    def open_request(request, timeout):
        captured.append((request, timeout))
        return Response(b'{"status":"pending"}')

    monkeypatch.setenv("JOSH_ROOM_AUTH_URL", "https://auth.example.invalid")
    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    assert auth._request("/session/synthetic") == {"status": "pending"}
    assert captured[0][0].full_url == "https://auth.example.invalid/session/synthetic"


def test_cancel_oauth_session_invalidates_worker_transaction(monkeypatch):
    captured = []

    def request(path, method="GET"):
        captured.append((path, method))
        return {"status": "canceled"}

    monkeypatch.setattr(auth, "_request", request)

    assert hasattr(auth, "cancel_oauth_session")
    assert auth.cancel_oauth_session("session-1") == {"status": "canceled"}
    assert captured == [("/session/session-1/cancel", "POST")]


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
                "ageIdentity": "AGE-SECRET-KEY-synthetic",
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
                "ageIdentity": "AGE-SECRET-KEY-synthetic",
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
            "ageIdentity": "AGE-SECRET-KEY-native",
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
        "ageIdentity": "AGE-SECRET-KEY-synthetic",
        "ageRecipients": ["age1daily", "age1recovery"],
        "endpoint": "https://new.example.invalid",
        "bucket": "oauth-archive",
        "expiresIn": 600,
    }, dimension_id="archive")

    runtime_config = json.loads((tmp_path / "runtime" / "josh-room" / "session" / "config.json").read_text())
    assert runtime_config["dimensions"]["archive"] == {
        "display_name": "Archive",
        "connection_id": "cloud-r2",
        "bucket": "archive",
        "catalog_key": "archive.jroom.age",
    }
    assert DimensionRegistry(runtime_config).select("archive").endpoint == "https://new.example.invalid"
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


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
