import io
import json
import signal
import subprocess

import pytest

from scripts.room_store_probe import (
    ProbeError,
    _interrupt_backup,
    parse_json_events,
    parse_s3_config,
    run_probe,
    summarize_events,
)


def test_restic_json_summary_is_extracted_without_path_fields():
    events = parse_json_events(
        '\n'.join(
            [
                json.dumps({"message_type": "status", "files_done": 2, "total_files": 3}),
                json.dumps(
                    {
                        "message_type": "summary",
                        "snapshot_id": "0123456789abcdef",
                        "files_new": 1,
                        "files_changed": 1,
                        "files_unmodified": 1,
                        "data_added": 4096,
                        "data_added_packed": 2048,
                        "total_bytes_processed": 8192,
                        "total_duration": 0.25,
                        "errors": 0,
                    }
                ),
            ]
        )
    )

    assert summarize_events(events) == {
        "summary_count": 1,
        "snapshot_id": "0123456789abcdef",
        "files_new": 1,
        "files_changed": 1,
        "files_unmodified": 1,
        "data_added": 4096,
        "data_added_packed": 2048,
        "total_bytes_processed": 8192,
        "errors": 0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"message_type": "summary", "errors": 1}),
        json.dumps({"message_type": "status", "files_done": 1}),
    ],
)
def test_restic_json_stream_fails_closed_on_malformed_or_missing_summary(payload):
    with pytest.raises(ProbeError):
        events = parse_json_events(payload)
        summarize_events(events)


class FakeProcess:
    def __init__(self, stdout, exit_code=None):
        self.stdout = io.StringIO(stdout)
        self.exit_code = exit_code
        self.signal = None
        self.killed = False

    def poll(self):
        return self.exit_code

    def send_signal(self, sig):
        self.signal = sig
        self.exit_code = 130

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.killed = True
        self.exit_code = -9


def test_cancel_probe_records_early_summary_without_signaling(tmp_path, monkeypatch):
    process = FakeProcess(
        json.dumps({"message_type": "summary", "snapshot_id": "synthetic"}) + "\n",
        exit_code=0,
    )
    monkeypatch.setattr("scripts.room_store_probe.subprocess.Popen", lambda *a, **k: process)

    result = _interrupt_backup("restic", {}, tmp_path)

    assert result == {
        "signal_sent": False,
        "exit_code": 0,
        "observed_message_type": "summary",
    }
    assert process.signal is None
    assert process.stdout.closed
    assert not process.killed


def test_cancel_probe_records_early_process_exit_without_signaling(tmp_path, monkeypatch):
    process = FakeProcess("", exit_code=0)
    monkeypatch.setattr("scripts.room_store_probe.subprocess.Popen", lambda *a, **k: process)

    result = _interrupt_backup("restic", {}, tmp_path)

    assert result["signal_sent"] is False
    assert result["exit_code"] == 0
    assert result["observed_message_type"] is None
    assert process.stdout.closed
    assert not process.killed


def test_cancel_probe_signals_active_backup_and_closes_process_stream(tmp_path, monkeypatch):
    process = FakeProcess(
        json.dumps({"message_type": "verbose_status", "current_files": 1}) + "\n"
    )
    observed = {}

    def popen(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return process

    monkeypatch.setattr("scripts.room_store_probe.subprocess.Popen", popen)

    result = _interrupt_backup("restic", {}, tmp_path)

    assert result == {
        "signal_sent": True,
        "exit_code": 130,
        "observed_message_type": "verbose_status",
    }
    assert "--verbose" in observed["args"]
    assert process.signal == signal.SIGINT
    assert process.stdout.closed
    assert not process.killed


def test_windows_cancel_uses_process_group_and_ctrl_break(tmp_path, monkeypatch):
    process = FakeProcess(
        json.dumps({"message_type": "verbose_status", "current_files": 1}) + "\n"
    )
    observed = {}
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 19, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)

    def popen(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return process

    monkeypatch.setattr("scripts.room_store_probe.subprocess.Popen", popen)

    result = _interrupt_backup("restic", {}, tmp_path, platform="nt")

    assert result["signal_sent"] is True
    assert process.signal == 19
    assert observed["kwargs"]["creationflags"] == 0x200
    assert "start_new_session" not in observed["kwargs"]
    assert process.stdout.closed


def test_windows_cancel_falls_back_to_shared_tree_terminator(tmp_path, monkeypatch):
    process = FakeProcess(
        json.dumps({"message_type": "verbose_status", "current_files": 1}) + "\n"
    )
    process.send_signal = lambda _signal: (_ for _ in ()).throw(OSError("synthetic failure"))
    terminations = []
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 19, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)

    def terminate(child, platform):
        terminations.append((child, platform))
        child.exit_code = -9

    monkeypatch.setattr(
        "scripts.room_store_probe.subprocess.Popen", lambda *a, **k: process
    )

    result = _interrupt_backup(
        "restic", {}, tmp_path, platform="nt", terminate_process=terminate
    )

    assert result["signal_sent"] is True
    assert result["exit_code"] == -9
    assert terminations == [(process, "nt")]
    assert process.stdout.closed


def test_remote_s3_config_keeps_credentials_out_of_its_representation():
    access_key = "synthetic-access-key"
    secret_key = "synthetic-secret-key"
    config = parse_s3_config(
        json.dumps(
            {
                "provider": "minio",
                "repository": "s3:https://storage.example.test/bucket/room-store-phase0",
                "access_key_id": access_key,
                "secret_access_key": secret_key,
                "session_token": "synthetic-session-token",
            }
        )
    )

    env = config.environment("synthetic-run")
    assert env["AWS_ACCESS_KEY_ID"] == access_key
    assert env["AWS_SECRET_ACCESS_KEY"] == secret_key
    assert env["RESTIC_REPOSITORY"].endswith("/room-store-phase0/probe-synthetic-run")
    assert access_key not in repr(config)
    assert secret_key not in repr(config)
    assert "synthetic-session-token" not in repr(config)


@pytest.mark.parametrize(
    "repository",
    [
        "s3:http://storage.example.test/bucket/room-store-phase0",
        "s3:https://storage.example.test/bucket/other-prefix",
        "s3:https://user:secret@storage.example.test/bucket/room-store-phase0",
        "s3:https://storage.example.test/bucket/room-store-phase0?secret=value",
    ],
)
def test_remote_s3_config_requires_https_existing_bucket_and_fixed_prefix(repository):
    with pytest.raises(ProbeError):
        parse_s3_config(
            json.dumps(
                {
                    "provider": "r2",
                    "repository": repository,
                    "access_key_id": "synthetic-access-key",
                    "secret_access_key": "synthetic-secret-key",
                }
            )
        )


def test_failed_probe_persists_completed_and_failed_stage_evidence(tmp_path, monkeypatch):
    evidence_file = tmp_path / "phase0.json"
    monkeypatch.setattr("scripts.room_store_probe._version", lambda _binary: "0.0.0")

    with pytest.raises(ProbeError, match="expected restic 0.19.1"):
        run_probe("synthetic-restic", evidence_file=evidence_file)

    evidence = json.loads(evidence_file.read_text())
    assert evidence["status"] == "failed"
    assert evidence["failed_stage"] == "version_pin"
    assert [stage["status"] for stage in evidence["stages"]] == ["passed", "failed"]
    assert evidence_file.stat().st_mode & 0o777 == 0o600
