"""Host admission for a single temporary, network-isolated T3 import job."""

from __future__ import annotations

import ctypes
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

from . import native_agents as native
from .cancellation import CLICancelled, sigterm_cancellation, terminate_owned_process

_LIMIT = 64 * 1024
_ERROR = re.compile(r"[a-z][a-z0-9-]{0,127}\Z")
_UUID = re.compile(native._UUID + r"\Z")
_ADMISSION_FIELDS = {"admitted", "authority_sha256", "tool", "scope", "destination", "key_generation", "native_resume_verified"}


def _fail(code="t3-import-job-refused", *, committed=False):
    raise native.NativeAgentError(code, committed=committed)


def _send(stream, message, deadline):
    encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode()
    if len(encoded) > _LIMIT:
        _fail()
    descriptor = stream.fileno()
    os.set_blocking(descriptor, False)
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_WRITE)
        while encoded:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                _fail("t3-import-job-timeout")
            try:
                written = os.write(descriptor, encoded)
            except BlockingIOError:
                continue
            encoded = encoded[written:]


def _receive(stream, buffer, deadline):
    descriptor = stream.fileno()
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                _fail("t3-import-job-timeout")
            data = os.read(descriptor, _LIMIT + 1 - len(buffer))
            if not data:
                _fail("t3-import-job-exited")
            buffer.extend(data)
            if len(buffer) > _LIMIT:
                _fail("t3-import-job-protocol-invalid")
    line, _, rest = buffer.partition(b"\n")
    buffer[:] = rest
    value = json.loads(line)
    if not isinstance(value, dict):
        _fail("t3-import-job-protocol-invalid")
    return value


