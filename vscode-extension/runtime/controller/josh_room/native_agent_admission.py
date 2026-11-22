"""Host-owned admission for local native session references, separate from PCC."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from .cancellation import terminate_owned_process
from .config import DimensionRegistry
from .encryption_domain import (
    EncryptionKeyset,
    validate_encryption_domain_id,
    validate_recipient,
)
from .policy import RepositoryIdentity
from .policy_config import policy_config_dir
from .private_paths import verify_private_path

FORMAT = "josh-room.native-agent-admission.v1"
FORMAT_V2 = "josh-room.native-agent-admission.v2"
POLICY_FILE = "native-agent-admission.json"
MAX_POLICY_BYTES = 64 * 1024
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FIELDS = {
    "dimension", "room", "profile", "workspace", "profile_root", "repository", "scope",
    "local_reference_root", "encryption_domain_id", "key_generation", "recipients",
}


class AdmissionError(ValueError):
    """A bounded reason code with no policy, repository, or credential contents."""


def _fail(code):
    raise AdmissionError(code)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("native-admission-invalid-policy")
        result[key] = value
    return result


def _canonical(path, *, exists=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("native-admission-unsafe-path")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            _fail("native-admission-unsafe-path")
    if path.resolve() != path or (exists and not path.is_dir()):
        _fail("native-admission-unsafe-path")
    return path


def _read_private_json(path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            _fail("native-admission-unsafe-policy")
    verify_private_path(path, directory=False)
    before = path.lstat()
    if before.st_nlink != 1 or before.st_size > MAX_POLICY_BYTES:
        _fail("native-admission-unsafe-policy")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            _fail("native-admission-policy-changed")
        data = handle.read(MAX_POLICY_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_POLICY_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        _fail("native-admission-policy-changed")
    return json.loads(data, object_pairs_hook=_unique_object)


def _recipients(values):
    if not isinstance(values, (list, tuple)) or len(values) != 2 or any(not isinstance(value, str) or not re.fullmatch(r"age1[0-9a-z]{58}", value) for value in values):
        _fail("native-admission-recipient-mismatch")
    if len(set(values)) != 2:
        _fail("native-admission-recipient-mismatch")
    return sorted(validate_recipient(value) for value in values)


def _repository(raw):
    if not isinstance(raw, dict) or set(raw) != {"host", "namespace", "name"}:
        _fail("native-admission-invalid-policy")
    return RepositoryIdentity(**raw)


def _grant(raw, version):
    expected = _FIELDS | ({"tools"} if version == FORMAT_V2 else set())
    if not isinstance(raw, dict) or set(raw) != expected:
        _fail("native-admission-invalid-policy")
    if version == FORMAT_V2:
        tools = raw["tools"]
        if not isinstance(tools, list) or not tools or any(not isinstance(value, str) or value not in {"codex", "claude"} for value in tools) or len(set(tools)) != len(tools):
            _fail("native-admission-invalid-tools")
    for name in ("dimension", "room", "profile"):
        if not isinstance(raw[name], str) or not _LABEL.fullmatch(raw[name]):
            _fail("native-admission-invalid-policy")
    if raw["scope"] not in {"personal", "work", "oss", "homelab"}:
        _fail("native-admission-invalid-policy")
    if raw["profile"] in {"personal", "work", "oss", "homelab"} and raw["scope"] != raw["profile"]:
        _fail("native-admission-scope-mismatch")
    if type(raw["key_generation"]) is not int or raw["key_generation"] < 1:
        _fail("native-admission-invalid-policy")
    workspace = _canonical(raw["workspace"], exists=False)
    source = _canonical(raw["profile_root"], exists=False)
    if source.is_relative_to(workspace) or workspace.is_relative_to(source):
        _fail("native-admission-unsafe-profile-root")
    references = _canonical(raw["local_reference_root"], exists=False)
    if references.is_relative_to(source) or source.is_relative_to(references):
        _fail("native-admission-unsafe-reference-root")
    if references.is_relative_to(workspace) or workspace.is_relative_to(references):
        _fail("native-admission-unsafe-reference-root")
    _repository(raw["repository"])
    validate_encryption_domain_id(raw["encryption_domain_id"])
    _recipients(raw["recipients"])
    return raw


def _git_output(workspace, *arguments):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    process = subprocess.Popen(
        ["git", "--no-optional-locks", "-C", str(workspace), *arguments],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    content = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    _fail("native-admission-repository-unavailable")
                chunk = os.read(process.stdout.fileno(), 4097 - len(content))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > 4096:
                    _fail("native-admission-repository-unavailable")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())):
            _fail("native-admission-repository-unavailable")
        return content.decode().strip()
    finally:
        if process.poll() is None:
            terminate_owned_process(process)
        process.stdout.close()


def _observed_repository(workspace):
    top = _git_output(workspace, "rev-parse", "--show-toplevel")
    if top != str(workspace):
        _fail("native-admission-repository-mismatch")
    remote = _git_output(workspace, "config", "--local", "--get", "remote.origin.url")
    return RepositoryIdentity.from_remote(remote)


def _native_client(config):
    import boto3
    from botocore.config import Config

    from .keyring import SecureBackendError, secure_lookup

    credentials = {field: secure_lookup(config.credential_profile, field) for field in ("access-key-id", "secret-access-key")}
    try:
        credentials["session-token"] = secure_lookup(config.credential_profile, "session-token")
    except SecureBackendError:
        pass
    return boto3.client(
        "s3", endpoint_url=config.endpoint, region_name=config.region,
        aws_access_key_id=credentials["access-key-id"],
        aws_secret_access_key=credentials["secret-access-key"],
        aws_session_token=credentials.get("session-token"),
        verify=config.ca_bundle if config.ca_bundle else config.verify_tls,
        config=Config(connect_timeout=config.timeout_seconds, read_timeout=config.timeout_seconds,
                      retries={"max_attempts": config.max_attempts, "mode": "standard"},
                      s3={"addressing_style": "path" if config.path_style else "virtual"}),
    )


def _current_keyset(dimension):
    from .auth import _read_keyset_record_from_backend

    if dimension.provider == "minio":
        from .minio import MinioBackend, MinioConfig
        config = MinioConfig.from_dimension(dimension)
        backend = MinioBackend(config, client=_native_client(config))
    elif dimension.provider == "s3":
        from .s3_backend import S3Backend, S3Config
        config = S3Config.from_dimension(dimension)
        backend = S3Backend(config, client=_native_client(config))
    else:
        _fail("native-admission-provider-unqualified")
    keyset, _etag = _read_keyset_record_from_backend(dimension, backend)
    if not isinstance(keyset, EncryptionKeyset):
        _fail("native-admission-keyset-unavailable")
    return keyset


def require_admission(binding, recipients, *, profile_root, operation, destination=None, config_home=None, tool="codex"):
    """Revalidate a host grant and enrolled keyset before reading native content.

    ``destination`` names the encrypted local output for capture, or source for
    verify. This function never enrolls a bucket, writes policy, or uploads data.
    Plan/status must not invoke it because keyset verification is a provider read.
    """
    try:
        if tool not in {"codex", "claude"}:
            _fail("native-admission-tool-unqualified")
        if operation not in {"configure", "launch", "capture", "verify", "restore", "materialize-session"}:
            _fail("native-admission-operation-denied")
        if not isinstance(binding, dict) or set(binding) != {"dimension", "room", "profile", "workspace"}:
            _fail("native-admission-binding-mismatch")
        workspace = _canonical(binding["workspace"])
        directory = policy_config_dir(config_home=config_home)
        if directory.is_relative_to(workspace):
            _fail("native-admission-unsafe-policy")
        verify_private_path(directory, directory=True)
        policy = _read_private_json(directory / POLICY_FILE)
        if not isinstance(policy, dict) or set(policy) != {"format", "grants"} or policy["format"] not in {FORMAT, FORMAT_V2} or not isinstance(policy["grants"], list) or not 1 <= len(policy["grants"]) <= 64:
            _fail("native-admission-invalid-policy")
        grants = [_grant(raw, policy["format"]) for raw in policy["grants"]]
        identities = [(grant["dimension"], grant["room"], grant["profile"], grant["workspace"]) for grant in grants]
        if len(identities) != len(set(identities)):
            _fail("native-admission-ambiguous-policy")
        matches = [grant for grant in grants if all(grant[name] == binding[name] for name in ("dimension", "room", "profile")) and grant["workspace"] == str(workspace)]
        if len(matches) != 1:
            _fail("native-admission-not-authorized")
        grant = matches[0]
        tools = grant["tools"] if policy["format"] == FORMAT_V2 else ["codex"]
        if tool not in tools:
            _fail("native-admission-tool-not-authorized")
        source = _canonical(profile_root, exists=False)
        if str(source) != grant["profile_root"]:
            _fail("native-admission-profile-root-mismatch")
        if directory.is_relative_to(source):
            _fail("native-admission-unsafe-policy")
        references = _canonical(grant["local_reference_root"])
        verify_private_path(references, directory=True)
        if _observed_repository(workspace) != _repository(grant["repository"]):
            _fail("native-admission-repository-mismatch")
        selected = DimensionRegistry(_read_private_json(directory / "config.json")).select(binding["dimension"])
        if selected.auth_state == "disconnected" or selected.encryption_domain_id != grant["encryption_domain_id"]:
            _fail("native-admission-dimension-mismatch")
        if _recipients(recipients) != _recipients(grant["recipients"]):
            _fail("native-admission-recipient-mismatch")
        if operation in {"capture", "verify", "restore", "materialize-session"}:
            if destination is None:
                _fail("native-admission-reference-required")
            reference = _canonical(destination, exists=False)
            if reference == Path(grant["local_reference_root"]) or not reference.is_relative_to(Path(grant["local_reference_root"])):
                _fail("native-admission-reference-denied")
            verify_private_path(reference.parent, directory=True)
        keyset = _current_keyset(selected)
        if keyset.binding != selected_binding(selected) or keyset.encryption_domain_id != grant["encryption_domain_id"] or keyset.key_generation != grant["key_generation"]:
            _fail("native-admission-keyset-mismatch")
        if _recipients([keyset.operational_recipient, *keyset.recovery_recipients]) != _recipients(grant["recipients"]):
            _fail("native-admission-recipient-mismatch")
        authority = {"format": policy["format"], "tool": tool, "grant": grant, "physical_binding": keyset.binding, "key_generation": keyset.key_generation}
        digest = hashlib.sha256(json.dumps(authority, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"admitted": True, "tool": tool, "scope": grant["scope"], "destination": "local-only", "key_generation": keyset.key_generation, "authority_sha256": digest, "native_resume_verified": False}
    except AdmissionError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError, BotoCoreError, ClientError):
        _fail("native-admission-unavailable")


def selected_binding(dimension):
    from .encryption_domain import physical_bucket_identity

    return physical_bucket_identity(dimension.provider, dimension.endpoint, dimension.bucket)
