import json
import os
from io import BytesIO
from pathlib import Path

from josh_room.auth import _request, ensure_runtime_session


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

    monkeypatch.setattr("josh_room.auth.urllib.request.urlopen", open_request)

    assert _request("/session/synthetic") == {"status": "pending"}
    assert captured[0][0].get_header("User-agent").startswith("Josh-Room/")


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
