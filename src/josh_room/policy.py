"""Host-owned capture policy and transport-neutral decision primitives."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

PROFILE_NAMES = frozenset({"personal", "work", "oss", "homelab"})
POLICY_SCHEMA_URI = "https://josh-room.invalid/schemas/capture-policy-v1.schema.json"
CAPTURE_MODES = frozenset({"metadata-only", "transcript", "transcript-and-assets"})
TRIGGERS = frozenset({"manual", "session-end", "stop", "subagent-stop", "reconcile"})
PATH_KINDS = frozenset({"directory", "worktree", "remote", "wsl", "symlink", "unknown"})
IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}$")
PART_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SOURCE_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
REASON_RE = re.compile(r"^[a-z][a-z0-9.-]{2,63}$")
SECRET_SOURCE_MARKERS = frozenset({"access", "auth", "bearer", "cookie", "credential", "identity", "key", "password", "secret", "token"})
DOT_SEGMENTS = frozenset({".", ".."})
LOGICAL_SOURCE_MAX_ITEMS = 32
LOGICAL_SOURCE_MAX_LENGTH = 96
REPOSITORY_NAMESPACE_MAX_LENGTH = 128
REPOSITORY_NAME_MAX_LENGTH = 128
CAPTURE_SIZE_FIELDS = ("record_bytes", "asset_bytes", "session_bytes", "outbox_bytes")


class PolicyConfigError(ValueError):
    """A private policy failed boundary validation."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


class PolicyInputError(ValueError):
    """An untrusted policy input was rejected before decision output."""

    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise PolicyConfigError("policy.invalid-identifier", f"{field_name} is not a valid identifier")
    return value


def _string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        raise PolicyConfigError("policy.invalid-string", f"{field_name} is not a valid string")
    return value


def _positive_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise PolicyConfigError("policy.invalid-limit", f"{field_name} must be a positive integer")
    return value


def _validate_capture_size(value: object) -> int:
    if type(value) is not int:
        raise PolicyInputError("size.invalid", "capture sizes must be integers")
    return value


def sanitize_logical_sources(values: object, *, allow_empty: bool = False) -> tuple[str, ...]:
    """Accept only bounded, credential-free logical source names."""

    if not isinstance(values, (list, tuple)):
        raise PolicyInputError("source.invalid", "logical sources must be a sequence")
    sources = tuple(values)
    if (not sources and not allow_empty) or len(sources) > LOGICAL_SOURCE_MAX_ITEMS or not all(isinstance(source, str) for source in sources):
        raise PolicyInputError("source.invalid", "logical sources are invalid")
    if len(set(sources)) != len(sources):
        raise PolicyInputError("source.invalid", "logical sources are invalid")
    for source in sources:
        if not isinstance(source, str) or len(source) > LOGICAL_SOURCE_MAX_LENGTH or not SOURCE_RE.fullmatch(source) or not source.startswith("codex."):
            raise PolicyInputError("source.invalid", "logical source is invalid")
        segments = source.removeprefix("codex.").split(".")
        if any(any(marker in segment for marker in SECRET_SOURCE_MARKERS) for segment in segments):
            raise PolicyInputError("source.secret-like", "logical source is not permitted")
    return sources


