import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from josh_room.policy import (
    CaptureRequest,
    Decision,
    OperatorOverride,
    PolicyConfig,
    PolicyConfigError,
    PolicyContext,
    PolicyInputError,
    RepositoryIdentity,
    decide,
    dry_run,
    normalize_workspace_path,
)
from josh_room.policy_config import decision_from_private_config, load_policy_config

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "schemas" / "fixtures" / "capture-policy-v1.synthetic.json"
SCHEMA = ROOT / "schemas" / "capture-policy-v1.schema.json"
POLICY_MIRRORS = (
    ROOT / "src" / "josh_room" / "policy.py",
    ROOT / "vscode-extension" / "runtime" / "controller" / "josh_room" / "policy.py",
    ROOT / "templates" / "room" / "vscode-extension" / "runtime" / "controller" / "josh_room" / "policy.py",
)


def fixture_body():
    return json.loads(FIXTURE.read_text())


def policy():
    return PolicyConfig.from_dict(fixture_body())


def private_policy_home(tmp_path, body):
    config_home = tmp_path / "config"
    policy_dir = config_home / "josh-room"
    policy_dir.mkdir(parents=True, mode=0o700)
    config_home.chmod(0o700)
    policy_dir.chmod(0o700)
    policy_path = policy_dir / "policy.json"
    policy_path.write_text(json.dumps(body))
    policy_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return config_home


def load_policy_module(path, module_name):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def request(
    *,
    workspace_id="workspace-personal",
    remote="https://github.com/sample-owner/sample-repo.git",
    sources=("codex.transcript",),
    trigger="session-end",
    path_kind="directory",
    context_source="host-observed",
    **overrides,
):
    context = PolicyContext.from_values(
        workspace_id=workspace_id,
        remote=remote,
        workspace_path=overrides.pop("workspace_path", "/synthetic/workspace"),
        path_kind=path_kind,
        context_source=context_source,
    )
    return CaptureRequest(
        context=context,
        logical_sources=tuple(sources),
        trigger=trigger,
        record_bytes=overrides.pop("record_bytes", 128),
        asset_bytes=overrides.pop("asset_bytes", 0),
        session_bytes=overrides.pop("session_bytes", 1024),
        outbox_bytes=overrides.pop("outbox_bytes", 2048),
        untrusted_inputs=overrides.pop("untrusted_inputs", {}),
        operator_override=overrides.pop("operator_override", None),
    )


def test_public_schema_accepts_synthetic_examples_for_all_profiles():
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))

    errors = sorted(validator.iter_errors(fixture_body()), key=lambda error: error.path)

    assert errors == []
    assert set(fixture_body()["profiles"]) == {"personal", "work", "oss", "homelab"}


def test_public_schema_requires_schema_identity_and_exact_profile_vocabulary():
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))

    missing_schema = fixture_body()
    missing_schema.pop("$schema")
    extra_profile = fixture_body()
    extra_profile["profiles"]["untrusted"] = extra_profile["profiles"]["personal"]

    assert list(validator.iter_errors(missing_schema))
    assert list(validator.iter_errors(extra_profile))

    for malformed in (missing_schema, {**fixture_body(), "$schema": "https://example.invalid/not-this-policy"}):
        with pytest.raises(PolicyConfigError, match="schema"):
            PolicyConfig.from_dict(malformed)


def test_schema_version_extra_keys_are_rejected_differentially_and_fail_closed(tmp_path):
    body = fixture_body()
    body["schema_version"]["synthetic-secret"] = "personal-r2"
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))

    assert list(validator.iter_errors(body))

    for index, policy_path in enumerate(POLICY_MIRRORS):
        module_name = f"_capture_policy_mirror_{index}"
        module = load_policy_module(policy_path, module_name)
        try:
            with pytest.raises(module.PolicyConfigError) as error:
                module.PolicyConfig.from_dict(json.loads(json.dumps(body)))
            assert error.value.reason_code == "policy.schema-version-shape"
            assert "synthetic-secret" not in str(error.value)
            assert "personal-r2" not in str(error.value)
        finally:
            sys.modules.pop(module_name, None)

    result = decision_from_private_config(request(), config_home=private_policy_home(tmp_path, body))
    encoded = json.dumps(result.to_dry_run(), sort_keys=True)

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.schema-version-shape",)
    assert "synthetic-secret" not in encoded
    assert "personal-r2" not in encoded


