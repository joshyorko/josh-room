"""Failure receipts must preserve useful boundaries without fixture secrets."""
import json
import secrets

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
