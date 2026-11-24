"""Container-only synthetic enrollment and read-only/restore postconditions.

Run with the installed controller's managed environment. No credentials leave
this process/keyring and no real provider or inherited host configuration exists.
"""
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
from josh_room.config import save_private_config

ROOT = Path("/pilot")
DIMENSION = "pilot-synthetic"


def cli(*args):
    result = subprocess.run([sys.executable, "-m", "josh_room", *args, "--json"], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("Fixture CLI failed: " + "/".join(args[:2]))
    return json.loads(result.stdout)


def git_state(root):
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return [subprocess.check_output(["git", "-C", str(root), *probe], text=True, env=environment)
            for probe in [("rev-parse", "HEAD"), ("ls-files", "--stage"), ("status", "--porcelain=v1")]]


def enroll():
    assert hashlib.sha256((ROOT / "minio").read_bytes()).hexdigest() == "7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f"
    assert keyring.backend_status().available
    access, secret = "fixture" + secrets.token_hex(10), secrets.token_hex(24)
    environment = {**os.environ, "MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret, "MINIO_BROWSER": "off"}
    log = (ROOT / "minio.log").open("wb")
    service = subprocess.Popen([str(ROOT / "minio"), "server", "/pilot/minio-data", "--address", "127.0.0.1:19400",
                                "--console-address", "127.0.0.1:19401", "--quiet"], env=environment, stdout=log, stderr=log)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen("http://127.0.0.1:19400/minio/health/ready", timeout=1) as reply:
                    if reply.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Synthetic MinIO readiness")
        keyring.store("pilot-synthetic-profile", {"access-key-id": access, "secret-access-key": secret})
        del access, secret, environment
        save_private_config({"default_dimension": DIMENSION, "default_backend": "minio", "connections": {
            "pilot-synthetic-connection": {"provider": "minio", "endpoint": "http://127.0.0.1:19400", "credential_profile": "pilot-synthetic-profile", "region": "us-east-1", "path_style": True}},
            "dimensions": {DIMENSION: {"provider": "minio", "connection": "pilot-synthetic-connection", "bucket": DIMENSION, "display_name": "Synthetic Catalog Pilot"}}})
        cli("provider", "bucket", "create", "--connection", "pilot-synthetic-connection", "--bucket", DIMENSION)
        recovery = ROOT / "keyring-runtime/recovery.identity"
        fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                subprocess.run(["age-keygen"], stdout=stream, stderr=subprocess.DEVNULL, check=True)
            status = cli("encryption", "initialize", "--dimension", DIMENSION, "--recovery-handoff", str(recovery))
        finally:
            recovery.unlink(missing_ok=True)
        (ROOT / "source-before.json").write_text(json.dumps({"git": git_state(ROOT / "workspace/a"),
            "readme_sha256": hashlib.sha256((ROOT / "workspace/a/README.md").read_bytes()).hexdigest()}))
        (ROOT / "provider-ready.json").write_text(json.dumps({"ok": True, "provider": "loopback-minio", "keyring": "private-native-secret-service", "encryption_domain_id": status["encryption_domain_id"]}))
        while not (ROOT / "stop-provider").exists():
            time.sleep(0.5)
    finally:
        service.terminate()
        service.wait(timeout=10)
        log.close()


def cold_restore():
    point = json.loads((ROOT / "ui-recovery-receipt.json").read_text())
    assert point["dimension_id"] == DIMENSION
    parent, snapshot, namespace = point["parent_room_id"], point["parent_snapshot_id"], point["namespace"]
    assert parent == "catalog-" + namespace
    destination = ROOT / "cold/parent"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    assert not destination.exists()
    cli("composition", "restore-parent", "--dimension", DIMENSION, "--namespace", namespace, "--parent-room", parent,
        "--parent-snapshot", snapshot, "--destination", str(destination))
    assert not (destination / "a").exists() and not (destination / "projects/a").exists()
    children = cli("composition", "parent-children", "--dimension", DIMENSION, "--namespace", namespace,
        "--parent-room", parent, "--parent-snapshot", snapshot, "--workspace", str(destination))["children"]
    assert len(children) == 1 and children[0]["path"] in {"a", "projects/a"}
    receipt = {"ok": True, "parent_snapshot": snapshot, "namespace": namespace, "parent_room": parent,
               "child_snapshot": children[0]["snapshot_id"], "child_path": children[0]["path"], "cold_children_absent": True,
               "fixture_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "boundary": "normal managed CLI cold parent restore; child awaits installed UI"}
    (ROOT / "cold-receipt.json").write_text(json.dumps(receipt))
    print(json.dumps(receipt))


def verify_child():
    receipt = json.loads((ROOT / "cold-receipt.json").read_text())
    baseline = json.loads((ROOT / "source-before.json").read_text())
    child = ROOT / "cold/parent" / receipt["child_path"]
    assert hashlib.sha256((child / "README.md").read_bytes()).hexdigest() == baseline["readme_sha256"]
    assert git_state(child) == baseline["git"]
    assert hashlib.sha256((ROOT / "workspace/a/README.md").read_bytes()).hexdigest() == baseline["readme_sha256"]
    assert git_state(ROOT / "workspace/a") == baseline["git"]
    assert not (ROOT / "cold/parent/b").exists() and not (ROOT / "cold/parent/projects/b").exists()
    marker = json.loads((child / ".josh-room.json").read_text())
    assert marker["snapshot_id"] == receipt["child_snapshot"]
    subprocess.run(["git", "-C", str(child), "fsck", "--full"], capture_output=True, check=True)
    receipt.update(child_pin_verified=True, child_bytes_verified=True, git_identity_verified=True,
                   source_readme_and_git_unchanged=True, readme_sha256=baseline["readme_sha256"], sibling_absent=True)
    (ROOT / "hydration-receipt.json").write_text(json.dumps(receipt))
    print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        {"enroll": enroll, "cold_restore": cold_restore, "verify_child": verify_child}[sys.argv[1]]()
    except Exception as error:
        (ROOT / "fixture-failure.json").write_text(json.dumps({"ok": False, "stage": sys.argv[1], "type": type(error).__name__, "diagnostic": str(error)[:200]}))
        raise