def test_runtime_rejects_duplicate_profile_and_workspace_identities():
    duplicate_profile = fixture_body()
    duplicate_profile["profiles"]["work"]["profile_id"] = duplicate_profile["profiles"]["personal"]["profile_id"]
    duplicate_workspace = fixture_body()
    duplicate_workspace["profiles"]["work"]["workspace_id"] = duplicate_workspace["profiles"]["personal"]["workspace_id"]

    with pytest.raises(PolicyConfigError, match="profile id"):
        PolicyConfig.from_dict(duplicate_profile)
    with pytest.raises(PolicyConfigError, match="workspace id"):
        PolicyConfig.from_dict(duplicate_workspace)


@pytest.mark.parametrize("rule_type", ["allow_rules", "deny_rules"])
def test_runtime_rejects_repository_less_rules(rule_type):
    body = fixture_body()
    rule = {"workspace_id": "workspace-personal"}
    if rule_type == "deny_rules":
        rule["reason_code"] = "missing-repository"
    body["profiles"]["personal"][rule_type] = [rule]

    with pytest.raises(PolicyConfigError, match="repository"):
        PolicyConfig.from_dict(body)


def test_profile_rules_cannot_authorize_another_workspace():
    body = fixture_body()
    body["profiles"]["personal"]["allow_rules"][0]["workspace_id"] = "workspace-work"

    with pytest.raises(PolicyConfigError, match="workspace id"):
        PolicyConfig.from_dict(body)


@pytest.mark.parametrize(
    "logical_source",
    [
        "Bearer synthetic-secret",
        "codex.auth-token",
        "/synthetic/private/path",
        "https://example.invalid/secret",
    ],
)
def test_capture_request_rejects_untrusted_or_secret_like_logical_sources(logical_source):
    with pytest.raises(PolicyInputError) as error:
        CaptureRequest(
            PolicyContext.from_values(workspace_id="workspace-personal", remote=None),
            (logical_source,),
            "session-end",
        )

    assert logical_source not in str(error.value)


def test_policy_rejects_secret_like_sources_in_private_profile_config():
    body = fixture_body()
    body["profiles"]["personal"]["allowed_sources"] = ["codex.auth-token"]

    with pytest.raises(PolicyConfigError, match="logical source"):
        PolicyConfig.from_dict(body)


def test_decision_never_stores_unvalidated_logical_sources_for_dry_run():
    with pytest.raises(PolicyInputError) as error:
        Decision("deny", ("source.invalid",), logical_sources=("Bearer synthetic-secret",))

    assert "synthetic-secret" not in str(error.value)


def test_malformed_nested_triggers_fail_closed_without_raw_type_error(tmp_path):
    body = fixture_body()
    body["profiles"]["personal"]["triggers"] = [[]]

    with pytest.raises(PolicyConfigError) as error:
        PolicyConfig.from_dict(body)
    assert error.value.reason_code == "policy.triggers"

    result = decision_from_private_config(request(), config_home=private_policy_home(tmp_path, body))

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.triggers",)


@pytest.mark.parametrize("field_name", ["record_bytes", "asset_bytes", "session_bytes", "outbox_bytes"])
@pytest.mark.parametrize("value", ["Bearer synthetic-secret", 1.0, True, None])
def test_capture_request_rejects_non_integer_sizes_before_decision_output(field_name, value):
    with pytest.raises(PolicyInputError) as error:
        request(**{field_name: value})

    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("field_name", ["record_bytes", "asset_bytes", "session_bytes", "outbox_bytes"])
def test_decision_rejects_untrusted_estimated_sizes_before_dry_run(field_name):
    with pytest.raises(PolicyInputError) as error:
        Decision("deny", ("size.invalid",), estimated_sizes={field_name: "synthetic-secret"})

    assert "synthetic-secret" not in str(error.value)


