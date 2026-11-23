"""Task-owned real CLI/MinIO check; credentials generated in memory only."""

import argparse
import copy
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
import uuid
from pathlib import Path

import boto3
from git_recovery_acceptance import advance_catalog_git, catalog_repository, contents
from git_recovery_acceptance import identity as git_identity
from minio_acceptance_audit import MinioAudit

from josh_room import keyring
from josh_room.config import persisted_config, save_private_config


def create_source(source):
    (source / "projects/a").mkdir(parents=True)
    (source / "b").mkdir()
    (source / "local").mkdir()
    (source / "projects/a/file.txt").write_text("synthetic child A original")
    (source / "b/file.txt").write_text("synthetic child B")
    (source / "local/file.txt").write_text("synthetic local only")
    (source / "notes.txt").write_text("synthetic parent note")
    for child_path in ("projects/a", "b"):
        catalog_repository(source / child_path)


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
    parser.add_argument('--qualify-domain', action='store_true', help='verify uncached domain resolution and audit provider mutations')
    parser.add_argument('--qualify-updates', action='store_true', help='also verify independent child Save and explicit parent pin refresh')
    parser.add_argument('--hold-for-native', action='store_true', help='keep only this owned fixture server until its stop-native file exists')
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
    audit = MinioAudit() if args.qualify_domain else None
    if audit:
        env.update(MINIO_AUDIT_WEBHOOK_ENABLE_FIXTURE="on",
                   MINIO_AUDIT_WEBHOOK_ENDPOINT_FIXTURE=f"http://127.0.0.1:{audit.server.server_port}")
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
        if audit:
            client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                                  aws_access_key_id=access, aws_secret_access_key=secret)
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

        def cli(*args, rejected=False):
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
            if rejected:
                assert result.returncode != 0 and body.get("ok") is False
                return body
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
        create_source(source)
        original_source = contents(source)
        original_git = git_identity(source / "projects/a")
        legacy_inventory = None
        if args.qualify_updates:
            legacy = base / 'legacy-source'
            legacy.mkdir()
            (legacy / 'legacy.txt').write_text('immutable synthetic legacy bytes')
            cli('snapshot', 'create', 'synthetic-legacy', '--source', str(legacy), '--backend', 'minio', '--dimension', 'composition-fixture')
            legacy_inventory = cli('snapshots', 'list', 'synthetic-legacy', '--backend', 'minio', '--dimension', 'composition-fixture')
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
        ]
        if audit:
            uncached = persisted_config()
            uncached["dimensions"]["composition-fixture"].pop("encryption_domain_id", None)
            save_private_config(uncached)
            before = audit.checkpoint(client)
            assert before > 0, "audit must observe enrollment writes"
        else:
            selection.extend(["--expected-domain", domain])
        plan = cli("composition", "capture-plan", *common, *selection)
        assert all(
            evidence["capture_mode"] == "full-repository"
            for evidence in plan["request"]["child_evidence"].values()
        )
        if audit:
            assert plan["request"]["destination_binding"]["encryption_domain_id"] == domain
            assert domain.replace("-", "") != plan["request"]["namespace"]
            assert cli("composition", "capture-plan", *common, *selection) == plan
            assert cli("composition", "capture-plan", *common, *selection, "--expected-domain", domain) == plan
            wrong = str(uuid.uuid4())
            for assertion in ["malformed", wrong]:
                cli("composition", "capture-plan", *common, *selection, "--expected-domain", assertion, rejected=True)
            apply = [*common, *selection, "--approve", plan["approval_sha256"], "--ack-local-only"]
            stale = copy.deepcopy(uncached)
            stale["dimensions"]["composition-fixture"]["encryption_domain_id"] = wrong
            save_private_config(stale)
            cli("composition", "save", *apply, "--expected-domain", domain, rejected=True)
            stale = copy.deepcopy(uncached)
            stale["connections"]["fixture-alias"] = stale["connections"]["composition-connection"]
            stale["dimensions"]["composition-fixture"]["connection"] = "fixture-alias"
            save_private_config(stale)
            cli("composition", "save", *apply, rejected=True)
            save_private_config(uncached)
            assert audit.checkpoint(client) == before, "read-only and rejected operations issued a provider mutation"
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
        assert git_identity(entered / "projects/a") == original_git
        assert contents(entered / "projects/a/.git") == contents(source / "projects/a/.git")
        assert contents(source) == original_source
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
            "full_child_git_verified": True,
        }
        if args.qualify_updates:
            child_root = entered / 'projects/a'
            advance_catalog_git(child_root)
            updated_git = git_identity(child_root)
            assert updated_git != original_git
            update_args = ['--dimension', 'composition-fixture', '--namespace', saved['namespace'],
                           '--room', child['room_id'], '--child-snapshot', child['snapshot_id'],
                           '--workspace', str(child_root), '--operation-id', 'fixture-child-update']
            update_plan = cli('composition', 'plan-child-save', *update_args)
            assert update_plan['changed']
            updated = cli('composition', 'save-child', *update_args, '--approve', update_plan['approval_sha256'])
            assert updated['marker_verified'] and not updated['parent_refreshed']
            assert cli('composition', 'save-child', *update_args, '--approve', update_plan['approval_sha256'])['snapshot_id'] == updated['snapshot_id']
            refresh_args = [*common, '--parent-snapshot', saved['parent_snapshot_id'], '--operation-id', 'fixture-pin-refresh',
                            '--set-child', 'projects/a=' + updated['snapshot_id']]
            refresh_plan = cli('composition', 'plan-parent-refresh', *refresh_args)
            refreshed = cli('composition', 'refresh-parent-pins', *refresh_args, '--approve', refresh_plan['approval_sha256'])
            assert not refreshed['workspace_modified']
            assert cli('composition', 'refresh-parent-pins', *refresh_args, '--approve', refresh_plan['approval_sha256'])['snapshot_id'] == refreshed['snapshot_id']
            for name, parent_pin, expected_child, expected_git in (
                ('old', saved['parent_snapshot_id'], child['snapshot_id'], original_git),
                ('new', refreshed['snapshot_id'], updated['snapshot_id'], updated_git),
            ):
                target = base / ('restore-' + name)
                cli('composition', 'restore-parent', *common, '--parent-snapshot', parent_pin, '--destination', str(target))
                selected = cli('composition', 'enter-child', *common, '--parent-snapshot', parent_pin, '--workspace', str(target), '--child-path', 'projects/a')
                assert selected['snapshot_id'] == expected_child
                assert (target / 'projects/a/file.txt').read_text() == 'synthetic child A original'
                assert git_identity(target / 'projects/a') == expected_git
                assert not (target / 'b').exists() and not (target / 'local').exists()
            noop_args = [*update_args]
            noop_args[noop_args.index('--child-snapshot') + 1] = updated['snapshot_id']
            noop_args[noop_args.index('--operation-id') + 1] = 'fixture-child-noop'
            noop = cli('composition', 'plan-child-save', *noop_args)
            assert not noop['changed']
            assert cli('composition', 'save-child', *noop_args, '--approve', noop['approval_sha256'])['catalog_writes'] is False
            parent_noop_args = [*refresh_args]
            parent_noop_args[parent_noop_args.index('--parent-snapshot') + 1] = refreshed['snapshot_id']
            parent_noop_args[parent_noop_args.index('--operation-id') + 1] = 'fixture-parent-noop'
            parent_noop = cli('composition', 'plan-parent-refresh', *parent_noop_args)
            assert not parent_noop['changed']
            assert cli('composition', 'refresh-parent-pins', *parent_noop_args, '--approve', parent_noop['approval_sha256'])['catalog_writes'] is False
            assert cli('snapshots', 'list', 'synthetic-legacy', '--backend', 'minio', '--dimension', 'composition-fixture') == legacy_inventory
            assert (source / 'projects/a/file.txt').read_text() == 'synthetic child A original'
            assert (source / 'b/file.txt').read_text() == 'synthetic child B'
            assert (source / 'notes.txt').read_text() == 'synthetic parent note'
            receipt.update(child_update_pin=updated['snapshot_id'], refreshed_parent_pin=refreshed['snapshot_id'],
                git_only_update_verified=True, historical_git_states_verified=True,
                old_parent_old_child_bytes_verified=True, new_parent_new_child_bytes_verified=True,
                update_and_refresh_retry_verified=True, child_noop_verified=True, parent_noop_verified=True, legacy_inventory_unchanged=True)
        if audit:
            assert audit.checkpoint(client) > before
            assert persisted_config() == uncached
            receipt.update(uncached_domain_verified=True, matching_assertion_verified=True,
                           deterministic_plan=True, mutation_count_during_plans_and_rejections=0,
                           stale_connection_rejected=True, conflicting_cache_rejected=True)
        receipt["source_sha"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (base / "loopback-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
        if args.hold_for_native:
            ready = {'fixture_root': str(base), 'parent_working': str(base / 'restore-new' if args.qualify_updates else entered),
                     'child_room': child['room_id'], 'receipt': receipt, 'legacy_inventory': legacy_inventory}
            if audit:
                ready["native_sources"] = {}
                for route in ("joshRoom.saveCatalog", "joshRoom.new"):
                    folder = base / route
                    create_source(folder)
                    ready["native_sources"][route] = str(folder)
            (scratch / 'native-ready.json').write_text(json.dumps(ready))
            while not (base / 'stop-native').exists():
                time.sleep(0.5)
    finally:
        server.terminate()
        server.wait(timeout=20)
        if audit:
            audit.close()


if __name__ == "__main__":
    main()
