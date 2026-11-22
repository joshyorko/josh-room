"""Explicit, offline provider-history import into one newly owned T3 process."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from . import native_agents as native
from .cancellation import (
    CLICancelled,
    defer_sigterm_cancellation,
    sigterm_cancellation,
    terminate_owned_process,
)
from .native_agent_owned_server import OwnedServer

T3_SHA256 = "93e6bf69ad395da3a2ba9bec2d8bce99579bc9a5c79167735132af9ac7d301ab"
NODE_SHA256 = "f2a178edacbe90a6ce67aa2c7b675f2c079eece8e761eb4363e2b6d719693b7e"
_NS_GET_USERNS = 0xB701
_RUNNABLE_TABLES = (
    "orchestration_v2_effect_outbox", "orchestration_v2_projection_runs",
    "orchestration_v2_projection_run_attempts", "orchestration_v2_projection_runtime_requests",
    "orchestration_v2_projection_provider_turns", "orchestration_v2_thread_launch_workflows", "scheduled_tasks",
)
_RPC_CLIENT = r"""let input='';for await(const chunk of process.stdin)input+=chunk;
const c=JSON.parse(input);
const ws=new WebSocket(`ws://127.0.0.1:${c.port}/ws?orchestrationProtocol=2`,{headers:{Authorization:`Bearer ${c.token}`}});
const timer=setTimeout(()=>process.exit(3),10000);
ws.addEventListener('open',()=>ws.send(JSON.stringify({_tag:'Request',id:'1',tag:c.tag,payload:c.payload,headers:[]})));
ws.addEventListener('error',()=>process.exit(4));
ws.addEventListener('message',async event=>{
const text=typeof event.data==='string'?event.data:await event.data.text();
for(const line of text.split('\n').filter(Boolean)){
const parsed=JSON.parse(line);for(const m of Array.isArray(parsed)?parsed:[parsed]){
if(m._tag==='Exit'){clearTimeout(timer);console.log(JSON.stringify(m.exit));ws.close();}
else if(m._tag==='Chunk')ws.send(JSON.stringify({_tag:'Ack',requestId:m.requestId}));
}}});
"""


def _isolated_network():
    native._linux()
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        native._fail("t3-import-network-isolation-required")
    try:
        mapping = Path("/proc/self/uid_map").read_text().split()
        if len(mapping) != 3 or mapping[0] != "0" or mapping[2] != "1":
            native._fail("t3-import-network-isolation-required")
        with Path("/proc/self/ns/net").open("rb") as network:
            owner = fcntl.ioctl(network.fileno(), _NS_GET_USERNS)
            try:
                expected = Path("/proc/self/ns/user").stat()
                actual = os.fstat(owner)
                if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    native._fail("t3-import-network-isolation-required")
            finally:
                os.close(owner)
    except OSError:
        native._fail("t3-import-network-isolation-required")


def _clean_profile(root, tool, workspace):
    if any((root / "t3").iterdir()):
        native._fail("t3-import-fresh-profile-required")
    _clean_provider_homes(root, tool, workspace)


def _clean_provider_homes(root, tool, workspace):
    for provider in ("codex", "claude"):
        allowed = {"sessions" if provider == "codex" else "projects"} if provider == tool else set()
        if {entry.name for entry in (root / provider).iterdir()} != allowed:
            native._fail("t3-import-provider-home-not-clean")
    if tool == "claude" and {entry.name for entry in (root / "claude/projects").iterdir()} != {native._claude_key(workspace)}:
        native._fail("t3-import-provider-home-not-clean")


def _runtime(executable):
    executable = native._canonical(Path(executable))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        native._fail("t3-import-runtime-unqualified")
    with executable.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != T3_SHA256:
        native._fail("t3-import-runtime-unqualified")
    node = shutil.which("node")
    if not node:
        native._fail("t3-import-rpc-client-unavailable")
    node = native._canonical(Path(node).resolve())
    with node.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != NODE_SHA256:
            native._fail("t3-import-rpc-client-unqualified")
    return str(executable), str(node)


def _run(arguments, environment, cwd, *, stdin=None):
    result = subprocess.run(
        arguments, env=environment, cwd=cwd, input=stdin, capture_output=True,
        text=True, timeout=20, check=False,
    )
    if result.returncode or len(result.stdout) > native.MAX_BYTES:
        native._fail("t3-import-command-failed")
    return result.stdout


def _environment(scratch, root):
    home, binaries = scratch / "home", scratch / "bin"
    home.mkdir(mode=0o700)
    binaries.mkdir(mode=0o700)
    for name in ("codex", "claude", "pi", "git"):
        native._write(binaries / name, b"#!/bin/sh\nexit 127\n")
        (binaries / name).chmod(0o700)
    return {
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
        "PATH": str(binaries), "TERM": "dumb", "NO_COLOR": "1", "T3CODE_NO_UPDATE_CHECK": "1",
        "T3CODE_HOME": str(root / "t3"), "CODEX_HOME": str(root / "codex"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
    }


def _settings(root, environment, tool):
    providers = {}
    for driver in ("codex", "claudeAgent", "pi"):
        enabled = driver == ("codex" if tool == "codex" else "claudeAgent")
        providers[driver] = {"driver": driver, "enabled": enabled}
        if driver != "pi":
            providers[driver]["config"] = {
                "binaryPath": str(Path(environment["PATH"]) / ("uninstalled-" + driver)),
                "homePath": str(root / ("codex" if driver == "codex" else "claude")),
            }
    userdata = root / "t3/userdata"
    userdata.mkdir(mode=0o700)
    native._write(userdata / "settings.json", json.dumps({"providerInstances": providers}).encode())


def _wait_owned(process, **expected):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            native._fail("t3-import-server-exited")
        try:
            return OwnedServer(process, **expected)
        except RuntimeError:
            time.sleep(0.1)
    native._fail("t3-import-server-unverifiable")


def _rpc(owner, node, environment, workspace, token, tag, payload, guard):
    deadline = time.monotonic() + 12
    while True:
        guard()
        owner.check()
        try:
            result = json.loads(_run(
                [node, "--input-type=module", "-e", _RPC_CLIENT], environment, workspace,
                stdin=json.dumps({"port": owner.port, "token": token, "tag": tag, "payload": payload}),
            ))
        except native.NativeAgentError:
            if tag == "agentSessions.scan" and time.monotonic() < deadline:
                time.sleep(0.1)
                continue
            raise
        owner.check()
        guard()
        if result.get("_tag") == "Success":
            return result["value"]
        # A busy startup must never cause an uncertain mutating request to retry.
        if tag != "agentSessions.scan" or time.monotonic() >= deadline:
            native._fail("t3-import-rpc-refused")
        time.sleep(0.1)


def _stopped_receipt(root, workspace, tool, identities):
    with sqlite3.connect((root / "t3/userdata/statev2.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        if any(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in _RUNNABLE_TABLES):
            raise native.NativeAgentError("t3-import-runnable-state-found", committed=True)
        rows = connection.execute("SELECT thread_id,status,runtime_payload_json FROM provider_session_runtime").fetchall()
        driver = "codex" if tool == "codex" else "claudeAgent"
        expected = {"import:" + driver + ":" + identity for identity in identities}
        if {row[0] for row in rows} != expected or any(row[1] != "stopped" or json.loads(row[2])["cwd"] != workspace for row in rows):
            raise native.NativeAgentError("t3-import-runtime-receipt-mismatch", committed=True)
    return [{"provider_session_id": identity, "new_t3_thread_id": "import:" + driver + ":" + identity} for identity in sorted(identities)]


def _revoke_local_sessions(executable, environment, workspace, root):
    command = [executable, "auth", "session", "list", "--base-dir", str(root / "t3"), "--json"]
    sessions = json.loads(_run(command, environment, workspace))
    if not isinstance(sessions, list) or len(sessions) > 64:
        native._fail("t3-import-auth-cleanup-failed")
    for session in sessions:
        identity = session.get("sessionId") if isinstance(session, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}", identity):
            native._fail("t3-import-auth-cleanup-failed")
        _run([executable, "auth", "session", "revoke", "--base-dir", str(root / "t3"), identity], environment, workspace)
    if json.loads(_run(command, environment, workspace)) != []:
        native._fail("t3-import-auth-cleanup-failed")


def import_history(root, *, executable, tool="codex", **binding):
    outcome = {"may_have_imported": False}
    try:
        return _import_history(root, executable, tool, binding, outcome)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, sqlite3.Error,
            subprocess.TimeoutExpired, KeyboardInterrupt, CLICancelled) as error:
        if outcome["may_have_imported"]:
            raise native.NativeAgentError("t3-import-outcome-uncertain", committed=True)
        if isinstance(error, (native.NativeAgentError, KeyboardInterrupt, CLICancelled)):
            raise
        raise native.NativeAgentError("t3-import-refused") from error


def _import_history(root, executable, tool, binding, outcome):
    _isolated_network()
    tool = native._tool(tool)
    binding = native._binding(**binding)
    root = native._load(root, binding)
    executable, node = _runtime(executable)
    recipients = native._json(native._read(root / native.POLICY, 4096))["recipients"]
    admission = native._admit(binding, recipients, root=root, operation="import-history", tool=tool)
    with native._lock(root) as lock_fd, sigterm_cancellation():
        _clean_profile(root, tool, binding["workspace"])
        with native._capture_barrier(root, tool, binding["workspace"]) as (barrier, inventory):
            entries, _ = native._entries(root, binding, barrier, inventory, tool)
            identities = {native._session_id(entry["path"], base64.b64decode(entry["data"], validate=True), tool) for entry in entries}
            if len(identities) != len(entries):
                native._fail("native-session-identity-ambiguous")

            def guard():
                _isolated_network()
                _clean_provider_homes(root, tool, binding["workspace"])
                if native._admit(binding, recipients, root=root, operation="import-history", tool=tool) != admission:
                    native._fail("native-admission-changed")
                if native._inventory(root, tool, binding["workspace"]) != inventory:
                    native._fail("source-changed")
                barrier.check()

            with tempfile.TemporaryDirectory(prefix="josh-room-import-") as directory:
                environment = _environment(Path(directory), root)
                guard()
                _settings(root, environment, tool)
                guard()
                project_output = _run([executable, "project", "add", binding["workspace"], "--base-dir", str(root / "t3")], environment, binding["workspace"])
                project_match = re.search(r"Added project ([^ ]+)", project_output)
                if not project_match:
                    native._fail("t3-import-project-unavailable")
                guard()
                process = None
                try:
                    issued = json.loads(_run([
                        executable, "auth", "session", "issue", "--base-dir", str(root / "t3"),
                        "--scope", "orchestration:read", "--scope", "orchestration:operate", "--ttl", "2m", "--json",
                    ], environment, binding["workspace"]))
                    with socket.socket() as endpoint:
                        endpoint.bind(("127.0.0.1", 0))
                        port = endpoint.getsockname()[1]
                    guard()
                    process = native._start_server(root, binding, executable, port, environment, lock_fd, desktop=True)
                    with _wait_owned(process, executable=Path(executable), argv=process.args, cwd=Path(binding["workspace"]), environment=environment, port=port) as owner:
                        candidates = _rpc(owner, node, environment, binding["workspace"], issued["token"], "agentSessions.scan", {}, guard)["candidates"]
                        driver = "codex" if tool == "codex" else "claudeAgent"
                        if len(candidates) != 1 or candidates[0]["path"] != binding["workspace"] or candidates[0]["sources"] != [driver] or candidates[0]["threadCount"] != len(identities):
                            native._fail("t3-import-scan-mismatch")
                        outcome["may_have_imported"] = True
                        result = _rpc(owner, node, environment, binding["workspace"], issued["token"], "agentSessions.import", {
                            "projectId": project_match.group(1), "expectedWorkspaceRoot": binding["workspace"],
                        }, guard)
                        if result != {"importedCount": len(identities), "skippedCount": 0}:
                            raise native.NativeAgentError("t3-import-result-mismatch", committed=True)
                finally:
                    with defer_sigterm_cancellation():
                        if process is not None and process.poll() is None:
                            terminate_owned_process(process, grace_seconds=2)
                        if process is not None and process.poll() is None:
                            native._fail("t3-import-server-stop-failed")
                        _revoke_local_sessions(executable, environment, binding["workspace"], root)
                guard()
                sessions = _stopped_receipt(root, binding["workspace"], tool, identities)
    return {"ok": True, "state": "history-imported", "tool": tool, "imported_count": len(sessions),
            "sessions": sessions, "cwd_matches": True, "runnable_effects": 0,
            "original_t3_identity_restored": False, "native_resume_verified": False}
