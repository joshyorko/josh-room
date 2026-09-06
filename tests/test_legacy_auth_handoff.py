"""Explicit legacy R2 grants provide source identity custody only."""

import os
import shutil
import stat
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from synthetic_identity import synthetic_identity

from josh_room import auth


@pytest.fixture
def legacy_source(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("JOSH_ROOM_RESULT_FILE", raising=False)
    runtime = auth._runtime_root()
    runtime.mkdir(parents=True, mode=0o700)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    paths = [*auth._runtime_paths(), runtime / "r2-logout.json", config / "config.json"]
    for path in paths:
        path.write_text("synthetic existing " + path.name)
        path.chmod(0o600)
    environment = {
        "JOSH_ROOM_RUNTIME_CONFIG": str(paths[2]),
        "JOSH_ROOM_RUNTIME_CREDENTIALS": str(paths[0]),
        "JOSH_ROOM_IDENTITY": str(paths[1]),
        "JOSH_ROOM_RUNTIME_PROFILE": "synthetic-existing-profile",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    def snapshot():
        return {
            path: (path.read_bytes(), path.stat().st_mode, path.stat().st_ino, path.stat().st_mtime_ns)
            for root in (runtime, config) for path in root.rglob("*") if path.is_file()
        }

    before = snapshot()

    def forbidden(*_args, **_kwargs):
        pytest.fail("source custody must not touch runtime, config, keyring, or destination keysets")

    for name in (
        "_write_runtime", "_clear_runtime_session", "_set_runtime_environment", "config_dir",
        "lookup_encryption_identity", "store_encryption_identity", "resolve_encryption_material",
        "ensure_minio_domain",
    ):
        monkeypatch.setattr(auth, name, forbidden)
    identity = synthetic_identity("explicit-legacy-r2-source")
    session = {
        "status": "authorized",
        "accessKeyId": "synthetic-access",
        "secretAccessKey": "synthetic-secret",
        "sessionToken": "synthetic-token",
        "endpoint": "https://r2.example.invalid",
        "bucket": "synthetic-bucket",
        "ageIdentity": f"# created: synthetic\n\n{identity}\n# public key: synthetic\n",
        "ageRecipients": ["synthetic-daily", "synthetic-recovery"],
    }
    monkeypatch.setattr(auth, "_request", lambda *_args, **_kwargs: session)
    handoff = tmp_path / "private" / "source.identity"
    handoff.parent.mkdir(mode=0o700)
    try:
        yield SimpleNamespace(session=session, handoff=handoff, identity=identity)
        assert snapshot() == before
        assert {name: os.environ.get(name) for name in environment} == environment
    finally:
        handoff.unlink(missing_ok=True)


@pytest.mark.parametrize("operation", [auth.poll_oauth_session, auth.wait_oauth_session])
@pytest.mark.parametrize(("purpose", "with_handoff"), [
    (None, False), ("encryption", False), ("r2", False),
    (None, True), ("encryption", True),
])
def test_legacy_r2_opt_in_rejects_invalid_combinations_before_request(
    operation, purpose, with_handoff, legacy_source, monkeypatch,
):
    monkeypatch.setattr(auth, "_request", lambda *_args, **_kwargs: pytest.fail("invalid mode requested broker"))
    with pytest.raises(ValueError, match="legacy R2 source requires"):
        operation("synthetic", purpose=purpose,
                  legacy_source_handoff=legacy_source.handoff if with_handoff else None,
                  legacy_r2_source=True)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize("purpose", [None, "r2"])
def test_handoff_without_opt_in_rejects_r2_purpose_before_request(purpose, legacy_source, monkeypatch):
    monkeypatch.setattr(auth, "_request", lambda *_args, **_kwargs: pytest.fail("implicit R2 mode requested broker"))
    with pytest.raises(ValueError, match="requires encryption purpose"):
        auth.poll_oauth_session("synthetic", purpose=purpose, legacy_source_handoff=legacy_source.handoff)


@pytest.mark.parametrize("declared", [{}, {"purpose": "r2", "capabilities": ["encryption", "r2"]}])
def test_default_encryption_handoff_never_accepts_a_legacy_r2_grant(declared, legacy_source):
    legacy_source.session.update(declared)
    with pytest.raises(RuntimeError, match="encryption-only"):
        auth.wait_oauth_session("synthetic", purpose="encryption", legacy_source_handoff=legacy_source.handoff)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize("declared", [
    {}, {"purpose": "r2"}, {"capabilities": ["encryption", "r2"]},
    {"purpose": "r2", "capabilities": ["encryption", "r2"]},
    {"purpose": "r2", "capabilities": ["r2", "encryption"]},
])
@pytest.mark.parametrize("operation", [auth.poll_oauth_session, auth.wait_oauth_session])
def test_explicit_legacy_r2_grant_writes_only_normalized_source_identity(declared, operation, legacy_source):
    legacy_source.session.update(declared)
    result = operation("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff, legacy_r2_source=True)
    assert result == {"status": "authorized", "purpose": "r2"}
    assert legacy_source.handoff.read_text() == legacy_source.identity + "\n"
    assert stat.S_IMODE(legacy_source.handoff.stat().st_mode) == 0o600
    assert list(legacy_source.handoff.parent.iterdir()) == [legacy_source.handoff]


@pytest.mark.parametrize("declared", [
    {"purpose": "encryption"}, {"purpose": None}, {"purpose": "synthetic-private-purpose"},
    {"capabilities": None}, {"capabilities": []}, {"capabilities": "encryption,r2"},
    {"capabilities": ["encryption"]}, {"capabilities": ["r2"]},
    {"capabilities": ["encryption", "r2", "synthetic-private-capability"]},
    {"capabilities": ["encryption", "r2", "r2"]}, {"capabilities": ["encryption", {}]},
])
def test_explicit_legacy_r2_rejects_contradictory_declarations(declared, legacy_source):
    legacy_source.session.update(declared)
    with pytest.raises(RuntimeError, match="legacy R2 source.*contract") as error:
        auth.poll_oauth_session("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff, legacy_r2_source=True)
    assert "synthetic-private" not in str(error.value)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize("field", ["accessKeyId", "secretAccessKey", "sessionToken", "endpoint", "bucket"])
@pytest.mark.parametrize("invalid", ["absent", None, "", 23, []])
def test_explicit_legacy_r2_requires_every_complete_storage_field(field, invalid, legacy_source):
    if invalid == "absent":
        del legacy_source.session[field]
    else:
        legacy_source.session[field] = invalid
    with pytest.raises(RuntimeError, match="complete R2 storage material"):
        auth.poll_oauth_session("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff, legacy_r2_source=True)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize(("field", "invalid", "category"), [
    ("ageIdentity", None, "identity missing"), ("ageIdentity", {}, "identity type"),
    ("ageIdentity", "synthetic-invalid", "identity format"),
    ("ageIdentity", "#" * (16 * 1024 + 1), "identity format"),
    ("ageIdentity", synthetic_identity("one") + "\n" + synthetic_identity("two"), "identity multiple"),
    ("ageRecipients", "synthetic-invalid", "recipient type"),
    ("ageRecipients", ["synthetic", {}], "recipient type"),
    ("ageRecipients", ["synthetic", "synthetic"], "recipient count"),
    ("ageRecipients", ["synthetic", ""], "recipient value"),
], ids=["missing", "type", "format", "oversized", "multiple", "recipient-type", "item-type", "count", "value"])
def test_explicit_legacy_r2_keeps_encryption_material_validation(field, invalid, category, legacy_source):
    legacy_source.session[field] = invalid
    with pytest.raises((TypeError, ValueError), match=category) as error:
        auth.poll_oauth_session("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff, legacy_r2_source=True)
    assert "synthetic-invalid" not in str(error.value)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize("outcome", ["denied", "canceled", "timeout", "interrupted", "write-failure", "interrupted-write"])
def test_explicit_legacy_r2_failure_leaves_no_handoff_and_preserves_runtime(outcome, legacy_source, monkeypatch):
    if outcome in {"denied", "canceled"}:
        legacy_source.session["status"] = outcome
    if outcome == "timeout":
        legacy_source.session["status"] = "pending"
        clock = [0.0]
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(auth.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    if outcome in {"interrupted", "write-failure", "interrupted-write"}:
        def fail(*_args, **_kwargs):
            if outcome == "write-failure":
                raise OSError("synthetic write failure")
            raise KeyboardInterrupt
        if outcome == "interrupted":
            monkeypatch.setattr(auth, "_request", fail)
        else:
            monkeypatch.setattr(auth.os, "fsync", fail)
    with pytest.raises((RuntimeError, KeyboardInterrupt)):
        auth.wait_oauth_session("synthetic", timeout=1, purpose="r2",
                               legacy_source_handoff=legacy_source.handoff, legacy_r2_source=True)
    assert not legacy_source.handoff.exists()


@pytest.mark.parametrize("http_status", [None, 404, 503])
def test_pending_legacy_cancel_preserves_runtime_on_every_broker_outcome(http_status, legacy_source, monkeypatch):
    legacy_source.session["status"] = "pending"
    assert auth.poll_oauth_session("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff,
                                  legacy_r2_source=True) == {"status": "pending"}

    def cancel(path, method):
        assert path == "/session/synthetic/cancel" and method == "POST"
        if http_status:
            raise HTTPError("https://auth.example.invalid", http_status, "synthetic", {}, None)
        return {"status": "canceled"}

    monkeypatch.setattr(auth, "_request", cancel)
    if http_status == 503:
        with pytest.raises(HTTPError):
            auth.cancel_oauth_session("synthetic", preserve_runtime_session=True)
    else:
        assert auth.cancel_oauth_session("synthetic", preserve_runtime_session=True)["status"] == "canceled"
    assert not legacy_source.handoff.exists()


def test_explicit_legacy_r2_real_age_commented_key_keeps_same_recipient(legacy_source, tmp_path):
    executable = shutil.which("age-keygen")
    if executable is None:
        pytest.skip("age-keygen unavailable")
    generated = []
    recipients = []
    for name in ("daily", "recovery"):
        path = tmp_path / f"{name}.identity"
        subprocess.run([executable, "-o", str(path)], check=True, capture_output=True)
        generated.append(path.read_text())
        recipients.append(subprocess.run([executable, "-y", str(path)], check=True, capture_output=True, text=True).stdout.strip())
        path.unlink()
    legacy_source.session.update(ageIdentity=generated[0], ageRecipients=recipients)
    assert auth.wait_oauth_session("synthetic", purpose="r2", legacy_source_handoff=legacy_source.handoff,
                                  legacy_r2_source=True) == {"status": "authorized", "purpose": "r2"}
    assert legacy_source.handoff.read_text() == next(line for line in generated[0].splitlines() if line.startswith("AGE-SECRET-KEY-")) + "\n"
    derived = subprocess.run([executable, "-y", str(legacy_source.handoff)], check=True, capture_output=True, text=True)
    assert derived.stdout.strip() == recipients[0]
