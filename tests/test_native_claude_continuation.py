"""Opt-in original-ID native turn against a deterministic local test backend.

Run only inside a new network namespace with loopback enabled. This tests the
native CLI protocol and restored context, never hosted inference or billing.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_native_claude_acceptance import (
    ASSISTANT,
    SESSION,
    cold_claude,  # noqa: F401 - register the shared encrypted cold-history fixture.
)

from josh_room.claude_native_history import ClaudeHistoryError, validate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux")
        or os.environ.get("JOSH_ROOM_TEST_CLAUDE_CONTINUATION") != "1",
        reason="explicit isolated synthetic Claude continuation opt-in required",
    ),
]
CLAUDE_SHA256 = "b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56"
PROMPT = "synthetic next turn after cold restore"
ANSWER = "SYNTHETIC_CLAUDE_RESUMED"


def _loopback_namespace():
    outer = os.environ.get("JOSH_ROOM_TEST_NATIVE_OUTER_NETNS")
    assert outer and outer.startswith("net:["), "launcher must record its outer network namespace"
    assert os.readlink("/proc/self/ns/net") != outer, "fresh network namespace required"
    assert {name for _index, name in socket.if_nameindex()} == {"lo"}, "external network interface present"
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    assert not routes, "IPv4 route table must be empty"
    for row in Path("/proc/net/ipv6_route").read_text().splitlines():
        fields = row.split()
        assert fields[-1] == "lo", "external IPv6 route present"
        # Linux also lists reject-default routes on lo. A usable default route
        # must not be mistaken for this harmless namespace placeholder.
        if fields[0] == "0" * 32 and fields[1] == "00":
            assert int(fields[8], 16) & 0x200, "usable IPv6 default route present"


def test_native_claude_resumes_cold_original_id_for_one_loopback_turn(request, tmp_path):
    _loopback_namespace()
    executable = os.environ.get("JOSH_ROOM_TEST_CLAUDE_CLI") or shutil.which("claude")
    if not executable:
        pytest.skip("installed pinned native Claude CLI required")
    executable = str(Path(executable).resolve())
    assert hashlib.sha256(Path(executable).read_bytes()).hexdigest() == CLAUDE_SHA256
    case = request.getfixturevalue("cold_claude")
    workspace = Path(case["binding"]["workspace"])
    config = case["root"] / "claude"
    restored = config / case["relative"]
    assert restored.read_bytes() == case["payload"]
    assert not (config / ".credentials.json").exists()
    assert not (config / "settings.json").exists()
    # The resumed CLI cannot discover the original source profile as fallback.
    shutil.rmtree(case["source"])
    home = tmp_path / "native-home"
    home.mkdir(mode=0o700)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "TERM": "dumb",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DO_NOT_TRACK": "1",
    }
    # --bare never reads OAuth/keychains. No managed settings may supply an
    # additional host customization outside the fresh HOME/config roots.
    assert not Path("/etc/claude-code").exists(), "host managed settings are outside this acceptance"
    version = subprocess.run([executable, "--version"], env=environment, capture_output=True, text=True, check=True, timeout=10)
    assert version.stdout.strip() == "2.1.310 (Claude Code)"
    credential = "synthetic-local-" + secrets.token_hex(24)
    requests = []
    faults = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            if not 0 < length <= 1024 * 1024 or len(requests) >= 4:
                faults.append("request-limit")
                self.send_error(400)
                return
            body = json.loads(self.rfile.read(length))
            requests.append({"path": self.path, "body": body,
                             "credential_verified": self.headers.get("x-api-key") == credential})
            if not requests[-1]["credential_verified"]:
                faults.append("unauthorized")
                self.send_error(401)
                return
            if (self.path.split("?")[0] != "/v1/messages" or body.get("stream") is not True
                    or body.get("tools") != [] or body.get("model") != "claude-synthetic-offline"):
                faults.append("unexpected-protocol")
                self.send_error(400)
                return
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_synthetic_resumed", "type": "message", "role": "assistant",
                    "model": body.get("model"), "content": [], "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0},
                }}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": ANSWER}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {
                    "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            data = "".join(f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.send_header("connection", "close")
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment.update(ANTHROPIC_API_KEY=credential, ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}")
    command = [
        executable, "--bare", "--safe-mode", "--tools", "", "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
        "--disable-slash-commands", "--no-chrome", "--permission-mode", "dontAsk",
        "--permission-prompts", "none", "--model", "claude-synthetic-offline",
        "--system-prompt", "Synthetic local continuation acceptance. No tools.",
        "--resume", SESSION, "--print", PROMPT, "--output-format", "json",
    ]
    try:
        process = subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, timeout=30, check=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    for name, value in (("stdout", process.stdout), ("stderr", process.stderr)):
        (tmp_path / f"claude.{name}").write_text(value.replace(credential, "<redacted-disposable-key>"))
    assert process.returncode == 0, "isolated Claude continuation failed; inspect task-private sanitized output"
    _loopback_namespace()
    result = json.loads(process.stdout)
    assert result["session_id"] == SESSION and result["result"] == ANSWER
    assert result["is_error"] is False and result["num_turns"] == 1
    assert result["subagent_stats"]["spawned"] == 0
    assert result["permission_denials"] == []
    assert result["usage"]["server_tool_use"] == {"web_search_requests": 0, "web_fetch_requests": 0}
    assert faults == [] and len(requests) == 1
    request = requests[0]
    assert request["credential_verified"] is True
    assert request["body"]["model"] == "claude-synthetic-offline" and request["body"]["tools"] == []
    history = json.dumps(request["body"]["messages"])
    assert "synthetic Claude question" in history and "synthetic retained Claude answer" in history and PROMPT in history
    after = restored.read_bytes()
    assert after.startswith(case["payload"])
    rows = [json.loads(row) for row in after.splitlines()]
    messages = [row for row in rows if row.get("type") in {"user", "assistant"}]
    assert len(messages) == 4 and all(row["sessionId"] == SESSION and row["cwd"] == str(workspace) for row in messages)
    assert messages[2]["parentUuid"] == ASSISTANT and messages[2]["message"]["content"] == PROMPT
    assert messages[3]["message"]["content"] == [{"type": "text", "text": ANSWER}]
    by_id = {row["uuid"]: row for row in rows if "uuid" in row}
    ancestry = []
    current = messages[3]["uuid"]
    while current != ASSISTANT:
        assert current not in ancestry and current in by_id
        ancestry.append(current)
        current = by_id[current]["parentUuid"]
    assert messages[2]["uuid"] in ancestry
    assert not (config / ".credentials.json").exists()
    for directory in (config, home):
        for path in directory.rglob("*"):
            if path.is_file():
                assert credential.encode() not in path.read_bytes()
    # Native continuation appends runtime metadata beyond the admitted capture
    # subset. This acceptance does not silently widen that safety boundary.
    with pytest.raises(ClaudeHistoryError):
        validate(str(case["relative"]), rows, str(workspace))
    print(json.dumps({"provider": "claude", "version": "2.1.310", "backend": "deterministic-loopback",
                      "provider_session_id": SESSION, "native_new_turn_observed": True,
                      "hosted_provider_acceptance": False, "requests": 1, "tools": 0,
                      "new_messages": 2, "original_history_prefix_unchanged": True,
                      "external_network_routes": 0, "fresh_native_auth": "generated-disposable-api-key"}))
