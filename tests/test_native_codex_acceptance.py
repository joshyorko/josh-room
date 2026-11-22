"""Actual pinned Codex persisted-history discovery, never a model or login call."""

from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import native_agents as agents

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native profile contract")]
SESSION_ID = "11111111-1111-4111-8111-111111111111"
TIMESTAMP = "2026-11-22T15:00:00Z"


class NativeProtocol:
    def __init__(self, process):
        self.process = process
        self.buffer = b""

    def request(self, number, method, params):
        self.process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        total = 0
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == number:
                    assert "error" not in message, message.get("error")
                    return message["result"]
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([self.process.stdout], [], [], remaining)[0], method
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, "isolated native app-server exited"
            total += len(chunk)
            assert total <= 1024 * 1024, "native protocol result exceeded fixture limit"
            self.buffer += chunk


def test_native_codex_discovers_and_selects_cold_materialized_history_without_auth(tmp_path, monkeypatch):
    executable = shutil.which("codex")
    if not executable or not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("installed Codex, age and age-keygen required")
    tmp_path.chmod(0o700)
    home = tmp_path / "empty-home"
    home.mkdir(mode=0o700)
    probe_home = tmp_path / "version-home"
    probe_home.mkdir(mode=0o700)
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(probe_home), "XDG_CONFIG_HOME": str(home / "config"), "TERM": "dumb", "RUST_LOG": "error"}
    version = subprocess.run([executable, "--version"], env=environment, capture_output=True, text=True, check=True, timeout=10)
    if version.stdout.strip() != f"codex-cli {agents.CODEX_VERSION}":
        pytest.skip(f"native parser acceptance pins Codex {agents.CODEX_VERSION}")
    keys, recipients = [], []
    for index in range(2):
        key = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
        key.chmod(0o600)
        recipients.append(subprocess.run(["age-keygen", "-y", str(key)], capture_output=True, text=True, check=True).stdout.strip())
        keys.append(key)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    binding = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": workspace}
    monkeypatch.setattr(agents, "_admit", lambda *args, **kwargs: {"admitted": True, "authority_sha256": "synthetic-authority"})
    source, cold = tmp_path / "source", tmp_path / "cold"
    agents.configure(source, recipients=recipients, **binding)
    relative = Path(f"sessions/2026/11/22/rollout-2026-11-22T15-00-00-{SESSION_ID}.jsonl")
    original = source / "codex" / relative
    original.parent.mkdir(mode=0o700, parents=True)
    for parent in original.parents:
        if parent.is_relative_to(source):
            parent.chmod(0o700)
    rows = [
        {"timestamp": TIMESTAMP, "type": "session_meta", "payload": {"id": SESSION_ID, "timestamp": TIMESTAMP, "cwd": str(workspace), "originator": "codex_cli_rs", "cli_version": agents.CODEX_VERSION, "source": "cli", "model_provider": "offline"}},
        {"timestamp": TIMESTAMP, "type": "event_msg", "payload": {"type": "user_message", "message": "synthetic discovery question", "images": None, "local_images": []}},
        {"timestamp": TIMESTAMP, "type": "event_msg", "payload": {"type": "agent_message", "message": "synthetic retained answer"}},
    ]
    for ordinal, row in enumerate(rows):
        row["ordinal"] = ordinal
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    original.write_bytes(payload)
    original.chmod(0o600)
    reference = tmp_path / "retained.age"
    agents.capture(source, output=reference, recipients=recipients, **binding)
    agents.configure(cold, recipients=recipients, **binding)
    result = agents.materialize_session(cold, source=reference, identity_paths=keys[:1], session_id=SESSION_ID, **binding)
    assert result["native_resume_verified"] is False
    native_home = cold / "codex"
    environment["CODEX_HOME"] = str(native_home)
    with socket.socket() as guard, (tmp_path / "native.stderr").open("wb") as stderr:
        guard.bind(("127.0.0.1", 0))
        guard.listen()
        guard.setblocking(False)
        port = guard.getsockname()[1]
        (native_home / "config.toml").write_text(f'cli_auth_credentials_store = "file"\nmodel = "synthetic-offline-selection"\nmodel_provider = "offline"\n[model_providers.offline]\nname = "Synthetic offline selection"\nbase_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n')
        process = subprocess.Popen([executable, "app-server", "--stdio", "--strict-config"], cwd=workspace, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0)
        try:
            protocol = NativeProtocol(process)
            initialized = protocol.request(0, "initialize", {"clientInfo": {"name": "josh_room_synthetic_acceptance", "version": "0.1"}, "capabilities": {"experimentalApi": True}})
            assert initialized["codexHome"] == str(native_home)
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            listing = protocol.request(1, "thread/list", {"limit": 20, "cwd": str(workspace), "currentAccountOnly": False, "useStateDbOnly": False})
            assert [thread["id"] for thread in listing["data"]] == [SESSION_ID]
            selected = protocol.request(2, "thread/read", {"threadId": SESSION_ID, "includeTurns": True})["thread"]
            assert selected["id"] == SESSION_ID and selected["sessionId"] == SESSION_ID
            assert selected["cwd"] == str(workspace) and selected["path"] == str(native_home / relative)
            assert selected["status"]["type"] == "notLoaded"
            items = selected["turns"][0]["items"]
            assert items[0]["content"][0]["text"] == "synthetic discovery question"
            assert items[1]["text"] == "synthetic retained answer"
            wrong_cwd = protocol.request(3, "thread/list", {"limit": 20, "cwd": str(tmp_path / "other-room"), "currentAccountOnly": False, "useStateDbOnly": False})
            assert wrong_cwd["data"] == []
            assert (native_home / relative).read_bytes() == payload
            assert not (native_home / "auth.json").exists()
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
        with pytest.raises(BlockingIOError):
            connection, _address = guard.accept()
            connection.close()
