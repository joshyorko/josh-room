"""Synthetic host policy and actual Git observation at native admission boundary."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from josh_room import native_agent_admission as admission
from josh_room.encryption_domain import physical_bucket_identity

pytestmark = pytest.mark.skipif(os.name != "posix", reason="initial native profile contract is Linux-only")


def write(path, document):
    path.write_text(json.dumps(document))
    path.chmod(0o600)


@pytest.fixture
def authorized(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    if not shutil.which("git"):
        pytest.skip("Git unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    subprocess.run(["git", "init", str(workspace)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://example.invalid/synthetic/demo.git"], capture_output=True, check=True)
    references = tmp_path / "references"
    references.mkdir(mode=0o700)
    config_home = tmp_path / "config"
    config_home.mkdir(mode=0o700)
    directory = config_home / "josh-room"
    directory.mkdir(mode=0o700)
    recipients = ["age1qyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqs3290gq", "age1qgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpquuzgag"]
    domain = "11111111-1111-4111-8111-111111111111"
    dimension = {"display_name": "Synthetic", "provider": "minio", "endpoint": "https://minio.example.invalid", "bucket": "synthetic", "credential_profile": "synthetic", "encryption_domain_id": domain}
    write(directory / "config.json", {"dimensions": {"synthetic": dimension}})
    grant = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": str(workspace), "repository": {"host": "example.invalid", "namespace": "synthetic", "name": "demo"}, "scope": "personal", "profile_root": str(tmp_path / "profile"), "local_reference_root": str(references), "encryption_domain_id": domain, "key_generation": 1, "recipients": recipients}
    policy = {"format": admission.FORMAT, "grants": [grant]}
    write(directory / admission.POLICY_FILE, policy)
    keyset = SimpleNamespace(binding=physical_bucket_identity("minio", dimension["endpoint"], dimension["bucket"]), encryption_domain_id=domain, key_generation=1, operational_recipient=recipients[0], recovery_recipients=(recipients[1],))
    monkeypatch.setattr(admission, "_current_keyset", lambda selected: keyset)
    binding = {"dimension": "synthetic", "room": "demo", "profile": "personal", "workspace": str(workspace)}
    return SimpleNamespace(binding=binding, recipients=recipients, config_home=config_home, directory=directory, policy=policy, grant=grant, dimension=dimension, keyset=keyset, references=references, workspace=workspace)


def check(fixture, **changes):
    options = {"profile_root": fixture.grant["profile_root"], "operation": "capture", "destination": fixture.references / "retained.age", "config_home": fixture.config_home}
    options.update(changes)
    return admission.require_admission(fixture.binding, fixture.recipients, **options)


def test_admission_requires_host_grant_and_current_dimension_recipients(authorized):
    result = check(authorized)
    assert len(result.pop("authority_sha256")) == 64
    assert result == {"admitted": True, "scope": "personal", "destination": "local-only", "key_generation": 1, "native_resume_verified": False}
    assert str(authorized.workspace) not in json.dumps(result)
    assert not list(authorized.references.iterdir())


@pytest.mark.parametrize("changed", ["dimension", "room", "profile", "workspace"])
def test_cli_binding_claim_cannot_authorize_another_scope(authorized, changed, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    authorized.binding[changed] = str(other) if changed == "workspace" else "other"
    with pytest.raises(admission.AdmissionError, match="not-authorized"):
        check(authorized)


@pytest.mark.parametrize("change", ["missing", "broad-mode", "symlink", "hardlink", "unknown-field", "duplicate-grant", "workspace-policy"])
def test_missing_or_unsafe_host_authority_is_denied(authorized, change, tmp_path, monkeypatch):
    path = authorized.directory / admission.POLICY_FILE
    if change == "missing":
        path.unlink()
    elif change == "broad-mode":
        path.chmod(0o644)
    elif change == "symlink":
        moved = path.with_name("moved.json")
        path.rename(moved)
        path.symlink_to(moved)
    elif change == "hardlink":
        os.link(path, tmp_path / "linked-policy")
    elif change == "unknown-field":
        authorized.policy["untrusted"] = True
        write(path, authorized.policy)
    elif change == "duplicate-grant":
        authorized.policy["grants"].append(copy.deepcopy(authorized.grant))
        write(path, authorized.policy)
    else:
        monkeypatch.setattr(admission, "policy_config_dir", lambda **kwargs: authorized.workspace)
    with pytest.raises(admission.AdmissionError):
        check(authorized)


@pytest.mark.parametrize("change", ["domain", "generation", "recipient", "physical-bucket"])
def test_rotated_or_other_dimension_keyset_is_denied(authorized, change):
    if change == "domain":
        authorized.keyset.encryption_domain_id = "22222222-2222-4222-8222-222222222222"
    elif change == "generation":
        authorized.keyset.key_generation = 2
    elif change == "recipient":
        authorized.keyset.recovery_recipients = ("age1" + "c" * 58,)
    else:
        authorized.keyset.binding = "other-physical-bucket"
    with pytest.raises(admission.AdmissionError):
        check(authorized)


def test_observed_repository_cannot_be_replaced_by_environment_claims(authorized, monkeypatch):
    other = authorized.workspace.parent / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other))
    assert check(authorized)["admitted"]
    subprocess.run(["git", "-C", str(authorized.workspace), "config", "--local", "remote.origin.url", "https://example.invalid/another/demo.git"], check=True, env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
    with pytest.raises(admission.AdmissionError, match="repository-mismatch"):
        check(authorized)


def test_work_remains_local_and_cannot_select_personal_reference_root(authorized, tmp_path):
    authorized.grant["scope"] = "work"
    authorized.grant["profile"] = "work"
    authorized.binding["profile"] = "work"
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    assert check(authorized)["destination"] == "local-only"
    with pytest.raises(admission.AdmissionError, match="reference-denied"):
        check(authorized, destination=tmp_path / "personal.age")
    with pytest.raises(admission.AdmissionError, match="operation-denied"):
        check(authorized, operation="upload")


def test_private_config_cannot_be_replaced_by_runtime_environment(authorized, monkeypatch, tmp_path):
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", str(tmp_path / "untrusted.json"))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "untrusted"))
    assert check(authorized)["admitted"]
    authorized.dimension["encryption_domain_id"] = "22222222-2222-4222-8222-222222222222"
    write(authorized.directory / "config.json", {"dimensions": {"synthetic": authorized.dimension}})
    with pytest.raises(admission.AdmissionError, match="dimension-mismatch"):
        check(authorized)


def test_provider_read_failure_is_sanitized_and_never_creates_reference(authorized, monkeypatch):
    def failed(selected):
        raise RuntimeError("synthetic sensitive provider detail")
    monkeypatch.setattr(admission, "_current_keyset", failed)
    with pytest.raises(admission.AdmissionError, match="^native-admission-unavailable$"):
        check(authorized)
    assert not list(authorized.references.iterdir())


def test_r2_keyset_read_is_explicitly_unqualified():
    selected = SimpleNamespace(provider="r2")
    with pytest.raises(admission.AdmissionError, match="provider-unqualified"):
        admission._current_keyset(selected)


def test_work_profile_cannot_claim_personal_scope(authorized):
    authorized.grant["profile"] = "work"
    authorized.binding["profile"] = "work"
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    with pytest.raises(admission.AdmissionError, match="scope-mismatch"):
        check(authorized)


def test_oversized_repository_metadata_is_bounded(authorized):
    subprocess.run(["git", "-C", str(authorized.workspace), "config", "--local", "remote.origin.url", "https://example.invalid/" + "a" * 5000], check=True)
    with pytest.raises(admission.AdmissionError, match="repository-unavailable"):
        check(authorized)


def test_unrelated_offline_grant_does_not_block_admitted_workspace(authorized):
    offline = copy.deepcopy(authorized.grant)
    offline.update(room="offline", workspace=str(authorized.workspace.parent / "absent-workspace"), local_reference_root=str(authorized.workspace.parent / "absent-references"))
    authorized.policy["grants"].append(offline)
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    assert check(authorized)["admitted"]


def test_authority_digest_changes_with_selected_host_grant(authorized):
    before = check(authorized)["authority_sha256"]
    alternate = authorized.workspace.parent / "alternate-references"
    alternate.mkdir(mode=0o700)
    authorized.grant["local_reference_root"] = str(alternate)
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    after = check(authorized, destination=alternate / "retained.age")["authority_sha256"]
    assert before != after


def test_admission_client_uses_only_native_secret_store(authorized, monkeypatch):
    import boto3

    from josh_room import keyring
    from josh_room.config import DimensionRegistry
    from josh_room.minio import MinioConfig

    def forbidden(*args, **kwargs):
        pytest.fail("legacy runtime credentials must not authorize native capture")

    fields = []
    def native(profile, field):
        fields.append((profile, field))
        if field == "session-token":
            raise keyring.SecureBackendError("synthetic missing optional session")
        return "synthetic-native-value"

    monkeypatch.setattr(keyring, "lookup", forbidden)
    monkeypatch.setattr(keyring, "secure_lookup", native)
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_PROVIDER_CREDENTIALS", "/synthetic/untrusted.json")
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: (service, kwargs))
    selected = DimensionRegistry({"dimensions": {"synthetic": authorized.dimension}}).select("synthetic")
    service, options = admission._native_client(MinioConfig.from_dimension(selected))
    assert service == "s3"
    assert fields == [("synthetic", field) for field in ("access-key-id", "secret-access-key", "session-token")]
    assert options["verify"] is True
    assert options["aws_session_token"] is None


def test_host_grant_cannot_admit_an_alternate_native_profile_root(authorized, tmp_path):
    with pytest.raises(admission.AdmissionError, match="profile-root-mismatch"):
        check(authorized, profile_root=tmp_path / "unapproved-native-profile")


@pytest.mark.parametrize("scope", ["personal", "work"])
def test_real_core_capture_and_verify_require_host_admission(authorized, monkeypatch, tmp_path, scope):
    from pathlib import Path

    from josh_room import native_agents as agents

    if not shutil.which("age") or not shutil.which("age-keygen"):
        pytest.skip("real age unavailable")
    keys = []
    recipients = []
    for index in range(2):
        key = tmp_path / f"synthetic-{index}.agekey"
        subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, check=True)
        key.chmod(0o600)
        recipients.append(subprocess.run(["age-keygen", "-y", str(key)], capture_output=True, check=True, text=True).stdout.strip())
        keys.append(key)
    authorized.grant.update(recipients=recipients, profile=scope, scope=scope)
    authorized.binding["profile"] = scope
    authorized.keyset.operational_recipient = recipients[0]
    authorized.keyset.recovery_recipients = (recipients[1],)
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(authorized.config_home))
    root = Path(authorized.grant["profile_root"])
    assert agents.configure(root, recipients=recipients, **authorized.binding)["admission"]["admitted"]
    target = root / "codex/sessions/2026/11/22/rollout-demo.jsonl"
    for directory in [root / "codex/sessions", root / "codex/sessions/2026", root / "codex/sessions/2026/11", target.parent]:
        directory.mkdir(mode=0o700)
    write(target, {"type": "session_meta", "payload": {"id": "11111111-1111-4111-8111-111111111111", "cwd": str(authorized.workspace), "cli_version": agents.CODEX_VERSION}})
    reference = authorized.references / "retained.age"
    assert agents.capture(root, output=reference, recipients=recipients, **authorized.binding)["ok"]
    assert agents.verify(root, source=reference, identity_paths=keys[:1], **authorized.binding)["ok"]
    cold = tmp_path / "cold-profile"
    authorized.grant["profile_root"] = str(cold)
    write(authorized.directory / admission.POLICY_FILE, authorized.policy)
    agents.configure(cold, recipients=recipients, **authorized.binding)
    materialized = agents.materialize_session(cold, source=reference, identity_paths=keys[:1], session_id="11111111-1111-4111-8111-111111111111", **authorized.binding)
    assert materialized["native_resume_verified"] is False
    assert (cold / target.relative_to(root)).read_bytes() == target.read_bytes()
    (authorized.directory / admission.POLICY_FILE).unlink()
    with pytest.raises(agents.NativeAgentError, match="admission"):
        agents.capture(root, output=authorized.references / "denied.age", recipients=recipients, **authorized.binding)
    assert not (authorized.references / "denied.age").exists()