@dataclass(frozen=True)
class RepositoryIdentity:
    """Credential-free repository identity used for policy matching."""

    host: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not HOST_RE.fullmatch(self.host.lower()) or self.host != self.host.lower():
            raise ValueError("repository host is not sanitized")
        namespace_parts = self.namespace.split("/") if isinstance(self.namespace, str) else ()
        if (
            not isinstance(self.namespace, str)
            or not self.namespace
            or len(self.namespace) > REPOSITORY_NAMESPACE_MAX_LENGTH
            or any(part in DOT_SEGMENTS for part in namespace_parts)
            or not all(PART_RE.fullmatch(part) for part in namespace_parts)
        ):
            raise ValueError("repository namespace is not sanitized")
        if not isinstance(self.name, str) or len(self.name) > REPOSITORY_NAME_MAX_LENGTH or self.name in DOT_SEGMENTS or not PART_RE.fullmatch(self.name):
            raise ValueError("repository name is not sanitized")

    @classmethod
    def from_remote(cls, remote: str) -> RepositoryIdentity:
        if not isinstance(remote, str) or not remote or any(ord(char) < 0x20 or char.isspace() for char in remote):
            raise ValueError("repository remote is invalid")
        if "://" not in remote and re.match(r"^[^/@:]+:[^@]+@", remote):
            raise ValueError("credential-bearing repository remote is rejected")

        if re.match(r"^[^/@]+@[^:]+:.+$", remote):
            user, rest = remote.split("@", 1)
            if user != "git":
                raise ValueError("credential-bearing repository remote is rejected")
            host, path = rest.split(":", 1)
        else:
            try:
                parsed = urlsplit(remote)
            except ValueError as error:
                raise ValueError("repository remote is invalid") from error
            if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
                raise ValueError("repository remote is invalid")
            if parsed.password is not None or (parsed.username is not None and (parsed.scheme in {"http", "https"} or parsed.username != "git")):
                raise ValueError("credential-bearing repository remote is rejected")
            if parsed.query or parsed.fragment:
                raise ValueError("repository remote is invalid")
            host = parsed.hostname
            path = parsed.path

        host = host.lower().rstrip(".")
        if not HOST_RE.fullmatch(host):
            raise ValueError("repository remote is invalid")
        path = unquote(path).strip("/")
        if any(part in DOT_SEGMENTS for part in path.split("/")):
            raise ValueError("repository remote contains a dot segment")
        path = path.removesuffix(".git")
        parts = path.split("/")
        if len(parts) < 2 or not all(PART_RE.fullmatch(part) for part in parts):
            raise ValueError("repository remote is invalid")
        return cls(host, "/".join(parts[:-1]), parts[-1])

    @property
    def stable_id(self) -> str:
        return f"{self.host}/{self.namespace}/{self.name}"

    def to_public(self) -> dict[str, str]:
        return {"host": self.host, "namespace": self.namespace, "name": self.name}


def normalize_workspace_path(value: str) -> str:
    """Normalize a path lexically without following symlinks or reading it."""

    if not isinstance(value, str) or not value or "\x00" in value or any(ord(char) < 0x20 for char in value):
        raise ValueError("workspace path is invalid")
    path = value.replace("\\", "/")
    drive_path = re.match(r"^/?[A-Za-z]:", path)
    parsed = None if drive_path else urlsplit(path)
    if parsed is not None and parsed.scheme and parsed.netloc:
        path = parsed.path
    else:
        path = re.sub(r"/{2,}", "/", path)
    if re.match(r"^/wsl(?:\.localhost|\$)/", path, re.IGNORECASE):
        parts = [part for part in path.split("/") if part]
        path = "/wsl/" + "/".join(parts[1:]).lower()
    drive = re.match(r"^/?([A-Za-z]):(?:/|$)(.*)$", path)
    if drive:
        path = "/" + drive.group(1).lower() + "/" + drive.group(2)
    normalized = posixpath.normpath(path)
    if normalized == ".":
        normalized = "/"
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("workspace path escapes its root")
    if drive:
        normalized = normalized.lower()
    return normalized.rstrip("/") or "/"


