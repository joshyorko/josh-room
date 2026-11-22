"""Opt-in real owned-server import; synthetic native data, never provider turns."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from josh_room import native_agents as agents
from josh_room.claude_native_history import project_key

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="Linux owned-server isolation"
    ),
]
SESSION = "11111111-1111-4111-8111-111111111111"


def api():
    return importlib.import_module("josh_room.native_agent_t3_import")


def write_private(path, payload):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(0o600)


def _cold_provider(tmp_path, monkeypatch, tool):
    executable = os.environ.get("JOSH_ROOM_TEST_T3")
    if not executable or not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("explicit installed T3 and real age required")
    tmp_path.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    binding = {
        "dimension": "synthetic",
        "room": "demo",
        "profile": "personal",
        "workspace": workspace,
    }
    authority = {"value": "a" * 64, "calls": []}

    def admitted(*args, **kwargs):
        authority["calls"].append(kwargs.get("operation"))
        return {"admitted": True, "authority_sha256": authority["value"]}

    monkeypatch.setattr(agents, "_admit", admitted)
    recipients, keys = [], []
    for index in range(2):
        key = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
        key.chmod(0o600)
        recipients.append(
            subprocess.run(
                ["age-keygen", "-y", str(key)],
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
        )
        keys.append(key)
    source, cold = tmp_path / "source", tmp_path / "cold"
    agents.configure(source, recipients=recipients, tool=tool, **binding)
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
                    "message": "synthetic question",
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
                "message": {"role": "user", "content": "synthetic question"},
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
                    "content": [{"type": "text", "text": "synthetic retained answer"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    write_private(source / tool / relative, payload)
    for parent in (source / tool / relative).parents:
        if parent.is_relative_to(source):
            parent.chmod(0o700)
    reference = tmp_path / "retained.age"
    agents.capture(
        source, output=reference, recipients=recipients, tool=tool, **binding
    )
    agents.configure(cold, recipients=recipients, tool=tool, **binding)
    agents.materialize_session(
        cold,
        source=reference,
        identity_paths=keys[:1],
        session_id=SESSION,
        tool=tool,
        **binding,
    )
    authority["calls"].clear()
    return {
        "root": cold,
        "binding": binding,
        "tool": tool,
        "relative": relative,
        "payload": payload,
        "executable": executable,
        "authority": authority,
        "admitted": admitted,
    }


@pytest.fixture(params=["codex", "claude"])
def cold_provider(tmp_path, monkeypatch, request):
    return _cold_provider(tmp_path, monkeypatch, request.param)


@pytest.fixture
def cold_codex(tmp_path, monkeypatch):
    return _cold_provider(tmp_path, monkeypatch, "codex")


def invoke(case):
    return api().import_history(
        case["root"],
        executable=case["executable"],
        tool=case["tool"],
        **case["binding"],
    )


def db(case):
    return sqlite3.connect(
        f"file:{case['root']}/t3/userdata/statev2.sqlite?mode=ro", uri=True
    )


def require_isolated():
    if {name for _index, name in socket.if_nameindex()} != {"lo"}:
        pytest.skip("run positive acceptance inside unshare -Urn with loopback enabled")


def assert_inert(connection):
    for table in [
        "orchestration_v2_effect_outbox",
        "orchestration_v2_projection_runs",
        "orchestration_v2_projection_run_attempts",
        "orchestration_v2_projection_runtime_requests",
        "orchestration_v2_projection_provider_turns",
        "orchestration_v2_thread_launch_workflows",
        "scheduled_tasks",
    ]:
        assert (
            connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
        ), table


def test_owned_api_imports_cold_native_history(cold_provider, monkeypatch, tmp_path):
    require_isolated()
    case = cold_provider
    marker = tmp_path / "untrusted-node-module-ran"
    preload = tmp_path / "untrusted-preload.cjs"
    preload.write_text(
        "require('node:fs').writeFileSync("
        + json.dumps(str(marker))
        + ", 'unexpected');throw Error('untrusted preload ran');"
    )
    module = tmp_path / "untrusted-node-modules/undici"
    module.mkdir(parents=True)
    (module / "index.js").write_text(preload.read_text())
    monkeypatch.setenv("NODE_OPTIONS", "--require=" + str(preload))
    monkeypatch.setenv("NODE_PATH", str(module.parent))
    result = invoke(case)
    driver = "codex" if case["tool"] == "codex" else "claudeAgent"
    derived = f"import:{driver}:{SESSION}"
    assert result["state"] == "history-imported"
    assert result["imported_count"] == 1
    assert result["sessions"] == [
        {"provider_session_id": SESSION, "new_t3_thread_id": derived}
    ]
    assert result["original_t3_identity_restored"] is False
    assert result["native_resume_verified"] is False
    assert result["cwd_matches"] is True
    assert result["runnable_effects"] == 0
    assert not marker.exists(), "untrusted ambient Node code executed"
    assert (case["root"] / case["tool"] / case["relative"]).read_bytes() == case[
        "payload"
    ]
    with db(case) as connection:
        assert_inert(connection)
        assert (
            connection.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 0
        )
        runtime = connection.execute(
            "SELECT thread_id,status,resume_cursor_json,runtime_payload_json FROM provider_session_runtime"
        ).fetchall()
        assert len(runtime) == 1 and runtime[0][:2] == (derived, "stopped")
        cursor, payload = json.loads(runtime[0][2]), json.loads(runtime[0][3])
        assert payload["cwd"] == str(case["binding"]["workspace"])
        assert cursor == (
            {"threadId": SESSION}
            if driver == "codex"
            else {"threadId": derived, "resume": SESSION}
        )
        native = json.loads(
            connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_provider_threads"
            ).fetchone()[0]
        )
        assert native["nativeThreadRef"] == {
            "driver": driver,
            "nativeId": SESSION,
            "strength": "strong",
        }
        assert native["status"] == "idle" and native["pendingBackgroundTasks"] == []
        messages = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_messages ORDER BY message_id"
            )
        ]
        assert [(row["role"], row["text"]) for row in messages] == [
            ("user", "synthetic question"),
            ("assistant", "synthetic retained answer"),
        ]
        app = json.loads(
            connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_threads"
            ).fetchone()[0]
        )
        assert app["id"] == derived and app["historyOrigin"] == "v1_import"
    assert not (case["root"] / "codex/auth.json").exists()
    assert not (case["root"] / "claude/.credentials.json").exists()
    with pytest.raises(
        agents.NativeAgentError, match="t3-import-fresh-profile-required"
    ):
        invoke(case)


@pytest.mark.parametrize(
    "unsafe",
    [
        "existing-t3",
        "auth-sibling",
        "config-sibling",
        "auth-selected",
        "config-selected",
        "busy",
    ],
)
def test_owned_api_refuses_unsafe_profiles(cold_provider, unsafe):
    require_isolated()
    case = cold_provider
    if unsafe == "existing-t3":
        write_private(case["root"] / "t3/existing", b"synthetic local state")
        code = "t3-import-fresh-profile-required"
    elif unsafe.startswith(("auth-", "config-")):
        sibling = (
            ("claude" if case["tool"] == "codex" else "codex")
            if unsafe.endswith("sibling")
            else case["tool"]
        )
        filename = (
            (".credentials.json" if sibling == "claude" else "auth.json")
            if unsafe.startswith("auth-")
            else ("settings.json" if sibling == "claude" else "config.toml")
        )
        write_private(case["root"] / sibling / filename, b"synthetic excluded state")
        code = "t3-import-provider-home-not-clean"
    else:
        with (
            agents._lock(case["root"]),
            pytest.raises(agents.NativeAgentError, match="profile-busy"),
        ):
            invoke(case)
        assert not list((case["root"] / "t3").iterdir())
        return
    with pytest.raises(agents.NativeAgentError, match=code):
        invoke(case)
    assert not (case["root"] / "t3/userdata/statev2.sqlite").exists()


def test_owned_api_refuses_unisolated_host(cold_provider):
    if {name for _index, name in socket.if_nameindex()} == {"lo"}:
        pytest.skip(
            "host namespace refusal is exercised outside the isolated acceptance run"
        )
    with pytest.raises(
        agents.NativeAgentError, match="t3-import-network-isolation-required"
    ):
        invoke(cold_provider)
    assert not list((cold_provider["root"] / "t3").iterdir())


def test_owned_api_rechecks_authority_and_stops_before_runnable_work(
    cold_provider, monkeypatch
):
    require_isolated()
    case = cold_provider
    checks = []

    def changed_authority(*args, **kwargs):
        checks.append(kwargs.get("operation"))
        result = case["admitted"](*args, **kwargs)
        if len(checks) > 1:
            result["authority_sha256"] = "b" * 64
        return result

    monkeypatch.setattr(agents, "_admit", changed_authority)
    with pytest.raises(agents.NativeAgentError, match="native-admission-changed"):
        invoke(case)
    assert len(checks) >= 2
    path = case["root"] / "t3/userdata/statev2.sqlite"
    if path.exists():
        with db(case) as connection:
            assert_inert(connection)
    assert (case["root"] / case["tool"] / case["relative"]).read_bytes() == case[
        "payload"
    ]


@pytest.mark.parametrize(
    "fault", ["cancel-after-import", "revoke-cleanup-failure", "server-start-failure"]
)
def test_owned_api_tracks_import_outcome_through_faults(cold_codex, monkeypatch, fault):
    from josh_room.cancellation import CLICancelled

    require_isolated()
    case = cold_codex
    module = api()
    imported, revoked, processes = [], [], []
    original_rpc, original_run = module._rpc, module._run
    original_start = agents._start_server

    def rpc(*args, **kwargs):
        result = original_rpc(*args, **kwargs)
        if args[5] == "agentSessions.import":
            imported.append(result)
            if fault == "cancel-after-import":
                raise CLICancelled()
        return result

    def run(arguments, *args, **kwargs):
        result = original_run(arguments, *args, **kwargs)
        if arguments[1:4] == ["auth", "session", "revoke"]:
            revoked.append(True)
            if fault == "revoke-cleanup-failure":
                raise agents.NativeAgentError("synthetic-revoke-cleanup-failure")
        return result

    def start(*args, **kwargs):
        if fault == "server-start-failure":
            raise agents.NativeAgentError("synthetic-start-failure")
        process = original_start(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module, "_rpc", rpc)
    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(agents, "_start_server", start)
    code = (
        "synthetic-start-failure"
        if fault == "server-start-failure"
        else "t3-import-outcome-uncertain"
    )
    with pytest.raises(agents.NativeAgentError, match="^" + code + "$") as caught:
        invoke(case)
    assert caught.value.committed is (fault != "server-start-failure")
    assert revoked == [True]
    assert all(process.poll() is not None for process in processes)
    assert bool(imported) is (fault != "server-start-failure")
    with db(case) as connection:
        assert_inert(connection)
        assert (
            connection.execute(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute(
            "SELECT count(*) FROM orchestration_v2_projection_threads"
        ).fetchone()[0] == (0 if fault == "server-start-failure" else 1)
    assert (case["root"] / "codex" / case["relative"]).read_bytes() == case["payload"]


def test_owned_api_refuses_untrusted_node_before_execution(
    cold_codex, monkeypatch, tmp_path
):
    require_isolated()
    case = cold_codex
    directory = tmp_path / "untrusted-bin"
    directory.mkdir(mode=0o700)
    marker = tmp_path / "untrusted-node-executed"
    executable = directory / "node"
    executable.write_text(
        "#!/bin/sh\nprintf unexpected > " + str(marker) + "\nexit 99\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(directory))
    with pytest.raises(
        agents.NativeAgentError, match="^t3-import-rpc-client-unqualified$"
    ) as caught:
        invoke(case)
    assert caught.value.committed is False
    assert not marker.exists()
    assert not list((case["root"] / "t3").iterdir())


@pytest.mark.parametrize("fault", ["cancel-after-issue", "invalid-issue-json"])
def test_owned_api_revokes_persisted_session_when_issue_output_is_lost(
    cold_codex, monkeypatch, fault
):
    from josh_room.cancellation import CLICancelled

    require_isolated()
    case = cold_codex
    module = api()
    original_run = module._run
    issued, revoked = [], []

    def run(arguments, *args, **kwargs):
        result = original_run(arguments, *args, **kwargs)
        if arguments[1:4] == ["auth", "session", "issue"]:
            issued.append(True)
            if fault == "cancel-after-issue":
                raise CLICancelled()
            return "synthetic malformed issue JSON"
        if arguments[1:4] == ["auth", "session", "revoke"]:
            revoked.append(True)
        return result

    monkeypatch.setattr(module, "_run", run)
    error = CLICancelled if fault == "cancel-after-issue" else agents.NativeAgentError
    with pytest.raises(error) as caught:
        invoke(case)
    if isinstance(caught.value, agents.NativeAgentError):
        assert caught.value.committed is False
    assert issued == [True] and revoked == [True]
    with db(case) as connection:
        assert_inert(connection)
        assert (
            connection.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM orchestration_v2_projection_threads"
            ).fetchone()[0]
            == 0
        )


def test_host_job_imports_via_private_admission_bridge(cold_provider):
    if {name for _index, name in socket.if_nameindex()} == {"lo"}:
        pytest.skip("host job acceptance requires the ordinary host namespace")
    case = cold_provider
    job = importlib.import_module("josh_room.native_agent_import_job")
    result = job.import_history(
        case["root"],
        executable=case["executable"],
        tool=case["tool"],
        **case["binding"],
    )
    driver = "codex" if case["tool"] == "codex" else "claudeAgent"
    derived = f"import:{driver}:{SESSION}"
    assert result["state"] == "history-imported" and result["imported_count"] == 1
    assert result["sessions"] == [
        {"provider_session_id": SESSION, "new_t3_thread_id": derived}
    ]
    assert result["cwd_matches"] is True
    assert result["runnable_effects"] == 0
    assert result["original_t3_identity_restored"] is False
    assert result["native_resume_verified"] is False
    assert len(case["authority"]["calls"]) >= 2
    assert set(case["authority"]["calls"]) == {"import-history"}
    with db(case) as connection:
        assert_inert(connection)
        assert (
            connection.execute("SELECT count(*) FROM auth_sessions").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 0
        )
        rows = connection.execute(
            "SELECT thread_id,status,resume_cursor_json,runtime_payload_json FROM provider_session_runtime"
        ).fetchall()
        assert len(rows) == 1 and rows[0][:2] == (derived, "stopped")
        cursor, runtime = json.loads(rows[0][2]), json.loads(rows[0][3])
        assert cursor == (
            {"threadId": SESSION}
            if driver == "codex"
            else {"threadId": derived, "resume": SESSION}
        )
        assert runtime["cwd"] == str(case["binding"]["workspace"])
        native = json.loads(
            connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_provider_threads"
            ).fetchone()[0]
        )
        assert native["nativeThreadRef"] == {
            "driver": driver,
            "nativeId": SESSION,
            "strength": "strong",
        }
        assert native["status"] == "idle" and native["pendingBackgroundTasks"] == []
        messages = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_messages ORDER BY message_id"
            )
        ]
        assert [(row["role"], row["text"]) for row in messages] == [
            ("user", "synthetic question"),
            ("assistant", "synthetic retained answer"),
        ]
        app = json.loads(
            connection.execute(
                "SELECT payload_json FROM orchestration_v2_projection_threads"
            ).fetchone()[0]
        )
        assert app["id"] == derived and app["historyOrigin"] == "v1_import"
    assert (case["root"] / case["tool"] / case["relative"]).read_bytes() == case[
        "payload"
    ]
    assert not (case["root"] / "codex/auth.json").exists()
    assert not (case["root"] / "claude/.credentials.json").exists()


def test_host_job_worker_sigkill_does_not_orphan_t3(cold_codex, monkeypatch):
    if {name for _index, name in socket.if_nameindex()} == {"lo"}:
        pytest.skip("host job acceptance requires the ordinary host namespace")
    case = cold_codex
    job = importlib.import_module("josh_room.native_agent_import_job")
    start, send = job._start_worker, job._send
    workers, observed, owned_pidfds = [], [], []

    def record_worker(*args, **kwargs):
        process = start(*args, **kwargs)
        workers.append(process)
        return process

    def kill_before_import_ack(stream, message, deadline):
        if message != {"ok": True}:
            return send(stream, message, deadline)
        assert len(workers) == 1
        worker = workers[0]
        children = set()
        for task in (Path("/proc") / str(worker.pid) / "task").iterdir():
            children.update(
                int(value) for value in (task / "children").read_text().split()
            )
        expected = Path(case["executable"]).stat()
        servers = []
        for child in children:
            try:
                actual = (Path("/proc") / str(child) / "exe").stat()
            except FileNotFoundError:
                continue
            if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                servers.append(child)
        assert len(servers) == 1, "expected one actual pinned T3 direct child"
        server = servers[0]
        owned_pidfds.append(os.pidfd_open(server))
        os.kill(worker.pid, signal.SIGKILL)
        until = time.monotonic() + 5
        while True:
            try:
                raw = (Path("/proc") / str(server) / "stat").read_text()
                state = raw[raw.rfind(")") + 2 :].split()[0]
            except FileNotFoundError:
                state = "gone"
            if state in {"gone", "Z", "X"}:
                break
            assert time.monotonic() < until, (
                "T3 still running after isolated worker SIGKILL"
            )
            time.sleep(0.025)
        # This executes before the parent wrapper's cleanup can terminate descendants.
        with agents._lock(case["root"]):
            pass
        observed.append(state)
        return None

    monkeypatch.setattr(job, "_start_worker", record_worker)
    monkeypatch.setattr(job, "_send", kill_before_import_ack)
    try:
        with pytest.raises(
            agents.NativeAgentError, match="^t3-import-outcome-uncertain$"
        ) as caught:
            job.import_history(
                case["root"],
                executable=case["executable"],
                tool="codex",
                **case["binding"],
            )
    finally:
        for descriptor in owned_pidfds:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)
    assert caught.value.committed is True
    assert len(observed) == 1 and observed[0] in {"gone", "Z", "X"}
    assert len(workers) == 1 and workers[0].poll() is not None
    with agents._lock(case["root"]):
        pass
    path = case["root"] / "t3/userdata/statev2.sqlite"
    if path.exists():
        with db(case) as connection:
            assert_inert(connection)
    assert (case["root"] / "codex" / case["relative"]).read_bytes() == case["payload"]
