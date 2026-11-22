"""Failure receipts must preserve useful boundaries without fixture secrets."""
import json
import secrets
from io import BytesIO
from types import SimpleNamespace

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
