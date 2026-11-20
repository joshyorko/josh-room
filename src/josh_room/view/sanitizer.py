"""Mandatory server-side and pre-export sanitization and sensitivity assessment."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .contract import (
    NON_SENSITIVE_MARKINGS,
    SENSITIVITY_LEVELS,
    SENSITIVITY_ORDER,
    GroupState,
    ItemState,
    StageDescriptor,
    StepState,
    ViewDescriptor,
    ViewState,
    ViewValidationError,
    validate_state_against_descriptor,
)

_SECRET_KEY_MARKERS = frozenset(
    {
        "secret",
        "password",
        "passwd",
        "token",
        "credential",
        "private_key",
        "private-key",
        "access_key",
        "access-key",
        "api_key",
        "api-key",
        "identity",
        "cookie",
        "authorization",
        "bearer",
        "ssn",
    }
)

_FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("age-identity", re.compile(r"\bAGE-SECRET-KEY-[A-Za-z0-9_-]+")),
    ("age-recipient", re.compile(r"\bage1[0-9a-z]{20,}")),
    ("private-key-pem", re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "host-path",
        re.compile(r"(?:(?<![A-Za-z0-9])(?:/home/|/Users/|/root/|/var/|/etc/|/private/)|[A-Za-z]:\\|\\\\wsl)"),
    ),
    (
        "private-ip",
        re.compile(
            r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![0-9.])"
        ),
    ),
    (
        "private-endpoint",
        re.compile(r"(?i)(?:\.workers\.dev|\.r2\.cloudflarestorage\.com|\.home\.arpa|\.lan\b|\.local\b)"),
    ),
)


@dataclass(frozen=True, slots=True)
class SanitizationPolicy:
    max_label_length: int = 96
    max_text_length: int = 160
    max_fields_per_item: int = 32
    max_summary_entries: int = 64
    max_digit_run: int = 8


DEFAULT_SANITIZATION_POLICY = SanitizationPolicy()


def _check_safe_text(
    value: str,
    field_name: str,
    *,
    max_length: int,
    max_digit_run: int,
) -> str:
    if len(value) > max_length:
        raise ViewValidationError(
            "text-oversize",
            f"{field_name} exceeds maximum length ({max_length})",
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ViewValidationError("control-chars", f"{field_name} contains control characters")
    digit_run = re.search(rf"\d{{{max_digit_run + 1},}}", value)
    if digit_run:
        raise ViewValidationError(
            "long-digit-run",
            f"{field_name} contains a digit run exceeding {max_digit_run} digits",
        )
    for code, pattern in _FORBIDDEN_VALUE_PATTERNS:
        if pattern.search(value):
            raise ViewValidationError(
                f"forbidden-{code}",
                f"{field_name} contains disallowed sensitive or host-specific content ({code})",
            )
    return value


def _check_key_not_secret(key: str, context: str) -> str:
    lowered = key.lower()
    if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
        raise ViewValidationError(
            "forbidden-secret-key",
            f"{context} key {key!r} is disallowed by sanitizer",
        )
    return key


def sanitize_descriptor(
    descriptor: ViewDescriptor,
    *,
    policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
) -> ViewDescriptor:
    """Validate and sanitize a ViewDescriptor."""
    if not isinstance(descriptor, ViewDescriptor):
        raise ViewValidationError("invalid-descriptor", "descriptor must be a ViewDescriptor")
    title = _check_safe_text(
        descriptor.title,
        "descriptor.title",
        max_length=policy.max_label_length,
        max_digit_run=policy.max_digit_run,
    )
    group_label = _check_safe_text(
        descriptor.group_label,
        "descriptor.group_label",
        max_length=48,
        max_digit_run=policy.max_digit_run,
    )
    stages = tuple(
        StageDescriptor(
            id=stage.id,
            name=_check_safe_text(
                stage.name,
                f"stage.{stage.id}.name",
                max_length=64,
                max_digit_run=policy.max_digit_run,
            ),
            order=stage.order,
            terminal=stage.terminal,
            failure=stage.failure,
        )
        for stage in descriptor.stages
    )
    return ViewDescriptor(
        title=title,
        stages=stages,
        group_label=group_label,
        legend=descriptor.legend,
        metric=descriptor.metric,
        view=descriptor.view,
        tool=descriptor.tool,
        tool_version=descriptor.tool_version,
        sensitivity=descriptor.sensitivity,
        sql_presets=descriptor.sql_presets,
    )


def _apply_base_state_sanitization(
    descriptor: ViewDescriptor,
    state: ViewState,
    policy: SanitizationPolicy,
) -> ViewState:
    validate_state_against_descriptor(descriptor, state)

    groups = tuple(
        GroupState(
            id=group.id,
            name=_check_safe_text(
                group.name,
                f"group.{group.id}.name",
                max_length=64,
                max_digit_run=policy.max_digit_run,
            ),
            order=group.order,
        )
        for group in state.groups
    )

    sanitized_items: list[ItemState] = []
    for item in state.items:
        label = _check_safe_text(
            item.label,
            f"item.{item.id}.label",
            max_length=policy.max_label_length,
            max_digit_run=policy.max_digit_run,
        )
        if len(item.fields) > policy.max_fields_per_item:
            raise ViewValidationError(
                "too-many-fields",
                f"item.{item.id} exceeds max fields ({policy.max_fields_per_item})",
            )
        clean_fields: dict[str, str | int | float | bool | None] = {}
        for key, value in item.fields.items():
            _check_key_not_secret(key, f"item.{item.id}.fields")
            if isinstance(value, str):
                clean_fields[key] = _check_safe_text(
                    value,
                    f"item.{item.id}.fields.{key}",
                    max_length=policy.max_text_length,
                    max_digit_run=policy.max_digit_run,
                )
            else:
                clean_fields[key] = value
        sanitized_items.append(
            ItemState(
                id=item.id,
                group=item.group,
                stage=item.stage,
                kind=item.kind,
                label=label,
                updated_at=item.updated_at,
                metric=item.metric,
                attachments=item.attachments,
                fields=MappingProxyType(clean_fields),
            )
        )

    steps = tuple(
        StepState(
            item_id=step.item_id,
            name=_check_safe_text(
                step.name,
                f"step.{step.item_id}.name",
                max_length=64,
                max_digit_run=policy.max_digit_run,
            ),
            at=step.at,
        )
        for step in state.steps
    )

    if len(state.summary) > policy.max_summary_entries:
        raise ViewValidationError(
            "too-many-summary-entries",
            f"summary exceeds maximum entries ({policy.max_summary_entries})",
        )
    clean_summary: dict[str, str | int | float | bool | None] = {}
    for key, value in state.summary.items():
        _check_key_not_secret(key, "summary")
        if isinstance(value, str):
            clean_summary[key] = _check_safe_text(
                value,
                f"summary.{key}",
                max_length=policy.max_text_length,
                max_digit_run=policy.max_digit_run,
            )
        else:
            clean_summary[key] = value

    return ViewState(
        taken_at=state.taken_at,
        since=state.since,
        groups=groups,
        items=tuple(sanitized_items),
        steps=steps,
        summary=MappingProxyType(clean_summary),
        stale=state.stale,
    )


def sanitize_state(
    descriptor: ViewDescriptor,
    state: ViewState,
    *,
    policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
    extra_sanitizer: Callable[[ViewState], ViewState] | None = None,
) -> ViewState:
    """Enforce mandatory base sanitization plus optional stricter tool hook.

    Tools can supply an ``extra_sanitizer`` to tighten rules further, but they
    can never bypass or weaken the base validator/sanitizer.
    """
    clean_descriptor = sanitize_descriptor(descriptor, policy=policy)
    first_pass = _apply_base_state_sanitization(clean_descriptor, state, policy)
    if extra_sanitizer is None:
        return first_pass
    tightened = extra_sanitizer(first_pass)
    if not isinstance(tightened, ViewState):
        raise ViewValidationError("invalid-sanitizer-result", "custom sanitizer must return ViewState")
    return _apply_base_state_sanitization(clean_descriptor, tightened, policy)


@dataclass(frozen=True, slots=True)
class SensitivityAssessment:
    level: str
    sources: tuple[str, ...]
    requires_acknowledgment: bool
    banner_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "sources": list(self.sources),
            "requires_acknowledgment": self.requires_acknowledgment,
            "banner_text": self.banner_text,
        }


def _read_workspace_sensitivity(workspace: Path | None) -> tuple[str | None, str | None]:
    if workspace is None:
        return None, None
    root = Path(workspace)
    ignore = root / ".josh-roomignore"
    if ignore.is_file() and not ignore.is_symlink():
        try:
            text = ignore.read_text(encoding="utf-8", errors="strict")
        except OSError:
            return "sensitive", "workspace-policy-unreadable"
        directives = [
            line.strip().split(":", 1)[1].strip().lower()
            for line in text.splitlines()
            if line.strip().lower().startswith("# josh-room sensitivity:")
        ]
        if directives:
            # Take most restrictive valid level; fail closed on unknown level.
            worst = None
            for raw in directives:
                level = raw if raw in SENSITIVITY_LEVELS else "sensitive"
                if worst is None or SENSITIVITY_ORDER[level] > SENSITIVITY_ORDER[worst]:
                    worst = level
            return worst, "workspace-capture-policy"
    return None, None


def resolve_sensitivity(
    descriptor: ViewDescriptor,
    *,
    workspace: Path | None = None,
) -> SensitivityAssessment:
    """Resolve effective sensitivity from provider descriptor and workspace policy.

    Most restrictive marking always wins (fail-closed).
    """
    level = descriptor.sensitivity
    sources = [f"provider:{descriptor.sensitivity}"]

    ws_level, ws_source = _read_workspace_sensitivity(workspace)
    if ws_level is not None and ws_source is not None:
        sources.append(f"{ws_source}:{ws_level}")
        if SENSITIVITY_ORDER[ws_level] > SENSITIVITY_ORDER[level]:
            level = ws_level

    requires_ack = level not in NON_SENSITIVE_MARKINGS
    if requires_ack:
        banner = f"Contains {level} data; keep on approved storage"
    else:
        banner = f"Contains {level} data; synthetic/non-regulated snapshot"
    return SensitivityAssessment(
        level=level,
        sources=tuple(sources),
        requires_acknowledgment=requires_ack,
        banner_text=banner,
    )
