import json
import os
import time
import urllib.request
import webbrowser
from pathlib import Path

from .progress import report_progress

WORKER_URL = "https://josh-room-auth.joshua-yorko.workers.dev"


def ensure_runtime_session(timeout: int = 600) -> None:
    if all(os.environ.get(name) and Path(os.environ[name]).is_file() for name in (
        "JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"
    )):
        report_progress("auth", "Cloudflare session is ready")
        return
    if _load_runtime():
        report_progress("auth", "Reusing this Room's Cloudflare session")
        return
    report_progress("auth", "Opening Cloudflare sign-in")
    started = _request("/session/start", method="POST")
    webbrowser.open(started["authorizationUrl"])
    report_progress("auth", "Waiting for Cloudflare approval in your browser")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = _request(f"/session/{started['sessionId']}")
        status = session.get("status")
        if status == "pending":
            time.sleep(2)
            continue
        if status != "authorized":
            raise RuntimeError(f"Cloudflare authorization {status or 'failed'}")
        _write_runtime(session)
        report_progress("auth", "Cloudflare session authorized")
        return
    raise RuntimeError("Cloudflare authorization timed out")


def _request(path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(
        WORKER_URL + path,
        method=method,
        headers={"User-Agent": "Josh-Room/0.1 (+https://github.com/joshyorko/josh-room)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _write_runtime(session: dict) -> None:
    root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    credentials = root / "r2.json"
    identity = root / "age.identity"
    config = root / "config.json"
    metadata = root / "session.json"
    credentials.write_text(json.dumps({
        "access-key-id": session["accessKeyId"],
        "secret-access-key": session["secretAccessKey"],
        "session-token": session["sessionToken"],
    }))
    identity.write_text(session["ageIdentity"].rstrip("\n") + "\n")
    config.write_text(json.dumps({
        "default_backend": "r2",
        "default_ide": "vscode-insiders",
        "age_recipients": session["ageRecipients"],
        "r2": {
            "endpoint": session["endpoint"],
            "bucket": session["bucket"],
            "region": "auto",
            "credential_profile": "oauth-runtime",
            "catalog_key": "catalog.jroom.age",
            "temporary_credentials": True,
        },
    }))
    metadata.write_text(json.dumps({"expires_at": time.time() + int(session["expiresIn"])}))
    for path in (credentials, identity, config, metadata):
        path.chmod(0o600)
    os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] = str(credentials)
    os.environ["JOSH_ROOM_IDENTITY"] = str(identity)
    os.environ["JOSH_ROOM_RUNTIME_CONFIG"] = str(config)


def _load_runtime() -> bool:
    root = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "josh-room" / "session"
    credentials = root / "r2.json"
    identity = root / "age.identity"
    config = root / "config.json"
    metadata = root / "session.json"
    if not all(path.is_file() for path in (credentials, identity, config, metadata)):
        return False
    try:
        expires_at = float(json.loads(metadata.read_text())["expires_at"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if expires_at <= time.time() + 60:
        return False
    os.environ["JOSH_ROOM_RUNTIME_CREDENTIALS"] = str(credentials)
    os.environ["JOSH_ROOM_IDENTITY"] = str(identity)
    os.environ["JOSH_ROOM_RUNTIME_CONFIG"] = str(config)
    return True
