"""Private XDG policy boundary for capture authorization."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .policy import CaptureRequest, Decision, PolicyConfig, PolicyConfigError, decide


def _reject_json_float(_value: str) -> None:
    raise ValueError("decimal policy numbers are not permitted")


def _deny_decision(request: CaptureRequest, reason_code: str) -> Decision:
    return Decision(
        "deny",
        (reason_code,),
        logical_sources=tuple(request.logical_sources),
        estimated_sizes={
            "record_bytes": request.record_bytes,
            "asset_bytes": request.asset_bytes,
            "session_bytes": request.session_bytes,
            "outbox_bytes": request.outbox_bytes,
        },
    )


def _config_home(*, config_home: Path | None) -> Path:
    if config_home is not None:
        configured = config_home
    elif "XDG_CONFIG_HOME" in os.environ:
        configured = os.environ["XDG_CONFIG_HOME"]
        if not configured:
            raise PolicyConfigError("policy.config-home-invalid", "config home must not be empty")
    else:
        configured = Path.home() / ".config"
    try:
        root = Path(configured)
    except (TypeError, ValueError) as error:
        raise PolicyConfigError("policy.config-home-invalid", "config home is invalid") from error
    if not root.is_absolute():
        raise PolicyConfigError("policy.config-home-invalid", "config home must be absolute")
    return Path(os.path.abspath(os.fspath(root)))


def _validate_no_symlink_components(path: Path) -> None:
    current = Path(path.parts[0])
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except (OSError, ValueError) as error:
            raise PolicyConfigError("policy.config-boundary", "private policy path cannot be inspected") from error
        if stat.S_ISLNK(mode):
            raise PolicyConfigError("policy.config-boundary", "private policy path cannot contain symlinks")


def _validate_owner(status: os.stat_result, reason_code: str) -> None:
    if os.name == "posix" and status.st_uid != os.getuid():
        raise PolicyConfigError(reason_code, "private policy path is not host-owned")


def _validate_config_home_root(root: Path) -> None:
    _validate_no_symlink_components(root)
    try:
        status = root.lstat()
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        raise PolicyConfigError("policy.config-boundary", "config home cannot be inspected") from error
    if stat.S_ISLNK(status.st_mode):
        raise PolicyConfigError("policy.config-boundary", "config home cannot be a symlink")
    if not stat.S_ISDIR(status.st_mode):
        raise PolicyConfigError("policy.config-home-invalid", "config home must be a directory")
    _validate_owner(status, "policy.config-owner")


def _validate_private_policy_dir(root: Path) -> Path:
    policy_dir = root / "josh-room"
    _validate_no_symlink_components(policy_dir)
    try:
        status = policy_dir.lstat()
    except FileNotFoundError:
        return policy_dir
    except (OSError, ValueError) as error:
        raise PolicyConfigError("policy.config-boundary", "private policy directory cannot be inspected") from error
    if stat.S_ISLNK(status.st_mode):
        raise PolicyConfigError("policy.config-boundary", "private policy directory cannot be a symlink")
    if not stat.S_ISDIR(status.st_mode):
        raise PolicyConfigError("policy.config-boundary", "private policy path is not a directory")
    _validate_owner(status, "policy.config-owner")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise PolicyConfigError("policy.config-permissions", "private policy directory is too broadly accessible")
    return policy_dir


def policy_config_dir(*, config_home: Path | None = None) -> Path:
    root = _config_home(config_home=config_home)
    _validate_config_home_root(root)
    return _validate_private_policy_dir(root)


def policy_config_path(*, config_home: Path | None = None) -> Path:
    return policy_config_dir(config_home=config_home) / "policy.json"


def load_policy_config(*, config_home: Path | None = None) -> PolicyConfig:
    try:
        path = policy_config_path(config_home=config_home)
        try:
            status = path.lstat()
        except FileNotFoundError:
            raise PolicyConfigError("policy.config-missing", "private capture policy is missing")
        except (OSError, ValueError) as error:
            raise PolicyConfigError("policy.config-boundary", "private capture policy cannot be inspected") from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise PolicyConfigError("policy.config-not-regular", "private capture policy must be a regular file")
        _validate_owner(status, "policy.config-owner")
        mode = stat.S_IMODE(status.st_mode)
        if mode & 0o077:
            raise PolicyConfigError("policy.config-permissions", "private capture policy is too broadly readable")
        try:
            body = json.loads(path.read_text(encoding="utf-8"), parse_float=_reject_json_float)
        except (OSError, UnicodeError, ValueError) as error:
            raise PolicyConfigError("policy.config-invalid", "private capture policy cannot be read") from error
    except PolicyConfigError:
        raise
    except Exception as error:
        raise PolicyConfigError("policy.config-invalid", "private capture policy is invalid") from error
    try:
        return PolicyConfig.from_dict(body)
    except PolicyConfigError:
        raise
    except Exception as error:
        raise PolicyConfigError("policy.config-invalid", "private capture policy is invalid") from error


def decision_from_private_config(request: CaptureRequest, *, config_home: Path | None = None) -> Decision:
    try:
        config = load_policy_config(config_home=config_home)
    except PolicyConfigError as error:
        return _deny_decision(request, error.reason_code)
    return decide(config, request)