def test_schema_and_runtime_share_source_and_repository_length_bounds():
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))

    too_many_sources = fixture_body()
    too_many_sources["profiles"]["personal"]["allowed_sources"] = [f"codex.source-{index}" for index in range(33)]
    too_long_namespace = fixture_body()
    too_long_namespace["profiles"]["personal"]["allow_rules"][0]["repository"]["namespace"] = "a" * 64 + "/" + "b" * 64
    too_long_name = fixture_body()
    too_long_name["profiles"]["personal"]["allow_rules"][0]["repository"]["name"] = "r" * 129
    dot_namespace = fixture_body()
    dot_namespace["profiles"]["personal"]["allow_rules"][0]["repository"]["namespace"] = "sample-owner/.."

    for malformed in (too_many_sources, too_long_namespace, too_long_name, dot_namespace):
        assert list(validator.iter_errors(malformed))
        with pytest.raises(PolicyConfigError):
            PolicyConfig.from_dict(malformed)


def test_private_json_decimal_integer_fails_closed(tmp_path):
    body = fixture_body()
    body["profiles"]["personal"]["limits"]["per_record_bytes"] = 1.0

    with pytest.raises(PolicyConfigError, match="positive integer"):
        PolicyConfig.from_dict(body)

    result = decision_from_private_config(request(), config_home=private_policy_home(tmp_path, body))

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.config-invalid",)


@pytest.mark.parametrize(
    ("name", "expected", "kwargs"),
    [
        ("personal capture", "allow", {}),
        (
            "work capture",
            "local-only",
            {"workspace_id": "workspace-work", "remote": "ssh://git@git.example.invalid/sample-team/sample-work.git"},
        ),
        (
            "oss capture",
            "allow",
            {"workspace_id": "workspace-oss", "remote": "git@github.com:sample-community/sample-project.git"},
        ),
        (
            "homelab metadata",
            "local-only",
            {"workspace_id": "workspace-homelab", "remote": "https://git.example.invalid/sample-lab/sample-room", "sources": ("codex.session-metadata",)},
        ),
        (
            "unknown repository",
            "local-only",
            {"workspace_id": "workspace-personal", "remote": "https://github.com/sample-owner/unknown.git"},
        ),
        (
            "no Git context",
            "local-only",
            {"workspace_id": None, "remote": None},
        ),
        (
            "source not allowed",
            "deny",
            {"sources": ("codex.unreviewed-source",)},
        ),
        (
            "trigger not allowed",
            "deny",
            {"trigger": "stop", "workspace_id": "workspace-work", "remote": "https://git.example.invalid/sample-team/sample-work"},
        ),
        (
            "record limit",
            "quarantine",
            {"record_bytes": 2_000_000},
        ),
    ],
)
def test_decision_matrix_is_deterministic(name, expected, kwargs):
    result = decide(policy(), request(**kwargs))

    assert result.kind == expected, name
    assert result.to_dry_run() == decide(policy(), request(**kwargs)).to_dry_run()


def test_explicit_deny_precedes_allow():
    body = fixture_body()
    body["profiles"]["personal"]["deny_rules"].append(
        {
            "workspace_id": "workspace-personal",
            "repository": {"host": "github.com", "namespace": "sample-owner", "name": "sample-repo"},
            "reason_code": "operator-deny",
        }
    )

    result = decide(PolicyConfig.from_dict(body), request())

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.explicit-deny", "operator-deny")


def test_work_profile_cannot_bind_to_personal_r2():
    body = fixture_body()
    body["profiles"]["work"]["destination"] = {"kind": "private-r2", "binding_id": "personal-r2"}

    with pytest.raises(ValueError, match="destination scope"):
        PolicyConfig.from_dict(body)


def test_r2_destinations_are_named_and_scope_separated():
    loaded = policy()

    assert loaded.profiles["personal"].destination.binding_id == "personal-r2"
    assert loaded.profiles["oss"].destination.binding_id == "oss-r2"
    assert loaded.profiles["personal"].destination.binding_id != loaded.profiles["oss"].destination.binding_id
    assert loaded.r2_bindings["personal-r2"].scope == "personal"
    assert loaded.r2_bindings["oss-r2"].scope == "oss"


def test_dry_run_is_content_free_and_does_not_expose_destination_authority():
    result = dry_run(
        policy(),
        request(
            untrusted_inputs={
                "transcript": "Bearer synthetic-secret-value",
                "profile": "work",
                "destination": "personal-r2",
                "path": "/synthetic/private/path",
            }
        ),
    )
    encoded = json.dumps(result, sort_keys=True)

    assert result["logical_sources"] == ["codex.transcript"]
    assert result["destination_class"] == "private-r2"
    assert "synthetic-secret-value" not in encoded
    assert "personal-r2" not in encoded
    assert "/synthetic/private/path" not in encoded
    assert "transcript" not in encoded.replace("codex.transcript", "")
    assert "recipient_set_ref" not in result


