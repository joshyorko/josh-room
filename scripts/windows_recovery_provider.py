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

    def cli(*args, expected=0):
        (base / "provider-stage.json").write_text(json.dumps({"stage": list(args[:2])}))
        result = subprocess.run([sys.executable, "-m", "josh_room", *args, "--json"],
                                capture_output=True, timeout=120, check=False)
        body = json.loads(result.stdout)
        if (result.returncode == 0) != (expected == 0):
            # No raw controller/provider output in retained diagnostics.
            raise RuntimeError("production CLI failed: " + " ".join(args[:2]))
        return body

    try:
        keyring.store(profile, {"access-key-id": access, "secret-access-key": secret})
        if keyring.secure_lookup(profile, "secret-access-key") != secret:
            raise RuntimeError("native provider credential roundtrip failed")
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
        cli("encryption", "initialize", "--dimension", "recovery-fixture",
            "--recovery-recipient", recipient)
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
        # Prove the installed CLI reads the exact point and refuses overwrite.
        arguments = ["synthetic-room", "--dimension", "recovery-fixture", "--snapshot", pins[0],
                     "--path", "code.py"]
        old = cli("recovery", "inspect", *arguments, "--include-text")
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
            "pins": pins, "cli_no_clobber": True, "native_secret_roundtrips": 4}))
        deadline = time.monotonic() + 600
        while not (base / "stop-provider").exists():
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("owned fixture lifetime ended before acceptance")
            time.sleep(0.2)
        if fingerprint(workspace) != json.loads((base / "provider-before.json").read_text()):
            raise RuntimeError("workspace or Room marker changed")
        # Scan only acceptance logs/profile/evidence, never normal provider data/keysets.
        for directory in (base / "profile", base / "evidence"):
            for candidate in directory.rglob("*"):
                if candidate.is_file() and candidate.stat().st_size <= 8 * 1024 * 1024:
                    data = candidate.read_bytes()
                    if (access.encode() in data or secret.encode() in data
                            or ("AGE" + "-SECRET-KEY-").encode() in data):
                        raise RuntimeError("synthetic provider credential leaked")
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
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        # Never retain raw exception/provider text; even synthetic keys stay private.
        base = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"])
        stage = json.loads((base / "provider-stage.json").read_text()) if (base / "provider-stage.json").exists() else {}
        reason = str(error) if isinstance(error, RuntimeError) and re.fullmatch(r"[A-Za-z0-9 :_-]{1,100}", str(error)) else None
        (base / "provider-failed.json").write_text(json.dumps({"state": "FAIL",
            "exception": type(error).__name__, "boundary": reason, **stage}))
        raise SystemExit(1) from None
