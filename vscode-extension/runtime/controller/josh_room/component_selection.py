"""Closed, non-authorizing Save component selection request."""
from __future__ import annotations

import hashlib
import json

COMPONENTS = ("rcc_environment", "homebrew_recovery", "hauler_content")


def selection_request(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or set(value) != {"format_version", "components"}:
        raise ValueError("invalid component selection request")
    modes = value["components"]
    if type(value["format_version"]) is not int or value["format_version"] != 1 or not isinstance(modes, dict) or set(modes) != set(COMPONENTS):
        raise ValueError("unsupported component selection request")
    if any(mode not in ("keep", "refresh", "omit") for mode in modes.values()):
        raise ValueError("unsupported component selection mode")
    return {"format_version": 1, "components": dict(modes)}


def selection_digest(request):
    return hashlib.sha256(json.dumps(selection_request(request), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def selection_plan(request, prior, *, rcc_present=False, homebrew=False, hauler=False):
    modes = request["components"]
    supplied = {"rcc_environment": rcc_present, "homebrew_recovery": homebrew, "hauler_content": hauler}
    return {name: {"mode": modes[name], "state": (
        "excluded" if modes[name] == "omit" else
        "pending" if modes[name] == "refresh" and supplied[name] else
        "reused" if prior.get(name) is not None else "absent"
    )} for name in COMPONENTS}


def plan_token(request, *, dimension, project, workspace, policy_sha256, archive=None, directory=None, hauler=None, prior_snapshot=None):
    """Bind reviewed selection to target, policy and explicit external input bytes."""
    import os
    import stat
    from pathlib import Path

    def file_hash(path):
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024**3:
                raise ValueError("component input must be a bounded regular file")
            digest = hashlib.sha256()
            total = 0
            while block := source.read(1024 * 1024):
                total += len(block)
                if total > 8 * 1024**3:
                    raise ValueError("component input exceeds its limit")
                digest.update(block)
            after = os.fstat(source.fileno())
            if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("component input changed during preview")
            return digest.hexdigest()

    inputs = []
    if archive is not None:
        inputs.append(["archive", file_hash(archive)])
    if directory is not None:
        from .room_store_homebrew import recipe_bytes
        inputs.append(["recipe", hashlib.sha256(recipe_bytes(directory)).hexdigest()])
    if hauler:
        # Image identities are resolved by Hauler during Save, not this read-only plan.
        inputs.append(["images", list(hauler["images"]), hauler["all_images"]])
        inputs.extend(["manifest", file_hash(path)] for path in hauler["manifests"])
        inputs.extend(["file", name, file_hash(path)] for path, name in hauler["files"])
    value = [selection_request(request), dimension, project, str(Path(workspace).resolve()), policy_sha256, inputs, prior_snapshot]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
