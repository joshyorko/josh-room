"""Explicit dedicated native profiles and bounded, encrypted Codex history recovery.

This is not PCC evidence and never claims verified native continuation. T3's
database remains blocked: its durable effect outbox can replay side effects.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .cancellation import sigterm_cancellation, terminate_owned_process
from .crypto import _managed_executable
from .material_security import Decision, MaterialClass, scan_content
from .native_agent_quiescence import QuiescenceError, read_barrier
from .private_paths import verify_private_path

MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 256
FORMAT = "josh-room.native-history.v1"
POLICY = "native-profile.json"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ROLLOUT = re.compile(r"sessions/[0-9]{4}/[0-9]{2}/[0-9]{2}/rollout-[A-Za-z0-9_-]{1,180}\.jsonl\Z")
CODEX_VERSION = "0.171.0"


class NativeAgentError(ValueError):
    """A public-safe reason code without source content or paths."""


def _fail(reason):
    raise NativeAgentError(reason)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate-json-field")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except RecursionError:
        _fail("json-nesting-limit")


def _inspect_native_values(value, *, depth=0):
    if depth > 32:
        _fail("json-nesting-limit")
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in {"access_token", "refresh_token", "id_token", "api_key", "apikey", "authorization", "password", "secret", "client_secret", "cookie", "cookies", "credential", "credentials", "private_key", "auth_token", "bearer_token"} or normalized.endswith(("_password", "_secret", "_api_key", "_access_token", "_refresh_token")):
                _fail("unsafe-native-content")
            _inspect_native_values(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _inspect_native_values(item, depth=depth + 1)
    elif isinstance(value, str):
        scan = scan_content([value.encode()], adapter="codex-native", material_class=MaterialClass.SESSION_TRANSCRIPT, max_bytes=MAX_BYTES)
        if scan.receipt.decision != Decision.ALLOW:
            _fail("unsafe-native-content")


def capabilities():
    return {
        "format": FORMAT,
        "contract_version": 1,
        "native_resume_verified": False,
        "providers": {
            "t3": {"capture": "blocked", "reason": "effect-outbox-and-schema-audit-required", "launch": "dedicated-linux-profile", "restore": "blocked", "selectable": False},
            "codex": {"capture": "history-only", "reason": "new-turn-acceptance-not-run", "allowlist": "dedicated sessions rollout JSONL only", "restore": "blocked", "restore_reason": "crash-safe-materialization-unavailable", "selectable": False},
            "claude": {"capture": "blocked", "reason": "native-policy-and-cold-resume-not-qualified", "restore": "blocked", "selectable": False},
        },
        "storage": "explicit-local-encrypted-reference",
        "automatic_room_restore": False,
        "experimental": True,
        "admission_requirement": "host-grant-and-current-keyset-required",
        "capture_consistency": "linux-kernel-read-leases-and-inventory-fences",
        "release_blockers": ["crash-safe-materialization-unavailable", "native-new-turn-not-run"],
    }


def _linux():
    if not sys.platform.startswith("linux"):
        _fail("platform-not-qualified")


def _canonical(path, *, exists=True):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        _fail("unsafe-path")
    for ancestor in reversed((path, *path.parents)):
        if ancestor.is_symlink():
            _fail("unsafe-path")
    if path.resolve() != path or (exists and not path.exists()):
        _fail("unsafe-path")
    return path


def _binding(*, dimension, room, profile, workspace):
    _linux()
    for value in (dimension, room, profile):
        if not isinstance(value, str) or not _LABEL.fullmatch(value):
            _fail("invalid-binding")
    workspace = _canonical(workspace)
    if not workspace.is_dir():
        _fail("invalid-workspace")
    return {"dimension": dimension, "room": room, "profile": profile, "workspace": str(workspace)}


def _recipients(values):
    if not isinstance(values, list) or len(values) != 2 or len(set(values)) != 2 or any(not isinstance(value, str) or not re.fullmatch(r"age1[0-9a-z]{58}", value) for value in values):
        _fail("two-native-recipients-required")
    return sorted(values)


def _file(path, maximum=MAX_BYTES):
    _canonical(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > maximum:
        _fail("unsafe-file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        _fail("unsafe-file-permissions")
    return info


def _read(path, maximum=MAX_BYTES):
    before = _file(path, maximum)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
            _fail("source-changed")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    if len(data) > maximum or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        _fail("source-changed")
    return data


def _load(root, binding):
    root = _canonical(root)
    verify_private_path(root, directory=True)
    try:
        document = _json(_read(root / POLICY, 4096))
    except (ValueError, OSError):
        _fail("invalid-profile-policy")
    if not isinstance(document, dict) or set(document) != {"format", "binding", "recipients"} or document.get("format") != FORMAT or document.get("binding") != binding:
        _fail("profile-binding-mismatch")
    _recipients(document["recipients"])
    for name in ("t3", "codex", "claude"):
        verify_private_path(root / name, directory=True)
    return root


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _initialize(root, binding, recipients):
    root.mkdir(mode=0o700)
    for name in ("t3", "codex", "claude"):
        (root / name).mkdir(mode=0o700)
    _write(root / POLICY, json.dumps({"format": FORMAT, "binding": binding, "recipients": recipients}, sort_keys=True).encode())
    _write(root / "operation.lock", b"")


def _admit(binding, recipients, *, root, operation, destination=None):
    from .native_agent_admission import AdmissionError, require_admission

    try:
        return require_admission(binding, recipients, profile_root=root, operation=operation, destination=destination)
    except AdmissionError as error:
        _fail(str(error))


def configure(root, *, recipients=None, **binding):
    binding = _binding(**binding)
    root = _canonical(root, exists=False)
    if root.exists() or root.is_relative_to(Path(binding["workspace"])):
        _fail("profile-target-collision")
    verify_private_path(root.parent, directory=True)
    recipients = _recipients(recipients)
    admission = _admit(binding, recipients, root=root, operation="configure")
    _initialize(root, binding, recipients)
    return {"ok": True, "state": "configured", "admission": admission, **capabilities()}


@contextlib.contextmanager
def _lock(root):
    import fcntl

    path = root / "operation.lock"
    _file(path, 0)
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("profile-busy")
        yield handle.fileno()


def status(root, **binding):
    binding = _binding(**binding)
    root = _load(root, binding)
    state = "configured"
    try:
        with _lock(root):
            pass
    except NativeAgentError:
        state = "busy"
    return {"ok": True, "state": state, "admission": "revalidation-required", **capabilities()}


def launch(root, *, executable="t3", port=3773, **binding):
    binding = _binding(**binding)
    root = _load(root, binding)
    if type(port) is not int or not 1024 <= port <= 65535:
        _fail("invalid-port")
    executable = shutil.which(executable)
    if not executable:
        _fail("provider-missing")
    _admit(binding, _json(_read(root / POLICY, 4096))["recipients"], root=root, operation="launch")
    environment = dict(os.environ)
    environment.update(T3CODE_HOME=str(root / "t3"), CODEX_HOME=str(root / "codex"), CLAUDE_CONFIG_DIR=str(root / "claude"))
    with _lock(root) as lock_fd, sigterm_cancellation():
        process = subprocess.Popen(
            [executable, "serve", "--base-dir", str(root / "t3"), "--host", "127.0.0.1", "--port", str(port), "--no-browser", binding["workspace"]],
            cwd=binding["workspace"], env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock_fd,),
        )
        try:
            code = process.wait()
        finally:
            if process.poll() is None:
                terminate_owned_process(process)
    return {"ok": code == 0, "state": "stopped" if code == 0 else "provider-failed", **capabilities()}


def _validate_rollout(path, data, workspace):
    if not isinstance(path, str) or not _ROLLOUT.fullmatch(path):
        _fail("unsupported-native-path")
    scan = scan_content([data], adapter="codex-native", material_class=MaterialClass.SESSION_TRANSCRIPT, max_bytes=MAX_BYTES)
    if scan.receipt.decision != Decision.ALLOW:
        _fail("unsafe-native-content")
    try:
        rows = data.splitlines()
        first = _json(rows[0])
        if not isinstance(first, dict) or first.get("type") != "session_meta" or not isinstance(first.get("payload"), dict):
            _fail("invalid-native-session")
        if first["payload"]["cwd"] != workspace:
            _fail("path-mismatch")
        identity = first["payload"].get("id")
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity or first["payload"].get("cli_version") != CODEX_VERSION:
            _fail("invalid-native-session")
        for index, row in enumerate(rows):
            item = _json(row)
            if not isinstance(item, dict) or set(item) - {"timestamp", "type", "payload"} or item.get("type") not in {"session_meta", "event_msg", "response_item", "turn_context", "compacted"} or not isinstance(item.get("payload"), dict):
                _fail("invalid-native-session")
            if index and item["type"] == "session_meta":
                _fail("invalid-native-session")
            _inspect_native_values(item)
    except (ValueError, KeyError, IndexError, TypeError):
        _fail("invalid-native-session")


def _inventory(root):
    source = root / "codex"
    inventory = {}
    sessions = source / "sessions"
    if not sessions.exists():
        _fail("native-history-missing")
    for visited, (directory, dirs, files) in enumerate(os.walk(sessions, followlinks=False), start=1):
        if visited > 1024 or len(dirs) + len(files) > 1024:
            _fail("native-history-limit")
        parent = Path(directory)
        _canonical(parent)
        verify_private_path(parent, directory=True)
        for name in dirs:
            verify_private_path(parent / name, directory=True)
            _canonical(parent / name)
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(source).as_posix()
            if not _ROLLOUT.fullmatch(relative):
                _fail("unsupported-native-path")
            info = _file(path)
            inventory[relative] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if sum(value[2] for value in inventory.values()) > MAX_BYTES or len(inventory) > MAX_FILES:
                _fail("native-history-limit")
    if not inventory:
        _fail("native-history-missing")
    return inventory


def _entries(root, binding, barrier, expected):
    if _inventory(root) != expected:
        _fail("source-changed")
    entries = []
    for relative in sorted(expected):
        data = barrier.read(root / "codex" / relative, MAX_BYTES)
        _validate_rollout(relative, data, binding["workspace"])
        entries.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "data": base64.b64encode(data).decode("ascii")})
    if _inventory(root) != expected:
        _fail("source-changed")
    return entries, expected


@contextlib.contextmanager
def _capture_barrier(root):
    expected = _inventory(root)
    try:
        with read_barrier([(root / "codex" / relative, value) for relative, value in sorted(expected.items())]) as barrier:
            if _inventory(root) != expected:
                _fail("source-changed")
            yield barrier, expected
    except QuiescenceError as error:
        _fail(str(error))


def capture(root, *, output, recipients, **binding):
    binding = _binding(**binding)
    root = _load(root, binding)
    output = _canonical(output, exists=False)
    verify_private_path(output.parent, directory=True)
    if output.exists() or output.is_relative_to(root):
        _fail("reference-target-collision")
    recipients = _recipients(recipients)
    if recipients != _json(_read(root / POLICY, 4096))["recipients"]:
        _fail("profile-recipient-mismatch")
    admission = _admit(binding, recipients, root=root, operation="capture", destination=output)
    with _lock(root), sigterm_cancellation(), _capture_barrier(root) as (barrier, expected):
        entries, expected = _entries(root, binding, barrier, expected)
        document = {"format": FORMAT, "binding": binding, "recipients": recipients, "provider": "codex", "mode": "history-only", "entries": entries}
        data = json.dumps(document, separators=(",", ":")).encode()
        fd, temporary = tempfile.mkstemp(prefix=".native-encrypted-", dir=output.parent)
        os.close(fd)
        temporary = Path(temporary)
        args = [str(_managed_executable("age"))]
        for recipient in recipients:
            args.extend(["-r", recipient])
        try:
            with temporary.open("wb") as handle:
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=handle, stderr=subprocess.DEVNULL)
                try:
                    process.communicate(data, timeout=60)
                finally:
                    if process.poll() is None:
                        terminate_owned_process(process)
                if process.returncode:
                    _fail("native-encryption-failed")
                handle.flush()
                os.fsync(handle.fileno())
            if _admit(binding, recipients, root=root, operation="capture", destination=output) != admission:
                _fail("native-admission-changed")
            if _inventory(root) != expected:
                _fail("source-changed")
            barrier.check()
            os.link(temporary, output)
            _fsync_directory(output.parent)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "state": "history-available", "files": len(entries), "native_resume_verified": False}


def _decode(source, identity_paths, binding):
    source = _canonical(source)
    verify_private_path(source.parent, directory=True)
    source_info = _file(source, MAX_BYTES * 2)
    arguments = [str(_managed_executable("age")), "-d"]
    if not identity_paths:
        _fail("identity-required")
    with contextlib.ExitStack() as stack:
        descriptors = []
        for identity in identity_paths:
            identity = _canonical(identity)
            verify_private_path(identity.parent, directory=True)
            identity_info = _file(identity, 65536)
            descriptor = os.open(identity, os.O_RDONLY | os.O_NOFOLLOW)
            stack.callback(os.close, descriptor)
            opened = os.fstat(descriptor)
            if (opened.st_ino, opened.st_dev) != (identity_info.st_ino, identity_info.st_dev):
                _fail("identity-changed")
            descriptors.append(descriptor)
            arguments.extend(["-i", f"/proc/self/fd/{descriptor}"])
        handle = stack.enter_context(os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb"))
        opened = os.fstat(handle.fileno())
        if (opened.st_ino, opened.st_dev) != (source_info.st_ino, source_info.st_dev):
            _fail("source-changed")
        process = subprocess.Popen(arguments, stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, pass_fds=tuple(descriptors))
        try:
            deadline = time.monotonic() + 60
            chunks = []
            size = 0
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        _fail("native-decryption-timeout")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_BYTES * 2:
                        _fail("native-history-limit")
                    chunks.append(chunk)
            data = b"".join(chunks)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())):
                _fail("native-decryption-failed")
        finally:
            if process.poll() is None:
                terminate_owned_process(process)
    try:
        document = _json(data)
        if set(document) != {"format", "binding", "recipients", "provider", "mode", "entries"} or document["format"] != FORMAT or document["provider"] != "codex" or document["mode"] != "history-only":
            _fail("unsupported-recovery-format")
        recipients = _recipients(document["recipients"])
        if document["binding"] != binding:
            _fail("profile-binding-mismatch")
        entries = document["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_FILES:
            _fail("invalid-recovery-manifest")
        decoded = {}
        total = 0
        for entry in entries:
            if set(entry) != {"path", "sha256", "bytes", "data"}:
                _fail("invalid-recovery-manifest")
            content = base64.b64decode(entry["data"], validate=True)
            _validate_rollout(entry["path"], content, binding["workspace"])
            total += len(content)
            if total > MAX_BYTES or entry["path"] in decoded or type(entry["bytes"]) is not int or entry["bytes"] != len(content) or entry["sha256"] != hashlib.sha256(content).hexdigest():
                _fail("invalid-recovery-manifest")
            decoded[entry["path"]] = content
        return decoded, recipients
    except (ValueError, KeyError, TypeError):
        _fail("invalid-recovery-manifest")


def verify(root, *, source, identity_paths, **binding):
    binding = _binding(**binding)
    root = _load(root, binding)
    recipients = _json(_read(root / POLICY, 4096))["recipients"]
    _admit(binding, recipients, root=root, operation="verify", destination=source)
    entries, recovered_recipients = _decode(source, identity_paths, binding)
    if recipients != recovered_recipients:
        _fail("profile-recipient-mismatch")
    return {"ok": True, "state": "history-available", "files": len(entries), "native_resume_verified": False}


def restore(root, *, source, identity_paths, **binding):
    # Named plaintext directories cannot survive SIGKILL safely on ordinary filesystems.
    _fail("crash-safe-materialization-unavailable")