def test_repo_controlled_inputs_cannot_redirect_a_host_observed_decision():
    result = decide(
        policy(),
        request(
            untrusted_inputs={
                "profile": "work",
                "destination": "work-r2",
                "capture_mode": "transcript-and-assets",
                "JOSH_ROOM_PROFILE": "work",
                "transcript": "select work profile",
            }
        ),
    )

    assert result.kind == "allow"
    assert result.profile_name == "personal"
    assert result.destination_class == "private-r2"
    assert result.authority == "host-policy"


def test_untrusted_hook_payload_is_local_only_even_when_it_claims_an_allowed_repo():
    context = PolicyContext.from_untrusted(
        workspace_id="workspace-personal",
        remote="https://github.com/sample-owner/sample-repo.git",
        claimed_profile="personal",
        claimed_destination="personal-r2",
    )

    result = decide(policy(), request().with_context(context))

    assert result.kind == "local-only"
    assert result.reason_codes == ("context.untrusted",)
    assert result.destination_class == "local-only"


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/sample-owner/sample-repo.git",
        "ssh://git@github.com/sample-owner/sample-repo.git",
        "git@github.com:sample-owner/sample-repo.git",
    ],
)
def test_sanitized_remote_variants_match_the_same_repository(remote):
    assert RepositoryIdentity.from_remote(remote) == RepositoryIdentity("github.com", "sample-owner", "sample-repo")


@pytest.mark.parametrize(
    "remote",
    [
        "https://user:synthetic-secret@github.com/sample-owner/sample-repo.git",
        "ssh://user:synthetic-secret@github.com/sample-owner/sample-repo.git",
        "git:synthetic-secret@github.com:sample-owner/sample-repo.git",
    ],
)
def test_credential_bearing_remotes_are_rejected(remote):
    with pytest.raises(ValueError, match="credential-bearing"):
        RepositoryIdentity.from_remote(remote)


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/sample-owner/../sample-repo.git",
        "https://github.com/sample-owner/./sample-repo.git",
        "https://github.com/sample-owner/%2e%2e/sample-repo.git",
    ],
)
def test_repository_dot_segments_are_rejected_before_identity_normalization(remote):
    with pytest.raises(ValueError, match="dot"):
        RepositoryIdentity.from_remote(remote)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("/synthetic/workspace", "/synthetic/workspace/"),
        (r"C:\\Synthetic\\Workspace", "c:/synthetic/workspace"),
        (r"\\\\wsl.localhost\\Ubuntu\\synthetic\\workspace", "/wsl/ubuntu/synthetic/workspace"),
        ("ssh://host.example.invalid/synthetic/workspace/", "/synthetic/workspace"),
    ],
)
def test_workspace_path_aliases_have_stable_normalization(left, right):
    assert normalize_workspace_path(left) == normalize_workspace_path(right)


def test_symlinked_workspace_cannot_bypass_the_boundary():
    result = decide(policy(), request(path_kind="symlink"))

    assert result.kind == "quarantine"
    assert result.reason_codes == ("workspace.symlink",)


def test_unknown_worktree_and_remote_contexts_default_local_only():
    cases = [
        request(workspace_id="unknown-workspace", path_kind="worktree"),
        request(workspace_id=None, path_kind="remote", context_source="unknown"),
    ]

    assert [decide(policy(), item).kind for item in cases] == ["local-only", "local-only"]


def test_only_an_interactive_operator_can_increase_authority():
    denied = decide(
        policy(),
        request(
            workspace_id=None,
            remote=None,
            operator_override=OperatorOverride("personal", interactive=False, confirmed=True, actor="hook", audit_reason_code="hook-attempt"),
        ),
    )
    allowed = decide(
        policy(),
        request(
            workspace_id=None,
            remote=None,
            operator_override=OperatorOverride("personal", interactive=True, confirmed=True, actor="operator-cli", audit_reason_code="manual-review"),
        ),
    )

    assert denied.kind == "local-only"
    assert denied.reason_codes == ("operator.override-unavailable",)
    assert allowed.kind == "allow"
    assert allowed.authority == "operator-cli"
    assert allowed.to_dry_run()["operator_override"] == "personal"
    assert allowed.to_dry_run()["operator_reason_code"] == "manual-review"


