"""Disposable real provider/keyring enrollment. Credentials stay in the disposable OS keyring."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from josh_room import keyring

base = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"]).absolute()
if not base.is_dir() or (base / "provider-workspace").exists():
    raise RuntimeError(
        "acceptance requires a task directory without a previous provider workspace"
    )
port = int(os.environ.get("JOSH_ROOM_ACCEPTANCE_PORT", "19400"))
if not 1024 <= port <= 65534:
    raise ValueError("invalid fixture port")
if (
    hashlib.sha256((base / "minio").read_bytes()).hexdigest()
    != "7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f"
):
    raise RuntimeError("MinIO release hash mismatch")
config_dir = base / "provider-config"
config_dir.mkdir(mode=0o700, exist_ok=True)
configuration = {
    "default_dimension": "recovery-fixture",
    "default_backend": "minio",
    "connections": {
        "recovery-fixture-connection": {
            "provider": "minio",
            "display_name": "Synthetic Local",
            "endpoint": f"http://127.0.0.1:{port}",
            "credential_profile": "recovery-fixture-profile",
            "region": "us-east-1",
            "path_style": True,
        }
    },
    "dimensions": {
        "recovery-fixture": {
            "display_name": "Synthetic Recovery",
            "connection": "recovery-fixture-connection",
            "bucket": "recovery-fixture",
            "catalog_key": "catalog.jroom.age",
        }
    },
}
config_file = config_dir / "config.json"
with config_file.open("x", encoding="utf-8") as stream:
    os.chmod(config_file, 0o600)
    json.dump(configuration, stream)
os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(
    base / "keyring-runtime/bus"
)
os.environ["JOSH_ROOM_CONFIG_DIR"] = str(base / "provider-config")
os.environ["XDG_STATE_HOME"] = str(base / "provider-state")
os.environ["XDG_CACHE_HOME"] = str(base / "provider-cache")
os.environ["XDG_RUNTIME_DIR"] = str(base / "provider-runtime")
for name in (
    "provider-state",
    "provider-cache",
    "provider-runtime",
    "minio-data",
    "provider-workspace",
):
    (base / name).mkdir(mode=0o700, exist_ok=True)
assert keyring.backend_status().available, keyring.backend_status().reason
access = "fixture" + secrets.token_hex(10)
secret = secrets.token_hex(24)
env = {
    **os.environ,
    "MINIO_ROOT_USER": access,
    "MINIO_ROOT_PASSWORD": secret,
    "MINIO_BROWSER": "off",
}
fd = os.open(base / "minio.log", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
minio = subprocess.Popen(
    [
        str(base / "minio"),
        "server",
        str(base / "minio-data"),
        "--address",
        f"127.0.0.1:{port}",
        "--console-address",
        f"127.0.0.1:{port + 1}",
        "--quiet",
    ],
    env=env,
    stdout=fd,
    stderr=fd,
)
os.close(fd)
(base / "minio.pid").write_text(str(minio.pid))
try:
    for attempt in range(100):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/minio/health/ready", timeout=1
            ) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("synthetic MinIO readiness failed")
    keyring.store(
        "recovery-fixture-profile",
        {"access-key-id": access, "secret-access-key": secret},
    )
    del access, secret, env
    python = sys.executable
    log = []

    def cli(*args):
        result = subprocess.run(
            [python, "-m", "josh_room", *args, "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            body = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("controller returned non-JSON") from None
        if result.returncode:
            print(json.dumps({"stage": args[0:2], "result": body}))
            raise RuntimeError("controller failed")
        log.append(
            {
                "command": list(args[:2]),
                "ok": body.get("ok"),
                "snapshot_id": body.get("snapshot_id"),
                "state": body.get("state"),
            }
        )
        return body

    cli(
        "provider",
        "bucket",
        "create",
        "--connection",
        "recovery-fixture-connection",
        "--bucket",
        "recovery-fixture",
    )
    identity = subprocess.run(
        ["age-keygen"], capture_output=True, text=True, check=True
    ).stdout
    keyring.store_value("recovery-fixture-recovery", "age-identity", identity)
    handoff = base / "provider-runtime/recovery.identity"
    fd = os.open(handoff, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(identity)
    del identity
    try:
        cli(
            "encryption",
            "initialize",
            "--dimension",
            "recovery-fixture",
            "--recovery-handoff",
            str(handoff),
        )
    finally:
        handoff.unlink(missing_ok=True)
    workspace = base / "provider-workspace"
    (workspace / "nested").mkdir(exist_ok=True)
    (workspace / "nested/配置.txt").write_bytes(b"synthetic nested")
    (workspace / "large.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    pins = []
    for content in (b"first\n", b"second\n", b"third\n"):
        (workspace / "changed.txt").write_bytes(content)
        (workspace / "code.py").write_bytes(
            ('def greeting():\n\treturn "' + content.decode().strip() + ' 你好"\n').encode()
        )
        pins.append(
            cli(
                "snapshot",
                "create",
                "synthetic-room",
                "--dimension",
                "recovery-fixture",
                "--backend",
                "minio",
                "--source",
                str(workspace),
            )["snapshot_id"]
        )
    (base / "provider-pins.json").write_text(json.dumps(pins))
    (base / "provider-before.json").write_text(
        json.dumps(
            {
                p.relative_to(workspace).as_posix(): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in workspace.rglob("*")
                if p.is_file()
            }
        )
    )
    (base / "provider-setup-receipt.json").write_text(
        json.dumps(
            {
                "provider": "minio",
                "isolated_secret_service": True,
                "pins": pins,
                "commands": log,
            },
            indent=2,
        )
    )
    print(
        json.dumps({"ok": True, "pins": pins, "isolated_secret_service": True}),
        flush=True,
    )
    minio.wait()
finally:
    if minio.poll() is None:
        minio.terminate()
        minio.wait()