@dataclass(frozen=True)
class PolicyContext:
    """Host-observed context. Claims from hooks and repositories are not authority."""

    workspace_id: str | None
    repository: RepositoryIdentity | None
    workspace_path: str | None = None
    normalized_workspace_path: str | None = None
    path_kind: str = "unknown"
    context_source: str = "unknown"
    remote_error: str | None = None
    path_error: str | None = None
    claimed_values: Mapping[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def from_values(
        cls,
        *,
        workspace_id: str | None,
        remote: str | None,
        workspace_path: str | None = None,
        path_kind: str = "unknown",
        context_source: str = "host-observed",
    ) -> PolicyContext:
        repository = None
        remote_error = None
        if remote is not None:
            try:
                repository = RepositoryIdentity.from_remote(remote)
            except ValueError as error:
                remote_error = str(error)
        normalized = None
        path_error = None
        if workspace_path is not None:
            try:
                normalized = normalize_workspace_path(workspace_path)
            except ValueError as error:
                path_error = str(error)
        if path_kind not in PATH_KINDS:
            path_kind = "unknown"
        return cls(workspace_id, repository, workspace_path, normalized, path_kind, context_source, remote_error, path_error)

    @classmethod
    def from_untrusted(cls, *, workspace_id: str | None, remote: str | None, **claims: object) -> PolicyContext:
        return cls(
            workspace_id=None,
            repository=None,
            context_source="untrusted",
            claimed_values=MappingProxyType({"workspace_id": workspace_id, "remote": remote, **claims}),
        )


@dataclass(frozen=True)
class OperatorOverride:
    profile_name: str
    interactive: bool
    confirmed: bool
    actor: str = "unknown"
    audit_reason_code: str | None = None
    cross_scope_confirmed: bool = False


@dataclass(frozen=True)
class CaptureRequest:
    context: PolicyContext
    logical_sources: tuple[str, ...]
    trigger: str
    record_bytes: int = 0
    asset_bytes: int = 0
    session_bytes: int = 0
    outbox_bytes: int = 0
    untrusted_inputs: Mapping[str, object] = field(default_factory=dict, repr=False)
    operator_override: OperatorOverride | None = None

    def __post_init__(self) -> None:
        for field_name in CAPTURE_SIZE_FIELDS:
            _validate_capture_size(getattr(self, field_name))
        if not isinstance(self.trigger, str):
            raise PolicyInputError("trigger.invalid", "capture trigger must be a string")
        object.__setattr__(self, "logical_sources", sanitize_logical_sources(self.logical_sources, allow_empty=True))

    def with_context(self, context: PolicyContext) -> CaptureRequest:
        return replace(self, context=context)


@dataclass(frozen=True)
class Limits:
    per_record_bytes: int
    per_asset_bytes: int
    per_session_bytes: int
    local_outbox_bytes: int
    retention_days: int


@dataclass(frozen=True)
class R2Binding:
    binding_id: str
    scope: str
    credential_profile_ref: str


@dataclass(frozen=True)
class Destination:
    kind: str
    binding_id: str | None = None


@dataclass(frozen=True)
class MatchRule:
    workspace_id: str
    repository: RepositoryIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.repository, RepositoryIdentity):
            raise TypeError("match rule requires a sanitized repository")

    def matches(self, context: PolicyContext) -> bool:
        return context.workspace_id == self.workspace_id and context.repository == self.repository


@dataclass(frozen=True)
class DenyRule(MatchRule):
    reason_code: str = "explicit-deny"


@dataclass(frozen=True)
class CaptureProfile:
    name: str
    profile_id: str
    workspace_id: str
    allowed_sources: frozenset[str]
    capture_mode: str
    triggers: frozenset[str]
    destination: Destination
    recipient_set_ref: str
    limits: Limits
    allow_rules: tuple[MatchRule, ...]
    deny_rules: tuple[DenyRule, ...]
    downstream_memory: bool
    policy_version: str
    provenance: str


