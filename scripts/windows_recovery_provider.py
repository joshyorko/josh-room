"""Owned Windows acceptance fixture: real MinIO, production CLI, native vault only.

The caller creates a private task root and sets PYTHONPATH to the installed VSIX.
Only the generated, predeclared vault targets are removed, including on failure.
No provider or UI callbacks are replaced.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from josh_room import keyring
from josh_room.private_paths import protect_private_directory


def fingerprint(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def public_cli_failure(body, *, forbidden=()):
    """Keep only an error code and bounded, shared-policy sanitized diagnostic."""
    from josh_room.cli import _sanitize_human_diagnostic

    result = {}
    code = body.get("error_code")
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", code):
        result["error_code"] = code
    message = body.get("error")
    if isinstance(message, str):
        for value in forbidden:
            message = message.replace(value, "[REDACTED]")
        message = re.sub(r"[A-Za-z]:[\\/][^\s\"'<>]+", "[PRIVATE PATH]", message)
        result["error"] = _sanitize_human_diagnostic(message)[:600]
    return result


def sanitize_ui_stdin():
    """Redact owned native-vault values before UI diagnostics can reach a file."""
    base = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"]).resolve(strict=True)
    targets = json.loads((base / "owned-vault-targets.json").read_text())
    if len(targets) != 4 or not all(
        isinstance(profile, str) and profile.startswith(("recovery-fixture-", "encryption-", "room-store-"))
        for profile, _field in targets
    ):
        raise RuntimeError("owned fixture target ledger invalid")
    forbidden = tuple(keyring.secure_lookup(profile, field) for profile, field in targets)
    message = sys.stdin.buffer.read(16384).decode("utf-8", errors="replace")
    print(json.dumps(public_cli_failure({"error": message}, forbidden=forbidden)))


def main():
    if sys.platform != "win32":
        raise RuntimeError("native Windows is required")
    base = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"]).resolve(strict=True)
    protect_private_directory(base)
    profile = "recovery-fixture-" + uuid.uuid4().hex
    domain = str(uuid.uuid4())
    targets = [(profile, field) for field in ("access-key-id", "secret-access-key")]
    targets += [(f"encryption-{domain}-1", "age-identity"),
                (f"room-store-{domain}-1", "room-store-secret")]
    # Check absence before taking ownership. Never replace an existing target.
    for name, field in targets:
        try:
            keyring.secure_lookup(name, field)
        except keyring.SecureBackendError:
            continue
        raise RuntimeError("fixture vault target already exists")
    (base / "owned-vault-targets.json").write_text(json.dumps(targets))
    status = keyring.backend_status()
    if not status.available or status.backend != "windows-credential-locker":
        raise RuntimeError("Windows Credential Manager unavailable")
    port = int(os.environ["JOSH_ROOM_ACCEPTANCE_PORT"])
    if not 1024 <= port < 65535:
        raise ValueError("invalid loopback fixture port")
    for name in ("provider-config", "provider-state", "provider-cache", "provider-runtime",
                 "minio-data", "provider-workspace"):
        directory = base / name
        directory.mkdir()
        protect_private_directory(directory)
    os.environ.update({"JOSH_ROOM_CONFIG_DIR": str(base / "provider-config"),
                       "XDG_STATE_HOME": str(base / "provider-state"),
                       "XDG_CACHE_HOME": str(base / "provider-cache"),
                       "XDG_RUNTIME_DIR": str(base / "provider-runtime")})
    configuration = {
        "default_dimension": "recovery-fixture", "default_backend": "minio",
        "connections": {"recovery-fixture-connection": {
            "provider": "minio", "display_name": "Synthetic Windows",
            "endpoint": f"http://127.0.0.1:{port}", "credential_profile": profile,
            "region": "us-east-1", "path_style": True}},
        "dimensions": {"recovery-fixture": {"display_name": "Synthetic Recovery",
            "connection": "recovery-fixture-connection", "bucket": "recovery-fixture",
            "catalog_key": "catalog.jroom.age", "encryption_domain_id": domain}}}
    (base / "provider-config/config.json").write_text(json.dumps(configuration))
    access = "fixture" + secrets.token_hex(10)
    secret = secrets.token_hex(24)
    server = None
    progress = {"native_backend": status.backend, "provider_credential_roundtrip": "NOT RUN", "commands": []}

    def cli(*args, expected=0):
        (base / "provider-stage.json").write_text(json.dumps({"stage": list(args[:2])}))
        result = subprocess.run([sys.executable, "-m", "josh_room", *args, "--json"],
                                capture_output=True, timeout=120, check=False)
        body = json.loads(result.stdout)
        if (result.returncode == 0) != (expected == 0):
            (base / "provider-cli-failed.json").write_text(json.dumps({
                "stage": list(args[:2]), "exit_code": result.returncode,
                **public_cli_failure(body, forbidden=(access, secret))}))
            raise RuntimeError("production CLI failed: " + " ".join(args[:2]))
        progress["commands"].append({"command": list(args[:2]), "state": "PASS"})
        (base / "provider-progress.json").write_text(json.dumps(progress))
        return body

    try:
        keyring.store(profile, {"access-key-id": access, "secret-access-key": secret})
        if keyring.secure_lookup(profile, "secret-access-key") != secret:
            raise RuntimeError("native provider credential roundtrip failed")
        progress["provider_credential_roundtrip"] = "PASS"
        (base / "provider-progress.json").write_text(json.dumps(progress))
        with (base / "minio.log").open("wb") as log:
            server = subprocess.Popen([str(base / "tools/minio.exe"), "server",
                str(base / "minio-data"), "--address", f"127.0.0.1:{port}",
                "--console-address", f"127.0.0.1:{port + 1}", "--quiet"],
                env={**os.environ, "MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret,
                     "MINIO_BROWSER": "off"}, stdout=log, stderr=log)
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/minio/health/ready",
                                            timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("owned MinIO readiness deadline")
        cli("provider", "bucket", "create", "--connection", "recovery-fixture-connection",
            "--bucket", "recovery-fixture")
        identity = subprocess.run(["age-keygen"], capture_output=True, check=True).stdout
        recipient = subprocess.run(["age-keygen", "-y"], input=identity,
                                   capture_output=True, check=True).stdout.decode().strip()
        del identity
        existing_handoff = base / "provider-runtime/existing-operational.identity"
        cli("encryption", "initialize", "--dimension", "recovery-fixture",
            "--recovery-recipient", recipient, "--material-handoff", str(existing_handoff))
        workspace = base / "provider-workspace"
        (workspace / "nested").mkdir()
        (workspace / "nested/配置.txt").write_bytes(b"synthetic nested\n")
        (workspace / "unrelated.bin").write_bytes(os.urandom(1024 * 1024))
        pins = []
        for content in ("first", "second", "third"):
            (workspace / "code.py").write_bytes(
                f'def greeting():\n\treturn "{content} 你好"\n'.encode())
            pins.append(cli("snapshot", "create", "synthetic-room", "--dimension",
                "recovery-fixture", "--backend", "minio", "--source", str(workspace))["snapshot_id"])
        cli("dimensions", "list")
        hierarchy = cli("dimensions", "list", "--with-hierarchy", "--dimension", "recovery-fixture")
        if pins[0] not in json.dumps(hierarchy):
            raise RuntimeError("catalog hierarchy is missing historical pin")
        # Prove the installed CLI reads the exact point and refuses overwrite.
        arguments = ["synthetic-room", "--dimension", "recovery-fixture", "--snapshot", pins[0],
                     "--path", "code.py"]
        os.environ["JOSH_ROOM_ENCRYPTION_MATERIAL"] = str(existing_handoff)
        try:
            old = cli("recovery", "inspect", *arguments, "--include-text")
        finally:
            os.environ.pop("JOSH_ROOM_ENCRYPTION_MATERIAL", None)
        expected = 'def greeting():\n\treturn "first 你好"\n'.encode()
        if old.get("sha256") != hashlib.sha256(expected).hexdigest():
            raise RuntimeError("installed CLI historical digest mismatch")
        existing = base / "existing.txt"
        existing.write_bytes(b"preserved destination")
        cli("recovery", "extract", *arguments, "--destination", str(existing), expected=1)
        if existing.read_bytes() != b"preserved destination":
            raise RuntimeError("existing destination modified")
        if not keyring.lookup_encryption_identity(domain, 1):
            raise RuntimeError("native operational identity missing")
        if not keyring.lookup_room_store_secret(domain, 1):
            raise RuntimeError("native Room Store secret missing")
        if not (workspace / ".josh-room.json").is_file():
            raise RuntimeError("synthetic Room marker missing")
        (base / "provider-before.json").write_text(json.dumps(fingerprint(workspace)))
        (base / "provider-pins.json").write_text(json.dumps(pins))
        (base / "provider-ready.json").write_text(json.dumps({"state": "PASS",
            "storage": "real-loopback-MinIO", "custody": status.backend,
            "controller": "installed-source-Python-fallback-not-immutable-RCC",
            "encryption_domain_id": domain, "key_generation": 1, "existing_private_handoff_cli": True,
            "pins": pins, "cli_no_clobber": True, "native_secret_roundtrips": 4}))
        deadline = time.monotonic() + 600
        while not (base / "stop-provider").exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("owned fixture lifetime ended before acceptance")
            time.sleep(0.2)
        (base / "provider-stage.json").write_text(json.dumps({"stage": ["verify", "workspace-inventory"]}))
        if fingerprint(workspace) != json.loads((base / "provider-before.json").read_text()):
            raise RuntimeError("workspace or Room marker changed")
        (base / "provider-stage.json").write_text(json.dumps({"stage": ["verify", "profile-logs"]}))
        # Log/privacy evidence does not require reading live SQLite/lock files.
        for directory in (base / "profile/logs", base / "evidence"):
            for candidate in directory.rglob("*"):
                if candidate.is_file() and candidate.stat().st_size <= 8 * 1024 * 1024:
                    data = candidate.read_bytes()
                    if (access.encode() in data or secret.encode() in data
                            or ("AGE" + "-SECRET-KEY-").encode() in data):
                        raise RuntimeError("synthetic provider credential leaked")
        # Only this owned extension's existing log, after exact native-secret
        # removal and the shared CLI sanitizer. No raw output is copied.
        forbidden = (access, secret, keyring.lookup_room_store_secret(domain, 1))
        diagnostics = []
        for candidate in (base / "profile").rglob("*.log"):
            if "josh" not in candidate.name.lower() or candidate.stat().st_size > 1024 * 1024:
                continue
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]:
                if any(label in line for label in ("START", "DONE", "[error]", "[warning]")):
                    diagnostics.append(public_cli_failure({"error": line}, forbidden=forbidden))
        (base / "ui-controller-summary.json").write_text(json.dumps({"messages": diagnostics[-24:]}))
        (base / "preservation.json").write_text(json.dumps({"state": "PASS",
            "workspace_and_marker_unchanged": True, "provider_credentials_absent_from_logs": True}))
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait(timeout=15)
        for name, field in targets:
            keyring.secure_delete(name, field)
        for name, field in targets:
            try:
                keyring.secure_lookup(name, field)
            except keyring.SecureBackendError:
                continue
            raise RuntimeError("owned vault target survived cleanup")
        (base / "custody-cleanup.json").write_text(json.dumps({"state": "PASS",
            "deleted_owned_native_targets": len(targets), "unrelated_targets_touched": 0}))


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--sanitize-ui"]:
            sanitize_ui_stdin()
        else:
            main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        # Never retain raw exception/provider text; even synthetic keys stay private.
        if sys.argv[1:] == ["--sanitize-ui"]:
            raise SystemExit(1) from None
        base = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"])
        stage = json.loads((base / "provider-stage.json").read_text()) if (base / "provider-stage.json").exists() else {}
        reason = str(error) if isinstance(error, RuntimeError) and re.fullmatch(r"[A-Za-z0-9 :_-]{1,100}", str(error)) else None
        (base / "provider-failed.json").write_text(json.dumps({"state": "FAIL",
            "exception": type(error).__name__, "boundary": reason,
            "errno": getattr(error, "errno", None), "winerror": getattr(error, "winerror", None), **stage}))
        raise SystemExit(1) from None