def _system_unshare():
    command = shutil.which("unshare", path=os.defpath)
    if not command:
        _fail("t3-import-isolation-unavailable")
    command = Path(command).resolve()
    for path in (command, *command.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            _fail("t3-import-isolation-unavailable")
    return str(command)


def _environment(root):
    environment = {
        "HOME": str(root), "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1",
    }
    temporary = os.environ.get("TMPDIR")
    if temporary:
        path = Path(temporary)
        if path.is_absolute() and path.resolve() == path:
            info = path.stat()
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077:
                environment["TMPDIR"] = temporary
    return environment


def _start_worker(root):
    return subprocess.Popen(
        [_system_unshare(), "-Urn", sys.executable, "-P", "-s", "-B", "-m", __name__, "--worker"],
        env=_environment(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True, cwd="/",
    )


def _public_admission(receipt):
    if (not isinstance(receipt, dict) or receipt.get("admitted") is not True
            or not isinstance(receipt.get("authority_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["authority_sha256"])):
        _fail("t3-import-job-admission-invalid")
    if (("tool" in receipt and receipt["tool"] not in {"codex", "claude"})
            or ("scope" in receipt and receipt["scope"] not in {"personal", "work", "oss", "homelab"})
            or ("destination" in receipt and receipt["destination"] != "local-only")
            or ("key_generation" in receipt and (type(receipt["key_generation"]) is not int or receipt["key_generation"] < 1))
            or ("native_resume_verified" in receipt and receipt["native_resume_verified"] is not False)):
        _fail("t3-import-job-admission-invalid")
    return {key: value for key, value in receipt.items() if key in _ADMISSION_FIELDS}


def _result(message, tool, may_import):
    if set(message) != {"kind", "result"} or not isinstance(message["result"], dict):
        _fail("t3-import-job-protocol-invalid")
    result = message["result"]
    if result.get("ok") is not True:
        if set(result) != {"ok", "error", "committed", "cancelled"} or not _ERROR.fullmatch(result.get("error", "")):
            _fail("t3-import-job-protocol-invalid")
        if may_import or result["committed"] is True:
            _fail("t3-import-outcome-uncertain", committed=True)
        if result["cancelled"] is True:
            raise CLICancelled
        _fail(result["error"])
    expected = {"ok", "state", "tool", "imported_count", "sessions", "cwd_matches", "runnable_effects", "original_t3_identity_restored", "native_resume_verified"}
    if (set(result) != expected or not may_import or result["state"] != "history-imported"
            or result["tool"] != tool or result["cwd_matches"] is not True
            or result["runnable_effects"] != 0 or result["original_t3_identity_restored"] is not False
            or result["native_resume_verified"] is not False or type(result["imported_count"]) is not int
            or not isinstance(result["sessions"], list) or not 1 <= len(result["sessions"]) <= native.MAX_FILES
            or result["imported_count"] != len(result["sessions"])):
        _fail("t3-import-job-protocol-invalid")
    identities = set()
    driver = "codex" if tool == "codex" else "claudeAgent"
    for session in result["sessions"]:
        if not isinstance(session, dict) or set(session) != {"provider_session_id", "new_t3_thread_id"}:
            _fail("t3-import-job-protocol-invalid")
        identity = session["provider_session_id"]
        if not isinstance(identity, str) or not _UUID.fullmatch(identity) or identity in identities or session["new_t3_thread_id"] != "import:" + driver + ":" + identity:
            _fail("t3-import-job-protocol-invalid")
        identities.add(identity)
    return result


def import_history(root, *, executable, tool="codex", **binding):
    native._linux()
    tool = native._tool(tool)
    binding = native._binding(**binding)
    root = native._load(root, binding)
    policy = native._read(root / native.POLICY, 4096)
    recipients = native._json(policy)["recipients"]
    baseline = None

    def admit():
        native._load(root, binding)
        if native._read(root / native.POLICY, 4096) != policy:
            _fail("native-admission-changed")
        receipt = _public_admission(native._admit(binding, recipients, root=root, operation="import-history", tool=tool))
        if baseline is not None and receipt != baseline:
            _fail("native-admission-changed")
        return receipt

    baseline = admit()
    process = None
    may_import = False
    try:
        with sigterm_cancellation():
            process = _start_worker(root)
            deadline = time.monotonic() + 180
            _send(process.stdin, {"root": str(root), "executable": str(executable), "tool": tool, "binding": binding, "parent_pid": os.getpid()}, deadline)
            buffer = bytearray()
            for _ in range(128):
                message = _receive(process.stdout, buffer, deadline)
                if message == {"kind": "admit"}:
                    try:
                        response = {"ok": True, "receipt": admit()}
                    except native.NativeAgentError as error:
                        response = {"ok": False, "error": str(error) if _ERROR.fullmatch(str(error)) else "native-admission-unavailable"}
                    _send(process.stdin, response, deadline)
                elif message == {"kind": "may-import"}:
                    may_import = True
                    _send(process.stdin, {"ok": True}, deadline)
                elif message.get("kind") == "result":
                    result = _result(message, tool, may_import)
                    if process.wait(timeout=10) != 0:
                        _fail("t3-import-job-exited")
                    admit()
                    return result
                else:
                    _fail("t3-import-job-protocol-invalid")
            _fail("t3-import-job-protocol-invalid")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError, KeyboardInterrupt, CLICancelled) as error:
        if may_import:
            _fail("t3-import-outcome-uncertain", committed=True)
        if isinstance(error, (native.NativeAgentError, KeyboardInterrupt, CLICancelled)):
            raise
        raise native.NativeAgentError("t3-import-job-refused") from error
    finally:
        if process is not None:
            if process.poll() is None:
                terminate_owned_process(process, grace_seconds=8)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def _loopback_up():
    import fcntl

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        current = fcntl.ioctl(control.fileno(), 0x8913, struct.pack("16sh", b"lo", 0))
        flags = struct.unpack("16sh", current[:18])[1]
        fcntl.ioctl(control.fileno(), 0x8914, struct.pack("16sh", b"lo", flags | 1))


def _worker():
    from . import native_agent_t3_import as backend

    deadline = time.monotonic() + 180
    buffer = bytearray()
    config = _receive(sys.stdin.buffer, buffer, deadline)
    if set(config) != {"root", "executable", "tool", "binding", "parent_pid"} or type(config["parent_pid"]) is not int:
        _fail("t3-import-job-protocol-invalid")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != config["parent_pid"]:
        _fail("t3-import-job-parent-lost")
    _loopback_up()
    backend._isolated_network()
    root = Path(config["root"])
    recipients = native._json(native._read(root / native.POLICY, 4096))["recipients"]

    def admit(binding, actual_recipients, *, root: Path, operation, tool, **extra):
        if (binding != config["binding"] or actual_recipients != recipients or str(root) != config["root"]
                or operation != "import-history" or tool != config["tool"] or extra):
            _fail("t3-import-job-admission-invalid")
        _send(sys.stdout.buffer, {"kind": "admit"}, deadline)
        reply = _receive(sys.stdin.buffer, buffer, deadline)
        if reply.get("ok") is not True:
            code = reply.get("error")
            _fail(code if isinstance(code, str) and _ERROR.fullmatch(code) else "native-admission-unavailable")
        if set(reply) != {"ok", "receipt"}:
            _fail("t3-import-job-protocol-invalid")
        return _public_admission(reply["receipt"])

    rpc = backend._rpc

    def guarded_rpc(*arguments, **keywords):
        if arguments[5] == "agentSessions.import":
            _send(sys.stdout.buffer, {"kind": "may-import"}, deadline)
            if _receive(sys.stdin.buffer, buffer, deadline) != {"ok": True}:
                _fail("t3-import-job-protocol-invalid")
        return rpc(*arguments, **keywords)

    native._admit = admit
    backend._rpc = guarded_rpc
    return backend.import_history(root, executable=config["executable"], tool=config["tool"], **config["binding"])


def main():
    if sys.argv[1:] != ["--worker"]:
        return 2
    try:
        with sigterm_cancellation():
            result = _worker()
    except (KeyboardInterrupt, CLICancelled):
        result = {"ok": False, "error": "t3-import-job-cancelled", "committed": False, "cancelled": True}
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        result = {"ok": False, "error": str(error) if isinstance(error, native.NativeAgentError) and _ERROR.fullmatch(str(error)) else "t3-import-job-refused", "committed": isinstance(error, native.NativeAgentError) and error.committed, "cancelled": False}
    try:
        _send(sys.stdout.buffer, {"kind": "result", "result": result}, time.monotonic() + 5)
    except (OSError, RuntimeError, ValueError):
        return 1
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