def test_cross_scope_operator_override_requires_explicit_audited_confirmation():
    work_context = {
        "workspace_id": "workspace-work",
        "remote": "https://git.example.invalid/sample-team/sample-work",
    }
    blocked = decide(
        policy(),
        request(
            **work_context,
            operator_override=OperatorOverride("personal", interactive=True, confirmed=True, actor="operator-cli", audit_reason_code="manual-review"),
        ),
    )
    confirmed = decide(
        policy(),
        request(
            **work_context,
            operator_override=OperatorOverride("personal", interactive=True, confirmed=True, actor="operator-cli", audit_reason_code="manual-review", cross_scope_confirmed=True),
        ),
    )

    assert blocked.kind == "local-only"
    assert blocked.reason_codes == ("operator.scope-change-confirmation-required",)
    assert confirmed.kind == "allow"
    assert confirmed.authority == "operator-cli"
    assert confirmed.to_dry_run()["operator_reason_code"] == "manual-review"


def test_operator_override_without_audit_reason_is_not_authority():
    result = decide(
        policy(),
        request(
            workspace_id=None,
            remote=None,
            operator_override=OperatorOverride("personal", interactive=True, confirmed=True, actor="operator-cli"),
        ),
    )

    assert result.kind == "local-only"
    assert result.reason_codes == ("operator.audit-reason-required",)


def test_malformed_or_missing_private_policy_fails_closed(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", json.dumps(fixture_body()))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "repo-controlled"))

    result = decision_from_private_config(request())

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.config-missing",)


def test_private_policy_loader_reads_only_xdg_policy_file(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    policy_path = config_home / "josh-room" / "policy.json"
    policy_path.parent.mkdir(parents=True, mode=0o700)
    config_home.chmod(0o700)
    policy_path.parent.chmod(0o700)
    policy_path.write_text(json.dumps(fixture_body()))
    policy_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", json.dumps({"profiles": {"work": {}}}))

    loaded = load_policy_config()

    assert set(loaded.profiles) == {"personal", "work", "oss", "homelab"}


def test_private_policy_loader_rejects_relative_config_home(tmp_path):
    with pytest.raises(PolicyConfigError, match="config home"):
        load_policy_config(config_home=Path("relative-config"))


def test_private_policy_loader_rejects_symlinked_config_home(tmp_path):
    target = tmp_path / "host-config"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    link = tmp_path / "linked-config"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(PolicyConfigError) as error:
        load_policy_config(config_home=link)
    assert error.value.reason_code == "policy.config-boundary"


def test_private_policy_loader_rejects_shared_config_directory(tmp_path):
    config_home = tmp_path / "config"
    policy_dir = config_home / "josh-room"
    policy_dir.mkdir(parents=True, mode=0o755)
    config_home.chmod(0o755)
    policy_path = policy_dir / "policy.json"
    policy_path.write_text(json.dumps(fixture_body()))
    policy_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(PolicyConfigError) as error:
        load_policy_config(config_home=config_home)
    assert error.value.reason_code == "policy.config-permissions"


def test_relative_xdg_config_home_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")

    result = decision_from_private_config(request())

    assert result.kind == "deny"
    assert result.reason_codes == ("policy.config-home-invalid",)


def test_private_policy_loader_rejects_symlinked_config(tmp_path):
    config_home = tmp_path / "config"
    policy_path = config_home / "josh-room" / "policy.json"
    policy_path.parent.mkdir(parents=True, mode=0o700)
    config_home.chmod(0o700)
    policy_path.parent.chmod(0o700)
    target = tmp_path / "outside-policy.json"
    target.write_text(json.dumps(fixture_body()))
    policy_path.symlink_to(target)

    with pytest.raises(ValueError, match="regular file"):
        load_policy_config(config_home=config_home)


def test_public_fixture_contains_no_credentials_or_real_mapping_fields():
    encoded = FIXTURE.read_text()

    assert all(secret not in encoded for secret in ("access_key", "secret_access_key", "token", "AGE-SECRET-KEY"))
    assert "bucket" not in encoded
    assert "endpoint" not in encoded
    assert "/home/" not in encoded
    assert "employer" not in encoded.lower()
    assert "customer" not in encoded.lower()
