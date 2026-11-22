"""Room-bound, private-root-free status and explicit native history discovery."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from . import native_agent_admission as authority
from . import native_agents as native
from .cancellation import sigterm_cancellation
from .private_paths import verify_private_path

_PROFILE_REF = re.compile(r"[0-9a-f]{64}\Z")


def _scope(dimension, room, workspace):
    native._linux()
    if any(not isinstance(value, str) or not authority._LABEL.fullmatch(value) for value in (dimension, room)):
        native._fail("native-room-binding-invalid")
    return authority._canonical(workspace)


def _matching_grants(dimension, room, workspace):
    directory = authority.policy_config_dir()
    if directory.is_relative_to(workspace):
        native._fail("native-room-authority-unsafe")
    verify_private_path(directory, directory=True)
    policy = authority._read_private_json(directory / authority.POLICY_FILE)
    if (not isinstance(policy, dict) or set(policy) != {"format", "grants"}
            or policy["format"] not in {authority.FORMAT, authority.FORMAT_V2}
            or not isinstance(policy["grants"], list) or not 1 <= len(policy["grants"]) <= 64):
        native._fail("native-room-authority-invalid")
    grants = [authority._grant(grant, policy["format"]) for grant in policy["grants"]]
    identities = [(grant["dimension"], grant["room"], grant["profile"], grant["workspace"]) for grant in grants]
    if len(identities) != len(set(identities)):
        native._fail("native-room-authority-ambiguous")
    matching = []
    for grant in grants:
        if (grant["dimension"], grant["room"], grant["workspace"]) != (dimension, room, str(workspace)):
            continue
        if directory.is_relative_to(Path(grant["profile_root"])):
            native._fail("native-room-authority-unsafe")
        encoded = json.dumps({"format": policy["format"], "grant": grant}, sort_keys=True, separators=(",", ":")).encode()
        reference = hashlib.sha256(encoded).hexdigest()
        tools = grant["tools"] if policy["format"] == authority.FORMAT_V2 else ["codex"]
        matching.append((reference, grant, sorted(tools)))
    return matching


def room_status(*, dimension, room, workspace):
    workspace = _scope(dimension, room, workspace)
    result = {
        "ok": True, "state": "room-status", "profiles": [],
        "capabilities": native.capabilities(), "native_resume_verified": False,
        "original_t3_identity_restored": False,
    }
    try:
        matching = _matching_grants(dimension, room, workspace)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        result["reason"] = "native-room-authority-unavailable"
        return result
    for reference, grant, tools in matching:
        profile = {"profile_ref": reference, "scope": grant["scope"], "tools": tools,
                   "state": "unavailable", "admission": "revalidation-required"}
        binding = {name: grant[name] for name in ("dimension", "room", "profile", "workspace")}
        try:
            profile["state"] = native.status(Path(grant["profile_root"]), **binding)["state"]
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            profile["reason"] = "native-profile-unavailable"
        result["profiles"].append(profile)
    if not result["profiles"]:
        result["reason"] = "native-room-profile-unavailable"
    return result


def _resolve(dimension, room, workspace, profile_ref, tool):
    if not isinstance(profile_ref, str) or not _PROFILE_REF.fullmatch(profile_ref):
        native._fail("native-profile-selector-invalid")
    try:
        matching = _matching_grants(dimension, room, workspace)
    except native.NativeAgentError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        native._fail("native-room-authority-unavailable")
    selected = [(grant, tools) for reference, grant, tools in matching if reference == profile_ref]
    if len(selected) != 1:
        native._fail("native-profile-selector-stale-or-unavailable")
    grant, tools = selected[0]
    if tool not in tools:
        native._fail("native-admission-tool-not-authorized")
    return grant


def discover(*, dimension, room, workspace, profile_ref, tool):
    workspace = _scope(dimension, room, workspace)
    tool = native._tool(tool)
    grant = _resolve(dimension, room, workspace, profile_ref, tool)
    binding = {name: grant[name] for name in ("dimension", "room", "profile", "workspace")}
    root = native._load(Path(grant["profile_root"]), binding)
    recipients = grant["recipients"]
    profile_recipients = native._json(native._read(root / native.POLICY, 4096))["recipients"]
    if native._recipients(profile_recipients) != native._recipients(recipients):
        native._fail("profile-recipient-mismatch")
    before = native._admit(binding, recipients, root=root, operation="discover", tool=tool)
    with native._lock(root), sigterm_cancellation(), native._capture_barrier(root, tool, str(workspace)) as (barrier, expected):
        entries, _ = native._entries(root, binding, barrier, expected, tool)
        sessions = []
        identities = set()
        for entry in entries:
            data = base64.b64decode(entry["data"], validate=True)
            identity = native._session_id(entry["path"], data, tool)
            if identity in identities:
                native._fail("native-session-identity-ambiguous")
            identities.add(identity)
            sessions.append({
                "provider_session_id": identity, "tool": tool, "cwd_matches": True,
                "record_count": len(data.splitlines()), "native_resume_verified": False,
                "t3_import": {"state": "requires-verified-owned-server", "new_t3_thread_id": None,
                              "original_t3_identity_restored": False},
            })
        _resolve(dimension, room, workspace, profile_ref, tool)
        if native._admit(binding, recipients, root=root, operation="discover", tool=tool) != before:
            native._fail("native-admission-changed")
        if native._inventory(root, tool, str(workspace)) != expected:
            native._fail("source-changed")
        barrier.check()
    return {"ok": True, "state": "history-discovered", "tool": tool, "scope": grant["scope"],
            "sessions": sessions, "native_resume_verified": False, "original_t3_identity_restored": False}
