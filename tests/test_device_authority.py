import json
from pathlib import Path

import pytest

from josh_room import device, keyring
from josh_room.keyring import BackendStatus


def test_backend_allowlist_and_session_diagnostics(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""
        stderr = "No such secret"

    monkeypatch.setattr(keyring.shutil, "which", lambda name: "/usr/bin/" + name)
    available = keyring.backend_status(
        platform_name="linux",
        environ={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/session"},
        runner=lambda *_args, **_kwargs: Result(),
    )
    assert available.backend == "linux-secret-service"
    assert available.available is True
    assert keyring.backend_status(platform_name="linux", backend="plaintext").reason == "unknown-backend"
    missing = keyring.backend_status(platform_name="linux", environ={}, runner=lambda *_args, **_kwargs: Result())
    assert missing.available is False
    assert missing.reason == "session-bus-missing"


def test_receipt_names_are_length_delimited():
    assert device._receipt_profile("a", "b.c") != device._receipt_profile("a.b", "c")

def test_real_native_authority_gate():
    import pytest

    status = keyring.backend_status()
    if not status.available:
        pytest.skip(f"SKIPPED: native secure authority unavailable ({status.reason})")
    assert status.backend in keyring.SECURE_BACKENDS


def test_device_enrollment_rotation_converges_and_removes_all_local_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    store = {}
    monkeypatch.setattr(device, "backend_status", lambda: BackendStatus("linux-secret-service", "linux", True, False, "available"))
    monkeypatch.setattr(device, "validate_recipient", lambda value, _label="recipient": value)
    monkeypatch.setattr(device, "secure_store", lambda profile, field, value: store.__setitem__((profile, field), value))
    monkeypatch.setattr(device, "secure_lookup", lambda profile, field: store[(profile, field)])
    monkeypatch.setattr(device, "secure_delete", lambda profile, field: store.pop((profile, field), None))

    first = device.enroll(
        profile="work",
        credential_profile="r2-old",
        recipients=["old-daily", "old-recovery"],
        credentials={"access-key-id": "access", "secret-access-key": "secret"},
        age_profile="age-work",
        recovery_profile="recovery",
        recovery_identity="recovery-secret",
    )
    converged = device.enroll(profile="work", credential_profile="r2-old", recipients=["old-daily", "old-recovery"])
    rotated = device.rotate_new_writes(profile="work", credential_profile="r2-new", recipients=["new-daily", "new-recovery"])

    assert first["changed"] is True
    assert converged["converged"] is True
    assert rotated["recipient_set_version"] == 2
    assert [item["version"] for item in device.read_recipient_sets(profile="work")] == [1, 2]
    assert "secret" not in json.dumps(first)
    device.remove_local()
    assert store == {}
    assert not Path(tmp_path, "device.json").exists()
def test_selected_profile_and_namespace_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    device._write_state({
        "schema_version": 1,
        "device_id": "device-test",
        "profiles": {
            "one": {
                "credential_profile": "cred-one",
                "age_profile": "age-one",
                "active_recipient_set_version": 1,
                "recipient_sets": {"1": {"recipients": ["one-a", "one-b"], "version": 1}},
            },
            "two": {
                "credential_profile": "cred-two",
                "age_profile": "age-two",
                "active_recipient_set_version": 1,
                "recipient_sets": {"1": {"recipients": ["two-a", "two-b"], "version": 1}},
            },
        },
    })
    with pytest.raises(device.DeviceError, match="explicit selection"):
        device.active_recipients()
    assert device.active_recipients(profile="two") == ["two-a", "two-b"]
    assert device.active_age_profile(profile="two") == "age-two"
    assert device.active_credential_profile(profile="two") == "cred-two"
    assert keyring._secret_attributes("cred-two", "access-key-id")[1] == "josh-room"
    assert keyring._secret_attributes("receipt-test", "receipt")[1] == "josh-room-device"


def test_doctor_failed_checks_have_remediation(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(device, "backend_status", lambda: BackendStatus(None, "linux", False, False, "session-bus-missing"))
    report = device.doctor()
    assert report["ok"] is False
    assert all("remediation" in check for check in report["checks"])
