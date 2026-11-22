"""Failure receipts must preserve useful boundaries without fixture secrets."""
import json
import secrets
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import windows_recovery_provider as fixture
from scripts.windows_recovery_provider import public_cli_failure


def test_public_failure_keeps_error_code_but_no_keys_paths_or_payload():
    value = secrets.token_hex(24)
    identity = "AGE" + "-SECRET-KEY-1" + secrets.token_hex(32)
    result = public_cli_failure({
        "error_code": "snapshot-backup-failed",
        "error": f"backup failed {value} {identity} C:\\owned\\private\\snapshot https://example.com/private",
        "text_preview": {"data": "must never survive"},
        "credentials": {"secret-access-key": value},
    }, forbidden=(value,))
    assert result["error_code"] == "snapshot-backup-failed"
    assert "backup failed" in result["error"]
    text = json.dumps(result)
    for excluded in (value, identity, "snapshot https", "example.com", "must never survive", "credentials", "owned"):
        assert excluded not in text


def test_public_failure_drops_invalid_codes_and_bounds_diagnostics():
    assert "error_code" not in public_cli_failure({"error_code": "private/value"})
    assert len(public_cli_failure({"error": "x" * 5000})["error"]) <= 600


def test_ui_diagnostic_uses_native_target_values_only_in_memory(tmp_path, monkeypatch, capsys):
    targets = [["recovery-fixture-synthetic", "access-key-id"],
               ["recovery-fixture-synthetic", "secret-access-key"],
               ["encryption-synthetic", "age-identity"],
               ["room-store-synthetic", "room-store-secret"]]
    ledger = tmp_path / "owned-vault-targets.json"
    ledger.write_text(json.dumps(targets))
    values = {tuple(target): secrets.token_hex(24) for target in targets}
    message = "private native store " + " ".join(values.values())
    monkeypatch.setenv("JOSH_ROOM_ACCEPTANCE_ROOT", str(tmp_path))
    monkeypatch.setattr(fixture.keyring, "secure_lookup", lambda profile, field: values[(profile, field)])
    monkeypatch.setattr(fixture.sys, "stdin", SimpleNamespace(buffer=BytesIO(message.encode())))
    fixture.sanitize_ui_stdin()
    result = json.loads(capsys.readouterr().out)
    assert "private native store" in result["error"]
    assert all(value not in result["error"] for value in values.values())
    assert list(tmp_path.iterdir()) == [ledger]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_candidate_cli_receipt_binds_actual_positive_and_negative_exit(tmp_path, exit_code):
    (tmp_path / "rcc-command-receipts").mkdir()
    runtime = tmp_path / "provider-runtime"
    runtime.mkdir()
    (runtime / "synthetic-marker").write_text("unchanged runtime")
    before = fixture.fingerprint(runtime)
    candidate = {"command": "owned-rcc", "artifact": "sha256:" + "a" * 64}
    receipts = []

    def run(command, **options):
        receipt = Path(command[command.index("--receipt-file") + 1])
        assert receipt.parent == tmp_path / "rcc-command-receipts"
        assert command.index("--receipt-file") < command.index("--inherit-streams") < command.index("--")
        assert options == {"capture_output": True, "timeout": 120, "check": False}
        receipt.write_text(json.dumps({"artifactDigest": candidate["artifact"], "exitCode": exit_code}))
        receipts.append(receipt)
        return SimpleNamespace(returncode=exit_code, stdout=b"{}")

    for _ in range(2):
        result, verified = fixture._run_cli(tmp_path, ["recovery", "inspect"], candidate, run=run)
        assert result.returncode == exit_code
        assert verified == {"artifact": candidate["artifact"], "exit_code": exit_code, "verified": True}
        assert fixture.fingerprint(runtime) == before
    assert receipts[0] != receipts[1]


@pytest.mark.parametrize("kind", ["missing", "malformed", "artifact", "exit", "string-exit", "boolean-exit"])
def test_candidate_cli_refuses_unverified_receipt(tmp_path, kind):
    (tmp_path / "rcc-command-receipts").mkdir()
    candidate = {"command": "owned-rcc", "artifact": "sha256:" + "a" * 64}

    def run(command, **_):
        receipt = Path(command[command.index("--receipt-file") + 1])
        value = {"artifactDigest": candidate["artifact"], "exitCode": 0}
        if kind == "artifact":
            value["artifactDigest"] = "sha256:" + "b" * 64
        elif kind == "exit":
            value["exitCode"] = 1
        elif kind == "string-exit":
            value["exitCode"] = "0"
        elif kind == "boolean-exit":
            value["exitCode"] = False
        if kind != "missing":
            receipt.write_text("{" if kind == "malformed" else json.dumps(value))
        return SimpleNamespace(returncode=0, stdout=b"{}")

    with pytest.raises(RuntimeError, match="execution receipt"):
        fixture._run_cli(tmp_path, [], candidate, run=run)


def test_source_cli_mode_keeps_existing_python_route(tmp_path):
    def run(command, **options):
        assert command == [fixture.sys.executable, "-m", "josh_room", "dimensions", "list", "--json"]
        assert options["check"] is False
        return SimpleNamespace(returncode=0, stdout=b"{}")

    _, verified = fixture._run_cli(tmp_path, ["dimensions", "list"], None, run=run)
    assert verified is None
    assert list(tmp_path.iterdir()) == []