@dataclass(frozen=True)
class PolicyConfig:
    profiles: Mapping[str, CaptureProfile]
    r2_bindings: Mapping[str, R2Binding]

    @classmethod
    def from_dict(cls, body: Mapping[str, object]) -> PolicyConfig:
        if not isinstance(body, Mapping):
            raise PolicyConfigError("policy.not-object", "policy must be an object")
        expected = {"$schema", "schema", "schema_version", "r2_bindings", "profiles"}
        if set(body) != expected:
            missing = expected - set(body)
            extra = set(body) - expected
            detail = f"missing {min(missing)}" if missing else f"unsupported field {min(extra)}"
            raise PolicyConfigError("policy.shape", detail)
        if body.get("$schema") != POLICY_SCHEMA_URI:
            raise PolicyConfigError("policy.schema-identity", "policy schema identity is invalid")
        if body.get("schema") != "josh-room.capture-policy":
            raise PolicyConfigError("policy.schema-unknown", "policy schema is unknown")
        version = body.get("schema_version")
        if not isinstance(version, Mapping) or set(version) != {"major", "minor"}:
            raise PolicyConfigError("policy.schema-version-shape", "schema_version must contain only major and minor")
        if (
            type(version.get("major")) is not int
            or version.get("major") != 1
            or type(version.get("minor")) is not int
            or version.get("minor") != 0
        ):
            raise PolicyConfigError("policy.version-unsupported", "policy version is unsupported")

        raw_bindings = body["r2_bindings"]
        if not isinstance(raw_bindings, Mapping):
            raise PolicyConfigError("policy.bindings-shape", "r2_bindings must be an object")
        bindings: dict[str, R2Binding] = {}
        for binding_id, raw in raw_bindings.items():
            binding_id = _identifier(binding_id, "binding id")
            if not isinstance(raw, Mapping) or set(raw) != {"scope", "credential_profile_ref"}:
                raise PolicyConfigError("policy.binding-shape", f"binding {binding_id} is malformed")
            scope = raw.get("scope")
            if not isinstance(scope, str) or scope not in PROFILE_NAMES:
                raise PolicyConfigError("policy.binding-scope", f"binding {binding_id} has an invalid scope")
            bindings[binding_id] = R2Binding(binding_id, scope, _identifier(raw.get("credential_profile_ref"), "credential profile reference"))

        raw_profiles = body["profiles"]
        if not isinstance(raw_profiles, Mapping) or set(raw_profiles) != PROFILE_NAMES:
            raise PolicyConfigError("policy.profiles-shape", "policy must define exactly the four named profiles")
        profiles = {name: _profile_from_dict(name, raw, bindings) for name, raw in raw_profiles.items()}
        if len({profile.profile_id for profile in profiles.values()}) != len(profiles):
            raise PolicyConfigError("policy.profile-id-duplicate", "profile id must be unique")
        if len({profile.workspace_id for profile in profiles.values()}) != len(profiles):
            raise PolicyConfigError("policy.workspace-id-duplicate", "workspace id must be unique")
        return cls(MappingProxyType(profiles), MappingProxyType(bindings))


def _repository_from_dict(raw: object, field_name: str = "repository") -> RepositoryIdentity:
    if not isinstance(raw, Mapping) or set(raw) != {"host", "namespace", "name"}:
        raise PolicyConfigError("policy.repository-shape", f"{field_name} is malformed")
    try:
        return RepositoryIdentity(raw["host"], raw["namespace"], raw["name"])
    except (TypeError, ValueError) as error:
        raise PolicyConfigError("policy.repository-invalid", f"{field_name} is not sanitized") from error


def _match_rule(raw: object, *, deny: bool = False) -> MatchRule | DenyRule:
    if not isinstance(raw, Mapping):
        raise PolicyConfigError("policy.match-shape", "match rule is malformed")
    expected = {"workspace_id", "repository"} | ({"reason_code"} if deny else set())
    if set(raw) - expected or "workspace_id" not in raw:
        raise PolicyConfigError("policy.match-shape", "match rule has unsupported fields")
    if "repository" not in raw:
        raise PolicyConfigError("policy.match-repository", "match rule requires a repository")
    workspace_id = _identifier(raw["workspace_id"], "workspace id")
    repository = _repository_from_dict(raw["repository"])
    if deny:
        reason_code = raw.get("reason_code")
        if not isinstance(reason_code, str) or not REASON_RE.fullmatch(reason_code):
            raise PolicyConfigError("policy.reason-code", "deny reason code is invalid")
        return DenyRule(workspace_id, repository, reason_code)
    return MatchRule(workspace_id, repository)


