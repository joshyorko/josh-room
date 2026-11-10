#!/usr/bin/env python3
"""Disposable two-node celld/MinIO contract, crash and cold-restore qualification."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def token(key, domain, access="write", expired=False):
    now = int(time.time())
    payload = b64(json.dumps({"domain": domain, "access": access, "iat": now - 2, "exp": now - 1 if expired else now + 600}).encode())
    return payload + "." + b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--celld", required=True)
    parser.add_argument("--minio", required=True)
    parser.add_argument("--esbuild", required=True)
    parser.add_argument("--scratch-parent", default=os.environ.get("TMPDIR", "/tmp"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    processes = []
    with tempfile.TemporaryDirectory(prefix="codex-room-store-fleet-", dir=args.scratch_parent) as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        def start(command, env, name):
            log = (directory / f"{name}.log").open("wb")
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            log.close()
            processes.append(process)
            return process

        def ready(url, process):
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("disposable service exited before readiness")
                try:
                    with urlopen(url, timeout=1) as response:
                        if response.status == 200:
                            return
                except (OSError, URLError):
                    time.sleep(0.1)
            raise RuntimeError("disposable service readiness timed out")

        try:
            minio_port = port()
            endpoint = f"http://127.0.0.1:{minio_port}"
            access, secret = secrets.token_hex(12), secrets.token_hex(24)
            env = {**os.environ, "MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret, "MINIO_BROWSER": "off"}
            minio = start([args.minio, "server", str(directory / "minio"), "--address", f"127.0.0.1:{minio_port}", "--console-address", f"127.0.0.1:{port()}"], env, "minio")
            ready(endpoint + "/minio/health/ready", minio)
            client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access, aws_secret_access_key=secret, region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
            bucket = "disposable-control"
            client.create_bucket(Bucket=bucket)
            signing_key, domain = secrets.token_bytes(32), "c" * 64
            config = json.loads((root / "worker/celld/wrangler.jsonc").read_text())
            (directory / "celld").mkdir()
            (directory / "src").mkdir()
            for relative in ("celld/index.js", "src/room-store.js"):
                shutil.copyfile(root / "worker" / relative, directory / relative)
                assert (root / "worker" / relative).read_bytes() == (directory / relative).read_bytes()
            config["main"] = "celld/index.js"
            config["vars"] = {"ROOM_STORE_EXPERIMENTAL": "enabled", "ROOM_STORE_DOMAIN_ID": domain, "ROOM_STORE_SIGNING_KEY": b64(signing_key)}
            config_path = directory / "wrangler.json"
            config_path.write_text(json.dumps(config))
            os.chmod(config_path, 0o600)
            env = {**os.environ, "AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret, "AWS_REGION": "us-east-1", "CELLD_ESBUILD": args.esbuild, "CELLD_READY_FLEET_GATE_MS": "0", "CELLD_DURABILITY": "bucket", "CELLD_REBALANCE_INTERVAL_MS": "0"}
            common = ["--bucket", f"s3://{bucket}", "--endpoint", endpoint, "--region", "us-east-1"]
            deployed = subprocess.run([args.celld, "deploy", str(config_path), *common], env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            (directory / "deploy.log").write_bytes(deployed.stdout)
            if deployed.returncode:
                raise RuntimeError("disposable celld deployment failed: " + deployed.stdout.decode(errors="replace")[-2000:])
            nodes = []
            for index in range(2):
                public, internal = port(), port()
                node_env = {**env, "CELLD_WATCH": str(directory / f"node-{index}")}
                command = [args.celld, *common, "--listen", f"127.0.0.1:{public}", "--internal-listen", f"127.0.0.1:{internal}", "--advertise", f"127.0.0.1:{internal}"]
                process = start(command, node_env, f"node-{index}")
                ready(f"http://127.0.0.1:{public}/.well-known/celld/health", process)
                nodes.append((public, process))

            def call(method, operation, body=None, mode="write", node=0):
                current_domain = config["vars"]["ROOM_STORE_DOMAIN_ID"]
                capability = token(signing_key, "d" * 64 if mode == "wrong-domain" else current_domain, "read" if mode == "read" else "write", mode == "expired")
                req = Request(f"http://127.0.0.1:{nodes[node][0]}/room-store/{current_domain}/{operation}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"authorization": f"Bearer {capability}", "content-type": "application/json"})
                try:
                    response = urlopen(req, timeout=30)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.load(response)

            contract_env = {**os.environ, "ROOM_STORE_TEST_KEY": b64(signing_key), "ROOM_STORE_TEST_DOMAIN": domain, "ROOM_STORE_TEST_ENDPOINTS": json.dumps([f"http://127.0.0.1:{public}" for public, _process in nodes])}
            subprocess.run(["node", str(root / "scripts/portable-room-store/remote-contract.mjs")], env=contract_env, check=True, timeout=60)
            bound = call("GET", "material")[1]["material"]
            for _public, process in nodes:
                process.kill()
                process.wait(timeout=10)
            for index in range(2):
                shutil.rmtree(directory / f"node-{index}", ignore_errors=True)
            time.sleep(11)
            public, internal = port(), port()
            process = start([args.celld, *common, "--listen", f"127.0.0.1:{public}", "--internal-listen", f"127.0.0.1:{internal}", "--advertise", f"127.0.0.1:{internal}"], {**env, "CELLD_WATCH": str(directory / "cold-node")}, "cold-node")
            nodes[0] = (public, process)
            ready(f"http://127.0.0.1:{public}/.well-known/celld/health", process)
            assert call("GET", "material")[1]["material"] == bound
            assert call("POST", "repository", {"repositoryId": bound["repositoryId"], "expectedGeneration": 1})[0] == 200
            assert call("POST", "seal", {}) == (200, {"status": "sealed"})
            assert call("POST", "repository", {"repositoryId": bound["repositoryId"], "expectedGeneration": 1})[0] == 409
            assert call("GET", "material")[1]["material"] == bound
            print("PASS SIGKILL all nodes, remove local caches, restore from MinIO alone, permanent rollback fence", flush=True)
            migration_contract(args, directory, root, config, config_path, env, common, client, endpoint, access, secret, signing_key, call)

        except Exception:
            for path in directory.glob("*.log"):
                # Runtime logs are kept private and never copied to repository evidence.
                print(f"Diagnostic {path.name}: {path.stat().st_size} bytes", flush=True)
            raise
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=8)


def migration_contract(args, directory, root, config, config_path, env, common, client, endpoint, access, secret, signing_key, call):
    from unittest.mock import patch

    from josh_room import auth, crypto
    from josh_room import portable_room_store as portable
    from josh_room.config import DimensionConfig
    from josh_room.encryption_domain import (
        KEYSET_CONTROL_KEY,
        EncryptionKeyset,
        EncryptionMaterial,
    )
    from josh_room.minio import MinioBackend, MinioConfig

    client.create_bucket(Bucket="disposable-snapshots")
    selected = DimensionConfig("lab", "Lab", "minio", endpoint, "disposable-snapshots", "lab", options=(("room_store_authority_endpoint", "https://authority.example.invalid"), ("room_store_authority_profile", "lab")))
    domain = portable.domain_id(selected)
    config["vars"]["ROOM_STORE_DOMAIN_ID"] = domain
    config_path.write_text(json.dumps(config))
    subprocess.run([args.celld, "deploy", str(config_path), *common], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if call("GET", "material")[0] == 404:
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("disposable migration deployment did not become active")
    identity = crypto.generate_identity(directory / "operational.age")
    recovery = crypto.generate_identity(directory / "recovery.age")
    source = EncryptionKeyset.create("minio", endpoint, selected.bucket, identity.read_text().splitlines()[-1], crypto.derive_recipient(identity), [crypto.derive_recipient(recovery)]).upgrade_for_room_store()
    password = directory / "password"
    password.write_text(source.room_store.secret)
    password.chmod(0o600)
    repository = f"s3:{endpoint}/{selected.bucket}/room-store/v1"
    restic_env = {**os.environ, "AWS_ACCESS_KEY_ID": access, "AWS_SECRET_ACCESS_KEY": secret, "RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD_FILE": str(password), "RESTIC_CACHE_DIR": str(directory / "restic-cache")}
    def restic(*command):
        result = subprocess.run(["restic", *command], env=restic_env, check=False, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("disposable Restic check failed")
        return result.stdout
    restic("init", "--repository-version", "2")
    repository_id = json.loads(restic("cat", "config"))["id"]
    source = source.bind_repository(repository_id, expected_generation=1)
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("synthetic snapshot bytes\n")
    restic("backup", str(workspace), "--json")
    backend = MinioBackend(MinioConfig.from_dimension(selected), client=client)
    backend.create_control(KEYSET_CONTROL_KEY, source.to_json())
    catalog = b"synthetic-encrypted-catalog-sentinel"
    backend.conditional_catalog_put(catalog, None)
    material = EncryptionMaterial(source, identity)
    dropped = False
    def transport(method, _session, operation, _headers, body):
        nonlocal dropped
        result = call(method, operation, None if body is None else json.loads(body))
        if operation == "material" and method == "POST" and result[0] == 201 and not dropped:
            dropped = True
            raise OSError("synthetic lost response after actual DO commit")
        return result
    # The lab injects loopback HTTP transport and disables only local keyring caching.
    with patch("josh_room.r2_room_store_material.keyring.store_room_store_secret", lambda *args: None), patch.object(auth, "_material_for_keyset", lambda keyset: EncryptionMaterial(keyset, identity)):
        marker = portable.migrate(selected, backend, material, transport=transport)
        resolved = portable.marker_keyset(marker, selected, material_factory=lambda base: EncryptionMaterial(base, identity), transport=transport)
        assert resolved.room_store.secret == source.room_store.secret
        assert resolved.room_store.repository_id == repository_id
        assert dropped
        assert source.room_store.secret.encode() not in backend.read_control(KEYSET_CONTROL_KEY, 65536)[0]
        original_authority = portable.authority
        with patch.object(portable, "authority", lambda dimension, selected_material, **kwargs: original_authority(dimension, selected_material, transport=transport)), patch.object(auth, "store_room_store_secret", lambda *args: None):
            before = backend.read_control(KEYSET_CONTROL_KEY, 65536)
            readback, _etag = auth._read_keyset_record_from_backend(selected, backend)
            assert readback.room_store.secret == source.room_store.secret
            assert backend.read_control(KEYSET_CONTROL_KEY, 65536) == before
            assert auth.ensure_room_store_keyset(selected, backend).room_store.secret == source.room_store.secret
            assert auth.bind_room_store_repository(selected, backend, repository_id, expected_generation=2).room_store.generation == 2
            try:
                auth.bind_room_store_repository(selected, backend, repository_id, expected_generation=8)
            except RuntimeError:
                pass
            else:
                raise AssertionError("migrated client accepted stale generation")
        password.write_text(resolved.room_store.secret)
        restic("backup", str(workspace), "--json")
        restic("restore", "latest", "--target", str(directory / "restored"))
        assert next(iter((directory / "restored").rglob("fixture.txt"))).read_bytes() == (workspace / "fixture.txt").read_bytes()
        before_rollback, _etag = backend.read_catalog()
        assert before_rollback == catalog
        restored = portable.rollback(selected, backend, transport=transport)
        assert restored.room_store.secret == source.room_store.secret
        assert backend.read_catalog()[0] == catalog
        restic("check")
    print("PASS real age custody migration and sealed rollback, existing Restic incremental backup/restore/check and catalog bytes preserved", flush=True)


if __name__ == "__main__":
    main()
