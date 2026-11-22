"""Opt-in installed T3 scan/import; run only in a loopback-only network namespace.

Set JOSH_ROOM_TEST_T3 to the installed executable and JOSH_ROOM_TEST_T3_UNDICI
 to its node_modules/undici/index.js. No provider turn or provider auth is called.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import native_agents as a
from josh_room.claude_native_history import project_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="Linux network namespace isolation"
    ),
]
SESSION = "11111111-1111-4111-8111-111111111111"
RPC_HELPER = r"""import {createRequire} from 'node:module';
let input='';for await(const chunk of process.stdin)input+=chunk;
const config=JSON.parse(input);
const require=createRequire(config.undici);
const {WebSocket}=require('undici');
const ws=new WebSocket(`ws://127.0.0.1:${config.port}/ws?orchestrationProtocol=2`,{headers:{Authorization:`Bearer ${config.token}`}});
const timer=setTimeout(()=>{console.error('RPC timeout');process.exit(3)},15000);
const outputs=[];let next=0;
ws.addEventListener('open',()=>send());
function send(){const request=config.requests[next];ws.send(JSON.stringify({_tag:'Request',id:String(next+1),tag:request.tag,payload:request.payload,headers:[]}));}
ws.addEventListener('error',event=>{console.error('RPC connection error: '+event.error?.message);process.exit(4)});
ws.addEventListener('message',async event=>{
const text=typeof event.data==='string'?event.data:await event.data.text();
for(const line of text.split('\n').filter(Boolean)){
const messages=JSON.parse(line);for(const message of Array.isArray(messages)?messages:[messages]){
if(message._tag==='Exit') {outputs.push(message);next++;if(next===config.requests.length){clearTimeout(timer);console.log(JSON.stringify(outputs));ws.close();}else send();}
else if(message._tag==='Chunk')ws.send(JSON.stringify({_tag:'Ack',requestId:message.requestId}));
}}
});
"""


def run(args, env, cwd=None):
    p = subprocess.run(
        args, check=False, env=env, cwd=cwd, capture_output=True, text=True, timeout=30
    )
    if p.returncode:
        raise RuntimeError(
            f"command failed ({Path(args[0]).name}, {args[1]}, exit {p.returncode}): "
            + p.stderr[:500]
        )
    return p.stdout


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_native_t3_imports_cold_provider_history(tmp_path, monkeypatch, tool):
    T3 = os.environ.get("JOSH_ROOM_TEST_T3")
    undici = os.environ.get("JOSH_ROOM_TEST_T3_UNDICI")
    NODE = shutil.which("node")
    if (
        not T3
        or not undici
        or not NODE
        or not shutil.which("age")
        or not shutil.which("age-keygen")
    ):
        pytest.skip("explicit installed T3, undici, Node and real age required")
    assert {name for _index, name in socket.if_nameindex()} == {"lo"}, (
        "run inside an isolated network namespace with loopback only"
    )
    BASE = tmp_path
    BASE.chmod(0o700)
    monkeypatch.setattr(
        a,
        "_admit",
        lambda *args, **kwargs: {
            "admitted": True,
            "authority_sha256": "synthetic-authority",
        },
    )
    helper = BASE / "rpc.mjs"
    helper.write_text(RPC_HELPER)
    PATH = os.environ["PATH"]
    d = BASE / ("case-" + tool)
    d.mkdir(mode=0o700)
    workspace = d / "workspace"
    workspace.mkdir(mode=0o700)
    home = d / "home"
    home.mkdir(mode=0o700)
    env = {
        "PATH": PATH,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "T3CODE_NO_UPDATE_CHECK": "1",
    }
    recipients = []
    keys = []
    for i in range(2):
        key = d / f"synthetic-{i}.agekey"
        run(["age-keygen", "-o", str(key)], env)
        key.chmod(0o600)
        keys.append(key)
        recipients.append(run(["age-keygen", "-y", str(key)], env).strip())
    binding = {
        "dimension": "synthetic",
        "room": "demo",
        "profile": "personal",
        "workspace": workspace,
    }
    source = d / "source"
    cold = d / "cold"
    a.configure(source, recipients=recipients, tool=tool, **binding)
    if tool == "codex":
        relative = Path(
            f"sessions/2026/11/22/rollout-2026-11-22T15-00-00-{SESSION}.jsonl"
        )
        stamp = "2026-11-22T15:00:00Z"
        rows = [
            {
                "timestamp": stamp,
                "type": "session_meta",
                "payload": {
                    "id": SESSION,
                    "timestamp": stamp,
                    "cwd": str(workspace),
                    "originator": "codex_cli_rs",
                    "cli_version": "0.171.0",
                    "source": "cli",
                    "model_provider": "offline",
                },
            },
            {
                "timestamp": stamp,
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "synthetic discovery question",
                    "images": None,
                    "local_images": [],
                },
            },
            {
                "timestamp": stamp,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "synthetic retained answer"}
                    ],
                },
            },
        ]
    else:
        relative = Path("projects") / project_key(str(workspace)) / f"{SESSION}.jsonl"
        common = {
            "isSidechain": False,
            "userType": "external",
            "cwd": str(workspace),
            "sessionId": SESSION,
            "version": "2.1.310",
            "gitBranch": "synthetic",
            "timestamp": "2026-11-22T16:00:00Z",
        }
        user = "22222222-2222-4222-8222-222222222222"
        rows = [
            {
                **common,
                "parentUuid": None,
                "type": "user",
                "uuid": user,
                "message": {"role": "user", "content": "synthetic Claude question"},
            },
            {
                **common,
                "parentUuid": user,
                "type": "assistant",
                "uuid": "33333333-3333-4333-8333-333333333333",
                "message": {
                    "id": "msg_synthetic",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-synthetic-offline",
                    "content": [
                        {"type": "text", "text": "synthetic retained Claude answer"}
                    ],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
    original = source / tool / relative
    original.parent.mkdir(parents=True, mode=0o700)
    for parent in original.parents:
        if parent.is_relative_to(source):
            parent.chmod(0o700)
    payload = ("".join(json.dumps(row) + "\n" for row in rows)).encode()
    original.write_bytes(payload)
    original.chmod(0o600)
    reference = d / "retained.age"
    a.capture(source, output=reference, recipients=recipients, tool=tool, **binding)
    a.configure(cold, recipients=recipients, tool=tool, **binding)
    materialized = a.materialize_session(
        cold,
        source=reference,
        identity_paths=keys[:1],
        session_id=SESSION,
        tool=tool,
        **binding,
    )
    assert materialized["native_resume_verified"] is False
    env.update(
        T3CODE_HOME=str(cold / "t3"),
        CODEX_HOME=str(cold / "codex"),
        CLAUDE_CONFIG_DIR=str(cold / "claude"),
    )
    bindir = d / "bin"
    bindir.mkdir(mode=0o700)
    calllog = d / "provider-calls.txt"
    for binary, version in [
        ("codex", "codex-cli 0.171.0"),
        ("claude", "2.1.310 (Claude Code)"),
        ("pi", "0.0.0"),
    ]:
        stub = bindir / binary
        stub.write_text(
            '#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then printf "%s\\n" "'
            + binary
            + ':version" >> "'
            + str(calllog)
            + '"; printf "%s\\n" "'
            + version
            + '"; exit 127; fi\nprintf "%s\\n" "'
            + binary
            + ':denied" >> "'
            + str(calllog)
            + '"\nexit 97\n'
        )
        stub.chmod(0o700)
    env["PATH"] = str(bindir) + ":" + PATH
    userdata = cold / "t3/userdata"
    userdata.mkdir(mode=0o700, exist_ok=True)
    (userdata / "settings.json").write_text(
        json.dumps(
            {
                "providerInstances": {
                    "codex": {
                        "driver": "codex",
                        "enabled": tool == "codex",
                        "config": {
                            "binaryPath": str(bindir / "uninstalled-codex"),
                            "homePath": str(cold / "codex"),
                        },
                    },
                    "claudeAgent": {
                        "driver": "claudeAgent",
                        "enabled": tool == "claude",
                        "config": {
                            "binaryPath": str(bindir / "uninstalled-claude"),
                            "homePath": str(cold / "claude"),
                        },
                    },
                    "pi": {"driver": "pi", "enabled": False},
                }
            }
        )
    )
    # Supported CLI project mutation and local least-scope bearer creation, before any server owns SQLite.
    projectout = run(
        [T3, "project", "add", str(workspace), "--base-dir", str(cold / "t3")],
        env,
        workspace,
    )
    match = re.search(r"Added project ([^ ]+)", projectout)
    assert match, "missing project id"
    project = match.group(1)
    issued = json.loads(
        run(
            [
                T3,
                "auth",
                "session",
                "issue",
                "--base-dir",
                str(cold / "t3"),
                "--scope",
                "orchestration:read",
                "--scope",
                "orchestration:operate",
                "--ttl",
                "2m",
                "--json",
            ],
            env,
            workspace,
        )
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    process = subprocess.Popen(
        [
            T3,
            "serve",
            "--base-dir",
            str(cold / "t3"),
            "--mode",
            "desktop",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-browser",
        ],
        env=env,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (d / "owned-server.pid").write_text(str(process.pid))
    try:
        for ready_attempt in range(150):
            assert process.poll() is None, "owned server exited before readiness"
            try:
                with socket.create_connection(
                    ("127.0.0.1", port), timeout=3
                ) as diagnostic:
                    diagnostic.sendall(
                        (
                            "GET /ws?orchestrationProtocol=2 HTTP/1.1\r\nHost: 127.0.0.1:"
                            + str(port)
                            + "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer "
                            + issued["token"]
                            + "\r\n\r\n"
                        ).encode()
                    )
                    status = diagnostic.recv(4096).split(b"\r\n")[0].decode()
                if " 101 " in status:
                    break
                if " 503 " not in status:
                    raise RuntimeError("Unexpected handshake status: " + status)
            except ConnectionRefusedError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Owned server readiness timeout")
        requests = [
            {"tag": "agentSessions.scan", "payload": {}},
            {
                "tag": "agentSessions.import",
                "payload": {
                    "projectId": project,
                    "expectedWorkspaceRoot": str(workspace) + "-wrong",
                },
            },
            {
                "tag": "agentSessions.import",
                "payload": {
                    "projectId": project,
                    "expectedWorkspaceRoot": str(workspace),
                },
            },
            {"tag": "agentSessions.scan", "payload": {}},
            {
                "tag": "agentSessions.import",
                "payload": {
                    "projectId": project,
                    "expectedWorkspaceRoot": str(workspace),
                },
            },
        ]
        rpc = subprocess.run(
            [NODE, str(BASE / "rpc.mjs")],
            check=False,
            input=json.dumps(
                {
                    "token": issued["token"],
                    "port": port,
                    "requests": requests,
                    "undici": undici,
                }
            ),
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if rpc.returncode:
            raise RuntimeError(f"RPC failed {rpc.returncode}: {rpc.stderr[:300]}")
        results = json.loads(rpc.stdout)
        assert [result["exit"]["_tag"] for result in results] == [
            "Success",
            "Failure",
            "Success",
            "Success",
            "Success",
        ]
        initial = results[0]["exit"]["value"]["candidates"]
        assert (
            len(initial) == 1
            and initial[0]["path"] == str(workspace)
            and initial[0]["threadCount"] == 1
        )
        assert initial[0]["sources"] == (
            ["codex"] if tool == "codex" else ["claudeAgent"]
        )
        assert results[2]["exit"]["value"] == {"importedCount": 1, "skippedCount": 0}
        assert results[4]["exit"]["value"] == {"importedCount": 0, "skippedCount": 0}
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        (d / "owned-server.pid").unlink()
    # Read-only SQLite aggregate/identity evidence; never seed or edit native T3 tables.
    conn = sqlite3.connect(f"file:{userdata}/statev2.sqlite?mode=ro", uri=True)
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    counts = {
        name: conn.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
        for name in tables
    }
    schema = {
        name: [r[1] for r in conn.execute('PRAGMA table_info("' + name + '")')]
        for name in tables
    }
    for name in [
        "orchestration_v2_effect_outbox",
        "orchestration_v2_projection_runs",
        "orchestration_v2_projection_run_attempts",
        "orchestration_v2_projection_runtime_requests",
        "orchestration_v2_projection_provider_turns",
        "orchestration_v2_thread_launch_workflows",
        "scheduled_tasks",
    ]:
        assert counts[name] == 0, name
    assert counts["orchestration_v2_projection_threads"] == 1
    assert counts["orchestration_v2_projection_messages"] == 2
    driver = "codex" if tool == "codex" else "claudeAgent"
    derived = "import:" + driver + ":" + SESSION
    runtime = dict(
        zip(
            schema["provider_session_runtime"],
            conn.execute("SELECT * FROM provider_session_runtime").fetchone(),
        )
    )
    assert runtime["thread_id"] == derived and runtime["status"] == "stopped"
    assert json.loads(runtime["runtime_payload_json"])["cwd"] == str(workspace)
    cursor = json.loads(runtime["resume_cursor_json"])
    assert cursor == (
        {"threadId": SESSION}
        if tool == "codex"
        else {"threadId": derived, "resume": SESSION}
    )
    native = json.loads(
        conn.execute(
            "SELECT payload_json FROM orchestration_v2_projection_provider_threads"
        ).fetchone()[0]
    )
    assert native["nativeThreadRef"] == {
        "driver": driver,
        "nativeId": SESSION,
        "strength": "strong",
    }
    assert native["status"] == "idle" and native["pendingBackgroundTasks"] == []
    app = json.loads(
        conn.execute(
            "SELECT payload_json FROM orchestration_v2_projection_threads"
        ).fetchone()[0]
    )
    assert app["id"] == derived and app["historyOrigin"] == "v1_import"
    messages = [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT payload_json FROM orchestration_v2_projection_messages ORDER BY message_id"
        )
    ]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert [message["text"] for message in messages] == (
        ["synthetic discovery question", "synthetic retained answer"]
        if tool == "codex"
        else ["synthetic Claude question", "synthetic retained Claude answer"]
    )
    conn.close()
    assert (cold / tool / relative).read_bytes() == payload
    calls = calllog.read_text().splitlines() if calllog.exists() else []
    assert not any(":denied" in value for value in calls), calls
    assert calls == ["codex:version"], calls
    assert not (cold / "codex/auth.json").exists()
    assert not (cold / "claude/.credentials.json").exists()