def _profile_from_dict(name: str, raw: object, bindings: Mapping[str, R2Binding]) -> CaptureProfile:
    if not isinstance(raw, Mapping):
        raise PolicyConfigError("policy.profile-shape", f"profile {name} is malformed")
    required = {"profile_id", "workspace_id", "allowed_sources", "capture_mode", "triggers", "destination", "recipient_set_ref", "limits", "allow_rules", "deny_rules", "downstream_memory", "policy_version", "provenance"}
    if set(raw) != required:
        raise PolicyConfigError("policy.profile-shape", f"profile {name} has unsupported or missing fields")
    if name not in PROFILE_NAMES:
        raise PolicyConfigError("policy.profile-name", f"profile {name} is unknown")
    profile_id = _identifier(raw["profile_id"], "profile id")
    workspace_id = _identifier(raw["workspace_id"], "workspace id")
    sources = raw["allowed_sources"]
    try:
        sources = sanitize_logical_sources(sources)
    except PolicyInputError as error:
        raise PolicyConfigError("policy.sources", f"profile {name} has invalid logical sources") from error
    capture_mode = raw["capture_mode"]
    if not isinstance(capture_mode, str) or capture_mode not in CAPTURE_MODES:
        raise PolicyConfigError("policy.capture-mode", f"profile {name} has an invalid capture mode")
    triggers = raw["triggers"]
    if (
        not isinstance(triggers, (list, tuple))
        or not triggers
        or not all(isinstance(item, str) and item in TRIGGERS for item in triggers)
        or len(set(triggers)) != len(triggers)
    ):
        raise PolicyConfigError("policy.triggers", f"profile {name} has invalid triggers")
    destination_raw = raw["destination"]
    if not isinstance(destination_raw, Mapping) or "kind" not in destination_raw:
        raise PolicyConfigError("policy.destination-shape", f"profile {name} destination is malformed")
    kind = destination_raw["kind"]
    if kind == "local-only":
        if set(destination_raw) != {"kind"}:
            raise PolicyConfigError("policy.destination-shape", f"profile {name} local destination is malformed")
        destination = Destination(kind)
    elif kind == "private-r2":
        if set(destination_raw) != {"kind", "binding_id"}:
            raise PolicyConfigError("policy.destination-shape", f"profile {name} R2 destination is malformed")
        binding_id = _identifier(destination_raw["binding_id"], "binding id")
        binding = bindings.get(binding_id)
        if binding is None:
            raise PolicyConfigError("policy.destination-missing", f"profile {name} destination binding is missing")
        if binding.scope != name:
            raise PolicyConfigError("policy.destination-scope", f"profile {name} destination scope is separated")
        destination = Destination(kind, binding_id)
    else:
        raise PolicyConfigError("policy.destination-kind", f"profile {name} destination kind is invalid")
    limits_raw = raw["limits"]
    if not isinstance(limits_raw, Mapping) or set(limits_raw) != {"per_record_bytes", "per_asset_bytes", "per_session_bytes", "local_outbox_bytes", "retention_days"}:
        raise PolicyConfigError("policy.limits-shape", f"profile {name} limits are malformed")
    limits = Limits(*(_positive_int(limits_raw[key], key) for key in ("per_record_bytes", "per_asset_bytes", "per_session_bytes", "local_outbox_bytes", "retention_days")))
    allow_raw = raw["allow_rules"]
    deny_raw = raw["deny_rules"]
    if not isinstance(allow_raw, list) or not isinstance(deny_raw, list):
        raise PolicyConfigError("policy.match-list", f"profile {name} match rules must be arrays")
    allow_rules = tuple(_match_rule(item) for item in allow_raw)
    deny_rules = tuple(_match_rule(item, deny=True) for item in deny_raw)
    if any(rule.workspace_id != workspace_id for rule in (*allow_rules, *deny_rules)):
        raise PolicyConfigError("policy.match-workspace", f"profile {name} rules must use its workspace id")
    if type(raw["downstream_memory"]) is not bool:
        raise PolicyConfigError("policy.memory-permission", f"profile {name} downstream_memory must be boolean")
    if raw["policy_version"] != "1.0" or raw["provenance"] != "private-host-config":
        raise PolicyConfigError("policy.provenance", f"profile {name} provenance is invalid")
    return CaptureProfile(name, profile_id, workspace_id, frozenset(sources), capture_mode, frozenset(triggers), destination, _identifier(raw["recipient_set_ref"], "recipient set reference"), limits, allow_rules, deny_rules, raw["downstream_memory"], raw["policy_version"], raw["provenance"])


