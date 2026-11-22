"""Task-owned real CLI/MinIO check; credentials generated in memory only."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from josh_room import keyring
from josh_room.config import save_private_config


def main():
    # A qualification fixture must never inherit a real provider/identity handoff.
    for name in (
        "JOSH_ROOM_IDENTITY",
        "JOSH_ROOM_ENCRYPTION_MATERIAL",
        "JOSH_ROOM_PROVIDER_CREDENTIALS",
        "JOSH_ROOM_SELECTED_RECIPIENTS",
        "JOSH_ROOM_RECIPIENTS",
        "JOSH_ROOM_DEVICE_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        os.environ.pop(name, None)
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--minio", type=Path, required=True)
    parser.add_argument("--secret-service-bus", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch.resolve(strict=True)
    bus = args.secret_service_bus.resolve(strict=True)
    if scratch not in bus.parents:
        raise ValueError(
            "Secret Service socket must be inside the dedicated task scratch"
        )
    if (
        hashlib.sha256(args.minio.read_bytes()).hexdigest()
        != "7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f"
    ):
        raise ValueError(
            "MinIO binary does not match the repository-pinned synthetic test release"
        )
    base = Path(tempfile.mkdtemp(prefix="provider-qualification-", dir=scratch))
    os.chmod(base, 0o700)
    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    os.environ.update(
        DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(bus),
        JOSH_ROOM_CONFIG_DIR=str(base / "provider-config"),
        JOSH_ROOM_INSTANCE=str(base / "provider-instance"),
        XDG_STATE_HOME=str(base / "provider-state"),
        XDG_CACHE_HOME=str(base / "provider-cache"),
        XDG_RUNTIME_DIR=str(base / "provider-runtime"),
    )
    for name in ("provider-state", "provider-cache", "provider-runtime", "minio-data"):
        (base / name).mkdir(mode=0o700, exist_ok=True)
    assert keyring.backend_status().available
    access = "fixture" + secrets.token_hex(10)
    secret = secrets.token_hex(24)
    env = {
        **os.environ,
        "MINIO_ROOT_USER": access,
        "MINIO_ROOT_PASSWORD": secret,
        "MINIO_BROWSER": "off",
    }
    fd = os.open(base / "minio.log", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        console = sock.getsockname()[1]
    endpoint = "http://127.0.0.1:" + str(port)
    server = subprocess.Popen(
        [
            str(args.minio.resolve()),
            "server",
            str(base / "minio-data"),
            "--address",
            "127.0.0.1:" + str(port),
            "--console-address",
            "127.0.0.1:" + str(console),
            "--quiet",
        ],
        env=env,
        stdout=fd,
        stderr=fd,
    )
    os.close(fd)
    try:
        for attempt in range(100):
            if server.poll() is not None:
                raise RuntimeError("owned MinIO exited before readiness")
            try:
                with urllib.request.urlopen(
                    endpoint + "/minio/health/ready", timeout=1
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("isolated MinIO readiness failed")
        keyring.store(
            "composition-fixture-profile",
            {"access-key-id": access, "secret-access-key": secret},
        )
        del access, secret, env
        save_private_config(
            {
                "connections": {
                    "composition-connection": {
                        "provider": "minio",
                        "endpoint": endpoint,
                        "credential_profile": "composition-fixture-profile",
                        "region": "us-east-1",
                        "path_style": True,
                    }
                },
                "dimensions": {
                    "composition-fixture": {
                        "provider": "minio",
                        "connection": "composition-connection",
                        "bucket": "composition-fixture",
                        "display_name": "Synthetic Composition",
                    }
                },
            }
        )

        def cli(*args):
            result = subprocess.run(
                [sys.executable, "-m", "josh_room", *args, "--json"],
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )

            try:
                body = json.loads(result.stdout)
            except ValueError:
                raise RuntimeError(
                    "CLI entry did not return JSON; exit " + str(result.returncode)
                ) from None
            if result.returncode:
                raise RuntimeError(
                    "CLI failed at "
                    + args[0]
                    + "/"
                    + args[1]
                    + ": "
                    + str(body.get("error", "unknown"))
                )
            return body

        cli(
            "provider",
            "bucket",
            "create",
            "--connection",
            "composition-connection",
            "--bucket",
            "composition-fixture",
        )
        identity = subprocess.run(
            ["age-keygen"], capture_output=True, text=True, check=True
        ).stdout
        recovery = base / "provider-runtime/recovery.identity"
        fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(identity)
        del identity
        try:
            domain = cli(
                "encryption",
                "initialize",
                "--dimension",
                "composition-fixture",
                "--recovery-handoff",
                str(recovery),
            )["encryption_domain_id"]
        finally:
            recovery.unlink(missing_ok=True)
        source = base / "provider-source"
        (source / "projects/a").mkdir(parents=True)
        (source / "b").mkdir()
        (source / "local").mkdir()
        (source / "projects/a/file.txt").write_text("synthetic child A original")
        (source / "b/file.txt").write_text("synthetic child B")
        (source / "local/file.txt").write_text("synthetic local only")
        (source / "notes.txt").write_text("synthetic parent note")
        common = [
            "--dimension",
            "composition-fixture",
            "--namespace",
            "3" * 32,
            "--parent-room",
            "synthetic-parent",
        ]
        selection = [
            "--workspace",
            str(source),
            "--assign",
            ".=local-only",
            "--assign",
            "projects/a=child",
            "--assign",
            "b=child",
            "--assign",
            "notes.txt=parent",
            "--expected-domain",
            domain,
        ]
        plan = cli("composition", "capture-plan", *common, *selection)
        saved = cli(
            "composition",
            "save",
            *common,
            *selection,
            "--approve",
            plan["approval_sha256"],
            "--ack-local-only",
        )
        assert (
            cli(
                "composition",
                "save",
                *common,
                *selection,
                "--approve",
                plan["approval_sha256"],
                "--ack-local-only",
            )["parent_snapshot_id"]
            == saved["parent_snapshot_id"]
        )
        entered = base / "provider-entered"
        cli(
            "composition",
            "restore-parent",
            *common,
            "--parent-snapshot",
            saved["parent_snapshot_id"],
            "--destination",
            str(entered),
        )
        assert (
            not (entered / "projects/a").exists()
            and not (entered / "b").exists()
            and not (entered / "local").exists()
        )
        child = cli(
            "composition",
            "enter-child",
            *common,
            "--parent-snapshot",
            saved["parent_snapshot_id"],
            "--workspace",
            str(entered),
            "--child-path",
            "projects/a",
        )
        assert (
            entered / "projects/a/file.txt"
        ).read_text() == "synthetic child A original"
        assert not (entered / "b").exists()
        assert (source / "local/file.txt").read_text() == "synthetic local only"
        receipt = {
            "ok": True,
            "provider": "loopback-minio",
            "endpoint": "127.0.0.1:" + str(port),
            "namespace": saved["namespace"],
            "parent_pin": saved["parent_snapshot_id"],
            "child_pin": child["snapshot_id"],
            "idempotent_save": True,
            "parent_only_then_whole_child": True,
            "source_unmodified": True,
        }
        receipt["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (base / "loopback-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    finally:
        server.terminate()
        server.wait(timeout=20)


if __name__ == "__main__":
    main()
