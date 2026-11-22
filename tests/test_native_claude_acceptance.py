"""Claude native-file recovery and optional actual SDK offline discovery."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from josh_room import native_agents as agents
from josh_room.claude_native_history import CLAUDE_VERSION, project_key

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native recovery contract")
SESSION = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
ASSISTANT = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def cold_claude(tmp_path, monkeypatch):
    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age required")
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    binding = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": workspace}
    keys, recipients = [], []
    for index in range(2):
        key = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
        key.chmod(0o600)
        recipients.append(subprocess.run(["age-keygen", "-y", str(key)], capture_output=True, check=True, text=True).stdout.strip())
        keys.append(key)
    monkeypatch.setattr(agents, "_admit", lambda *args, **kwargs: {"admitted": True, "authority_sha256": "synthetic-authority"})
    source, cold = tmp_path / "source", tmp_path / "cold"
    agents.configure(source, recipients=recipients, tool="claude", **binding)
    relative = Path("projects") / project_key(str(workspace)) / f"{SESSION}.jsonl"
    native = source / "claude" / relative
    native.parent.mkdir(mode=0o700, parents=True)
    for parent in native.parents:
        if parent.is_relative_to(source):
            parent.chmod(0o700)
    common = {"isSidechain": False, "userType": "external", "cwd": str(workspace), "sessionId": SESSION, "version": CLAUDE_VERSION, "gitBranch": "synthetic", "timestamp": "2026-11-22T16:00:00Z"}
    rows = [
        {**common, "parentUuid": None, "type": "user", "message": {"role": "user", "content": "synthetic Claude question"}, "uuid": USER},
        {**common, "parentUuid": USER, "type": "assistant", "message": {"id": "msg_synthetic", "type": "message", "role": "assistant", "model": "claude-synthetic-offline", "content": [{"type": "text", "text": "synthetic retained Claude answer"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}}, "uuid": ASSISTANT},
    ]
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    native.write_bytes(payload)
    native.chmod(0o600)
    (source / "claude/.credentials.json").write_text("excluded synthetic auth sentinel")
    (source / "claude/settings.json").write_text("excluded synthetic settings sentinel")
    reference = tmp_path / "claude.age"
    agents.capture(source, output=reference, recipients=recipients, tool="claude", **binding)
    assert agents.verify(source, source=reference, identity_paths=keys[:1], tool="claude", **binding)["files"] == 1
    agents.configure(cold, recipients=recipients, tool="claude", **binding)
    materialized = agents.materialize_session(cold, source=reference, identity_paths=keys[:1], session_id=SESSION, tool="claude", **binding)
    assert materialized["native_resume_verified"] is False
    return {"root": cold, "source": source, "reference": reference, "keys": keys, "recipients": recipients, "binding": binding, "relative": relative, "payload": payload}


def test_claude_native_cold_bytes_identity_exclusions_and_retry(cold_claude):
    case = cold_claude
    assert (case["root"] / "claude" / case["relative"]).read_bytes() == case["payload"]
    rows = [json.loads(row) for row in case["payload"].splitlines()]
    assert rows[0]["parentUuid"] is None and rows[1]["parentUuid"] == USER
    assert not (case["root"] / "claude/.credentials.json").exists()
    assert not (case["root"] / "claude/settings.json").exists()
    result = agents.materialize_session(case["root"], source=case["reference"], identity_paths=case["keys"][:1], session_id=SESSION, tool="claude", **case["binding"])
    assert result["recovered_commit"] is True and result["native_resume_verified"] is False


def test_claude_reference_cannot_be_reinterpreted_as_codex(cold_claude):
    case = cold_claude
    with pytest.raises(agents.NativeAgentError):
        agents.verify(case["source"], source=case["reference"], identity_paths=case["keys"][:1], tool="codex", **case["binding"])


def test_t3_projection_blocked_before_read_or_authority(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T3 projection must not read native data or authority")

    monkeypatch.setattr(agents, "_load", forbidden)
    monkeypatch.setattr(agents, "_admit", forbidden)
    with pytest.raises(agents.NativeAgentError, match="native-thread-projection-import-unavailable"):
        agents.capture(tmp_path / "unread", output=tmp_path / "absent.age", recipients=[], tool="t3")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.integration
def test_native_claude_sdk_discovers_cold_original_identity_without_process_auth_or_network(cold_claude, tmp_path):
    configured = os.environ.get("JOSH_ROOM_TEST_CLAUDE_SDK")
    if not configured or not shutil.which("node"):
        pytest.skip("pinned Claude SDK0.3.310 and Node required; set JOSH_ROOM_TEST_CLAUDE_SDK to installed sdk.mjs")
    sdk = Path(configured)
    assert sdk.name == "sdk.mjs" and json.loads((sdk.parent / "package.json").read_text())["version"] == "0.3.310"
    case = cold_claude
    home = tmp_path / "empty-home"
    home.mkdir(mode=0o700)
    environment = {"PATH": os.environ["PATH"], "HOME": str(home), "CLAUDE_CONFIG_DIR": str(case["root"] / "claude"), "XDG_CONFIG_HOME": str(home / "config")}
    script = r'''import assert from 'node:assert/strict';
import cp from 'node:child_process';
import net from 'node:net';
import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';
import {syncBuiltinESMExports} from 'node:module';
import {pathToFileURL} from 'node:url';
const calls=[];
const denied=name=>(...args)=>{calls.push(name);throw new Error('prohibited side effect')};
globalThis.fetch=denied('fetch');
for(const name of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork'])cp[name]=denied(name);
for(const [module,name] of [[net,'connect'],[net,'createConnection'],[http,'request'],[http,'get'],[https,'request'],[https,'get'],[tls,'connect']])module[name]=denied(name);
net.Socket.prototype.connect=denied('Socket.connect');
syncBuiltinESMExports();
const sdk=await import(pathToFileURL(process.argv[1]).href);
const cwd=process.argv[2],session='11111111-1111-4111-8111-111111111111';
const listing=await sdk.listSessions({dir:cwd,includeWorktrees:false,includeProgrammatic:false});
const info=await sdk.getSessionInfo(session,{dir:cwd});
const messages=await sdk.getSessionMessages(session,{dir:cwd});
const wrong=await sdk.listSessions({dir:cwd+'-other',includeWorktrees:false});
assert.equal(listing.length,1);assert.equal(listing[0].sessionId,session);assert.equal(info.sessionId,session);
assert.equal(messages.length,2);assert.equal(messages[0].uuid,'22222222-2222-4222-8222-222222222222');
assert.equal(messages[1].uuid,'33333333-3333-4333-8333-333333333333');
assert.equal(messages[0].session_id,session);assert.equal(messages[1].session_id,session);
assert.equal(messages[0].message.content,'synthetic Claude question');
assert.equal(messages[1].message.content[0].text,'synthetic retained Claude answer');
assert.deepEqual(wrong,[]);assert.deepEqual(calls,[]);
console.log(JSON.stringify({sessionId:session,messages:messages.length,calls}));'''
    process = subprocess.run(["node", "--input-type=module", "-e", script, str(sdk), str(case["binding"]["workspace"])], env=environment, capture_output=True, check=True, text=True, timeout=15)
    assert json.loads(process.stdout) == {"sessionId": SESSION, "messages": 2, "calls": []}
    assert (case["root"] / "claude" / case["relative"]).read_bytes() == case["payload"]
    rows = [json.loads(row) for row in case["payload"].splitlines()]
    assert rows[0]["parentUuid"] is None and rows[1]["parentUuid"] == USER
    assert not (case["root"] / "claude/.credentials.json").exists()