@dataclass(frozen=True)
class Decision:
    kind: str
    reason_codes: tuple[str, ...]
    profile_name: str | None = None
    profile_id: str | None = None
    workspace_id: str | None = None
    destination_class: str = "local-only"
    logical_sources: tuple[str, ...] = ()
    estimated_sizes: Mapping[str, int] = field(default_factory=dict)
    authority: str = "host-policy"
    operator_override: str | None = None
    operator_reason_code: str | None = None
    policy_version: str | None = None
    provenance: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_sources", sanitize_logical_sources(self.logical_sources, allow_empty=True))
        if not isinstance(self.estimated_sizes, Mapping):
            raise PolicyInputError("size.invalid", "estimated sizes are invalid")
        if self.estimated_sizes:
            if set(self.estimated_sizes) != set(CAPTURE_SIZE_FIELDS):
                raise PolicyInputError("size.invalid", "estimated sizes are invalid")
            for field_name in CAPTURE_SIZE_FIELDS:
                _validate_capture_size(self.estimated_sizes[field_name])
        object.__setattr__(self, "estimated_sizes", MappingProxyType(dict(self.estimated_sizes)))

    def to_dry_run(self) -> dict[str, object]:
        return {
            "decision": self.kind,
            "reason_codes": list(self.reason_codes),
            "profile": self.profile_name,
            "profile_id": self.profile_id,
            "workspace_id": self.workspace_id,
            "logical_sources": list(self.logical_sources),
            "estimated_sizes": dict(self.estimated_sizes),
            "destination_class": self.destination_class,
            "authority": self.authority,
            "operator_override": self.operator_override,
            "operator_reason_code": self.operator_reason_code,
            "policy_version": self.policy_version,
            "provenance": self.provenance,
        }


def _decision(
    kind: str,
    reasons: tuple[str, ...],
    request: CaptureRequest,
    profile: CaptureProfile | None = None,
    *,
    authority: str = "host-policy",
    operator_override: str | None = None,
    operator_reason_code: str | None = None,
) -> Decision:
    return Decision(
        kind=kind,
        reason_codes=reasons,
        profile_name=profile.name if profile else None,
        profile_id=profile.profile_id if profile else None,
        workspace_id=profile.workspace_id if profile else None,
        destination_class=profile.destination.kind if profile and kind in {"allow", "local-only"} else "local-only",
        logical_sources=tuple(request.logical_sources),
        estimated_sizes={"record_bytes": request.record_bytes, "asset_bytes": request.asset_bytes, "session_bytes": request.session_bytes, "outbox_bytes": request.outbox_bytes},
        authority=authority,
        operator_override=operator_override,
        operator_reason_code=operator_reason_code,
        policy_version=profile.policy_version if profile else None,
        provenance=profile.provenance if profile else None,
    )


def _matching_profiles(config: PolicyConfig, context: PolicyContext) -> list[CaptureProfile]:
    return [profile for profile in config.profiles.values() if any(rule.matches(context) for rule in profile.allow_rules)]


def _matching_denials(config: PolicyConfig, context: PolicyContext) -> list[tuple[str, str]]:
    return [
        (profile.name, rule.reason_code)
        for profile in config.profiles.values()
        for rule in profile.deny_rules
        if rule.matches(context)
    ]


