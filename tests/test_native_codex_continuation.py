"""Opt-in original-ID Codex continuation against a deterministic loopback backend."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from josh_room import native_agents as agents

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux")
        or os.environ.get("JOSH_ROOM_TEST_CODEX_CONTINUATION") != "1",
        reason="explicit isolated synthetic Codex continuation opt-in required",
    ),
]
CODEX_SHA256 = "b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0"
SESSION = "11111111-1111-4111-8111-111111111111"
STAMP = "2026-11-22T15:00:00Z"
OLD_USER = "synthetic retained original question"
OLD_ASSISTANT = "synthetic retained original answer"
PROMPT = "synthetic cold followup"
ANSWER = "SYNTHETIC_CODEX_RESUMED"


def _loopback_namespace():
    outer = os.environ.get("JOSH_ROOM_TEST_NATIVE_OUTER_NETNS")
    assert outer and outer.startswith("net:["), "launcher must record its outer network namespace"
    assert os.readlink("/proc/self/ns/net") != outer, "fresh network namespace required"
    assert {name for _, name in socket.if_nameindex()} == {"lo"}, "external network interface present"
    assert not Path("/proc/net/route").read_text().splitlines()[1:], "IPv4 route table must be empty"
    for row in Path("/proc/net/ipv6_route").read_text().splitlines():
        fields = row.split()
        assert fields[-1] == "lo", "external IPv6 route present"
        if fields[0] == "0" * 32 and fields[1] == "00":
            assert int(fields[8], 16) & 0x200, "usable IPv6 default route present"


def _cold_session(tmp_path, monkeypatch):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age required")
    tmp_path.chmod(0o700)
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
    relative = Path(f"sessions/2026/11/22/rollout-2026-11-22T15-00-00-{SESSION}.jsonl")
    original = source / "codex" / relative
    original.parent.mkdir(mode=0o700, parents=True)
    for parent in original.parents:
        if parent.is_relative_to(source):
            parent.chmod(0o700)
    rows = [
        {"type": "session_meta", "payload": {"id": SESSION, "timestamp": STAMP, "cwd": str(workspace),
            "originator": "codex_cli_rs", "cli_version": agents.CODEX_VERSION, "source": "cli", "model_provider": "offline"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": OLD_USER, "images": None, "local_images": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": OLD_USER}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": OLD_ASSISTANT}]}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": OLD_ASSISTANT}},
    ]
    payload = "".join(json.dumps({"timestamp": STAMP, **row}) + "\n" for row in rows).encode()
    original.write_bytes(payload)
    original.chmod(0o600)
    archive = tmp_path / "reference.age"
    agents.capture(source, output=archive, recipients=recipients, **binding)
    shutil.rmtree(source)
    agents.configure(cold, recipients=recipients, **binding)
    result = agents.materialize_session(cold, source=archive, identity_paths=keys[:1], session_id=SESSION, **binding)
    assert result["native_resume_verified"] is False
    native_home = cold / "codex"
    assert (native_home / relative).read_bytes() == payload
    assert not (native_home / "auth.json").exists()
    return workspace, native_home, relative, payload


def _configure(native_home, port, tmp_path):
    # Supported model_catalog_json metadata removes tools independently of the
    # model response. No production-model entry or instructions are copied.
    model = {"slug": "synthetic-local", "display_name": "Synthetic local", "supported_reasoning_levels": [],
             "shell_type": "disabled", "visibility": "list", "supported_in_api": True, "priority": 99,
             "support_verbosity": False, "truncation_policy": {"mode": "bytes", "limit": 10000},
             "experimental_supported_tools": [], "include_apps_usage_instructions": False,
             "model_messages": {"instructions_template": "Synthetic local text-only fixture."},
             "context_window": 65536, "apply_patch_tool_type": None, "node_repl_disabled": True}
    model_path = tmp_path / "models.json"
    model_path.write_text(json.dumps({"models": [model]}))
    features = ("shell_tool", "unified_exec", "apply_patch_freeform", "view_image", "apps", "connectors", "plugins",
                "plugin_hooks", "codex_hooks", "hooks", "multi_agent", "multi_agent_v2", "collab", "code_mode",
                "js_repl", "web_search", "search_tool", "tool_search", "memories", "memory_tool", "shell_snapshot",
                "daemon_auto_start", "remote_models", "responses_websockets", "responses_websockets_v2",
                "enable_request_compression", "request_permissions_tool", "current_time_reminder", "sleep_tool",
                "token_budget", "goals", "send_message_to_user_async", "deferred_executor", "tool_suggest",
                "image_generation", "computer_use", "browser_use", "remote_control", "plugin_discovery_prefetch")
    config = f'''cli_auth_credentials_store = "file"
model = "synthetic-local"
model_provider = "offline"
model_catalog_json = {json.dumps(str(model_path))}
web_search = "disabled"
project_doc_max_bytes = 0
approval_policy = "never"
sandbox_mode = "read-only"
[model_providers.offline]
name = "Task-private deterministic fixture"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
requires_openai_auth = false
env_key = "JOSH_ROOM_SYNTHETIC_CODEX_KEY"
request_max_retries = 0
stream_max_retries = 0
supports_websockets = false
[features]
''' + "".join(f"{name} = false\n" for name in features) + '''skip_host_skill_discovery = true
[tools.update_plan]
enabled = false
[tools.experimental_request_user_input]
enabled = false
[plugins._default]
enabled = false
[agents]
enabled = false
[analytics]
enabled = false
[otel]
exporter = "none"
metrics_exporter = "none"
'''
    (native_home / "config.toml").write_text(config)


class Protocol:
    def __init__(self, process):
        self.process = process
        self.buffer = b""
        self.messages = []
        self.total = 0

    def read(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([self.process.stdout], [], [], remaining)[0], "native protocol deadline"
            chunk = os.read(self.process.stdout.fileno(), 65536)
            assert chunk, "isolated native app-server exited"
            self.total += len(chunk)
            assert self.total <= 4 * 1024 * 1024, "bounded synthetic protocol output exceeded"
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        self.messages.append(message)
        assert not ("id" in message and "method" in message), "unexpected native tool/approval request"
        return message

    def request(self, number, method, params):
        self.process.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            message = self.read(deadline)
            if message.get("id") == number:
                assert "error" not in message, message.get("error")
                return message["result"]


def test_native_codex_resumes_cold_original_id_for_one_loopback_turn(tmp_path, monkeypatch):
    _loopback_namespace()
    executable = os.environ.get("JOSH_ROOM_TEST_CODEX_CLI") or shutil.which("codex")
    if not executable:
        pytest.skip("installed pinned native Codex required")
    executable = str(Path(executable).resolve())
    assert hashlib.sha256(Path(executable).read_bytes()).hexdigest() == CODEX_SHA256
    assert not Path("/etc/codex").exists(), "host managed settings are outside this acceptance"
    workspace, native_home, relative, payload = _cold_session(tmp_path, monkeypatch)
    home = tmp_path / "empty-home"
    home.mkdir(mode=0o700)
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CODEX_HOME": str(native_home),
                   "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
                   "XDG_STATE_HOME": str(home / "state"), "TERM": "dumb", "RUST_LOG": "error"}
    version = subprocess.run([executable, "--version"], env=environment, capture_output=True, text=True, check=True, timeout=10)
    assert version.stdout.strip() == f"codex-cli {agents.CODEX_VERSION}"
    credential = secrets.token_hex(24)
    requests, faults = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024 or requests:
                faults.append("request-limit")
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(length))
            requests.append(body)
            if (self.path != "/v1/responses" or self.headers.get("Authorization") != "Bearer " + credential
                    or body.get("tools", []) != [] or body.get("model") != "synthetic-local"):
                faults.append("unexpected-protocol-auth-model-or-tools")
                self.send_error(400)
                return
            history = json.dumps(body["input"])
            if not all(value in history for value in (OLD_USER, OLD_ASSISTANT, PROMPT)):
                faults.append("missing-restored-context")
                self.send_error(400)
                return
            events = [
                {"type": "response.created", "response": {"id": "resp_synthetic"}},
                {"type": "response.output_item.done", "item": {"id": "msg_synthetic", "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": ANSWER}]}},
                {"type": "response.completed", "response": {"id": "resp_synthetic",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}},
            ]
            data = "".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, (tmp_path / "native.stderr").open("wb") as stderr:
        _configure(native_home, server.server_port, tmp_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment["JOSH_ROOM_SYNTHETIC_CODEX_KEY"] = credential
        process = None
        try:
            process = subprocess.Popen([executable, "app-server", "--stdio", "--strict-config"], cwd=workspace,
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0, start_new_session=True)
            protocol = Protocol(process)
            initialized = protocol.request(0, "initialize", {"clientInfo": {"name": "josh_room_synthetic_acceptance", "version": "0.1"},
                "capabilities": {"experimentalApi": True}})
            assert initialized["codexHome"] == str(native_home)
            process.stdin.write(b'{"method":"initialized","params":{}}\n')
            process.stdin.flush()
            resumed = protocol.request(1, "thread/resume", {"threadId": SESSION, "cwd": str(workspace), "model": "synthetic-local",
                "modelProvider": "offline", "approvalPolicy": "never", "sandbox": "read-only"})["thread"]
            assert resumed["id"] == SESSION and resumed["cwd"] == str(workspace)
            turn = protocol.request(2, "turn/start", {"threadId": SESSION, "input": [{"type": "text", "text": PROMPT}],
                "model": "synthetic-local"})["turn"]
            deadline = time.monotonic() + 40
            while not any(message.get("method") == "turn/completed" for message in protocol.messages):
                protocol.read(deadline)
            completed = next(message["params"]["turn"] for message in protocol.messages if message.get("method") == "turn/completed")
            assert completed["id"] == turn["id"] and completed["status"] == "completed", faults
            selected = protocol.request(3, "thread/read", {"threadId": SESSION, "includeTurns": True})["thread"]
            assert selected["id"] == SESSION and selected["sessionId"] == SESSION and selected["cwd"] == str(workspace)
            assert ANSWER in json.dumps(selected)
            assert faults == [] and len(requests) == 1
            assert requests[0]["tools"] == []
        finally:
            if process is not None:
                process.stdin.close()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                process.stdout.close()
            server.shutdown()
            thread.join(timeout=5)
    after = (native_home / relative).read_bytes()
    assert after.startswith(payload) and ANSWER.encode() in after and PROMPT.encode() in after
    rows = [json.loads(row) for row in after.splitlines()]
    assert rows[0]["payload"]["id"] == SESSION
    assert not any(row.get("type") == "response_item" and row["payload"].get("type") in {"function_call", "custom_tool_call"} for row in rows)
    assert not (native_home / "auth.json").exists()
    for directory in (native_home, home):
        for path in directory.rglob("*"):
            if path.is_file():
                assert credential.encode() not in path.read_bytes()
    assert credential.encode() not in (tmp_path / "native.stderr").read_bytes()
    _loopback_namespace()
    print(json.dumps({"provider": "codex", "version": agents.CODEX_VERSION, "backend": "deterministic-loopback",
        "provider_session_id": SESSION, "native_new_turn_observed": True, "hosted_provider_acceptance": False,
        "requests": 1, "tools": 0, "original_history_prefix_unchanged": True, "external_network_routes": 0,
        "fresh_native_auth": "generated-disposable-api-key"}))