def decide(config: PolicyConfig, request: CaptureRequest) -> Decision:
    """Evaluate a validated policy with deterministic, fail-closed precedence."""

    if request.context.context_source != "host-observed":
        return _decision("local-only", ("context.untrusted",), request)
    if request.context.remote_error:
        reason = "repository.remote-credentials" if "credential-bearing" in request.context.remote_error else "repository.remote-invalid"
        return _decision("quarantine", (reason,), request)
    if request.context.path_error:
        return _decision("quarantine", ("workspace.path-invalid",), request)
    if request.context.path_kind == "symlink":
        return _decision("quarantine", ("workspace.symlink",), request)
    if not request.logical_sources:
        return _decision("deny", ("source.empty",), request)
    override = request.operator_override
    authority = "host-policy"
    operator_override = None
    operator_reason_code = None
    denied = _matching_denials(config, request.context)
    if denied:
        name, reason = min(denied)
        return _decision("deny", ("policy.explicit-deny", reason), request, config.profiles[name])

    candidates = _matching_profiles(config, request.context)
    if override is not None:
        if (
            override.actor != "operator-cli"
            or type(override.interactive) is not bool
            or type(override.confirmed) is not bool
            or type(override.cross_scope_confirmed) is not bool
            or not override.interactive
            or not override.confirmed
            or not isinstance(override.profile_name, str)
            or override.profile_name not in config.profiles
        ):
            return _decision("local-only", ("operator.override-unavailable",), request)
        if not isinstance(override.audit_reason_code, str) or not REASON_RE.fullmatch(override.audit_reason_code):
            return _decision("local-only", ("operator.audit-reason-required",), request)
        if any(candidate.name != override.profile_name for candidate in candidates) and not override.cross_scope_confirmed:
            return _decision(
                "local-only",
                ("operator.scope-change-confirmation-required",),
                request,
                authority="operator-cli",
                operator_override=override.profile_name,
                operator_reason_code=override.audit_reason_code,
            )
        profile = config.profiles[override.profile_name]
        authority = "operator-cli"
        operator_override = override.profile_name
        operator_reason_code = override.audit_reason_code
    else:
        if len(candidates) > 1:
            return _decision("quarantine", ("policy.ambiguous-profile",), request)
        if not candidates:
            return _decision("local-only", ("context.unknown",), request)
        profile = candidates[0]

    if request.trigger not in profile.triggers:
        return _decision("deny", ("trigger.not-allowed",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    if not set(request.logical_sources).issubset(profile.allowed_sources):
        return _decision("deny", ("source.not-allowed",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    if profile.capture_mode == "metadata-only" and any(source.endswith(("transcript", "assets")) for source in request.logical_sources):
        return _decision("deny", ("capture-mode.metadata-only",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    if profile.capture_mode == "transcript" and any(source.endswith("assets") for source in request.logical_sources):
        return _decision("deny", ("capture-mode.assets-not-allowed",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    limit_checks = (
        (request.record_bytes, profile.limits.per_record_bytes, "limits.per-record"),
        (request.asset_bytes, profile.limits.per_asset_bytes, "limits.per-asset"),
        (request.session_bytes, profile.limits.per_session_bytes, "limits.per-session"),
        (request.outbox_bytes, profile.limits.local_outbox_bytes, "limits.local-outbox"),
    )
    for observed, limit, reason in limit_checks:
        if observed < 0 or observed > limit:
            return _decision("quarantine", (reason,), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    if profile.destination.kind == "local-only":
        return _decision("local-only", ("destination.local-only",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)
    return _decision("allow", ("policy.allowed",), request, profile, authority=authority, operator_override=operator_override, operator_reason_code=operator_reason_code)


def dry_run(config: PolicyConfig, request: CaptureRequest) -> dict[str, object]:
    return decide(config, request).to_dry_run()
