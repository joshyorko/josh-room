import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from . import device as _device
from . import r2 as _r2
from . import scheduler as _scheduler
from .auth import (
    EncryptionStateError,
    _keyset_from_backend,
    _resolve_recovery_recipients,
    _valid_identity,
    cancel_oauth_session,
    encryption_status,
    ensure_minio_domain,
    ensure_runtime_session,  # noqa: F401 - retained as an observable interactive-auth boundary.
    load_runtime_session,
    logout_runtime_session,
    poll_oauth_session,
    r2_room_store_session_state,
    r2_session_state,
    resolve_encryption_material,
    runtime_capabilities,
    runtime_session_state,
    start_oauth_session,
    wait_oauth_session,
)
from .cancellation import CLICancelled, sigterm_cancellation
from .catalog import Catalog
from .config import (
    ConnectionConfig,
    DimensionRegistry,
    auth_status,
    connection_configs,
    default_s3_endpoint,
    persisted_config,
    private_config,
    save_private_config,
)
from .crypto import CryptoError, _managed_executable, decrypt, generate_identity
from .encryption_domain import validate_recipient
from .harvest import HarvestController
from .harvest_bridge import HostHarvestBridge, HostHarvestConfig, load_host_policy
from .jat import (
    _jat_contract,
    run_build,
    run_copy,
    run_doctor,
    run_export,
    run_extract,
    run_inspect,
    run_restore,
    run_serve,
)
from .keyring import backend_status as _keyring_backend_status
from .keyring import lookup_value as lookup_keyring_value
from .keyring import secure_lookup as lookup_secure_value
from .keyring import store as store_keyring
from .keyring import store_value as store_keyring_value
from .local_store import ImmutableLocalStore
from .minio import MinioBackend, MinioConfig
from .minio import check_bucket_access as check_minio_bucket
from .minio import create_bucket as create_minio_bucket
from .minio import list_buckets as list_minio_buckets
from .operations import (
    _legacy_migration_source_binding,
    _material_recipients,
    _migration_result,
    _new_journal,
    _read_journal,
    _read_legacy_migration_catalog,
    _read_remote_catalog,
    _safe_journal_update,
    _validate_journal_identity,
    _validate_journal_mapping_set,
    copy_snapshot_stream,
    create_snapshot,
    hydrate,
    link_workspace,
    migrate_encryption,
    plan_encryption_migration,
    reconcile_encryption_migration,
    remove_room,
    remove_snapshot,
    repair_workspace,
    serve_snapshot,
    snapshot_payload_kind,
)
from .pcc_hooks import (
    codex_hook_main,
    codex_hook_status,
    install_codex_hooks,
    process_codex_hook,
    remove_codex_hooks,
    repair_codex_hooks,
)
from .pcc_outbox import PccOutbox
from .pcc_replay import ReplayLimits, ReplayReader
from .policy import CaptureRequest, PolicyContext, decide
from .progress import report_progress
from .s3_backend import sdk_error_boundary
from .tls import initialize_system_trust
from .workspace_state import context_status, local_status


def _deferred_room_store_call(module, name):
    def invoke(*args, **kwargs):
        operation = getattr(importlib.import_module(module, __package__), name)
        globals()[name] = operation
        return operation(*args, **kwargs)

    return invoke


hydrate_room_store = _deferred_room_store_call('.room_store_bridge', 'hydrate_room_store')
open_existing_room_store = _deferred_room_store_call('.room_store_bridge', 'open_existing_room_store')
open_writable_room_store = _deferred_room_store_call('.room_store_bridge', 'open_writable_room_store')
preview_room_store = _deferred_room_store_call('.room_store_bridge', 'preview_room_store')
save_room_store = _deferred_room_store_call('.room_store_bridge', 'save_room_store')
complete_logical_catalog_removal = _deferred_room_store_call('.room_store_lifecycle', 'complete_logical_catalog_removal')
copy_logical_jat_as_new = _deferred_room_store_call('.room_store_lifecycle', 'copy_logical_jat_as_new')
copy_logical_jat_to_dimension = _deferred_room_store_call('.room_store_lifecycle', 'copy_logical_jat_to_dimension')
export_logical_jat = _deferred_room_store_call('.room_store_lifecycle', 'export_logical_jat')
extract_logical_jat = _deferred_room_store_call('.room_store_lifecycle', 'extract_logical_jat')
inspect_logical_jat = _deferred_room_store_call('.room_store_lifecycle', 'inspect_logical_jat')
optimize_room_store = _deferred_room_store_call('.room_store_lifecycle', 'optimize_room_store')
reconcile_room_store = _deferred_room_store_call('.room_store_lifecycle', 'reconcile_room_store')
remove_logical_catalog_records = _deferred_room_store_call('.room_store_lifecycle', 'remove_logical_catalog_records')
serve_logical_jat = _deferred_room_store_call('.room_store_lifecycle', 'serve_logical_jat')
verify_room_store = _deferred_room_store_call('.room_store_lifecycle', 'verify_room_store')

R2Backend = _r2.R2Backend
R2Config = _r2.R2Config
_HUMAN_DIAGNOSTIC_LIMIT = 4096


def _store_credentials(profile, credentials):
    if os.environ.get("JOSH_ROOM_EXTENSION_MODE") != "1":
        store_keyring(profile, credentials)


def _store_identity(profile, value):
    if os.environ.get("JOSH_ROOM_EXTENSION_MODE") != "1":
        store_keyring_value(profile, "age-identity", value, label="Josh Room age identity")


def _json_option(parser):
    parser.add_argument("--json", action="store_true")


def _assert_delete_inventory(catalog, project, args):
    expected_revision = getattr(args, "expected_revision", None)
    expected_snapshots = getattr(args, "expected_snapshots", None)
    if expected_revision is not None and catalog.body.get("revision") != expected_revision:
        raise ValueError("catalog changed after deletion confirmation; reload and confirm again")
    if project is None:
        raise ValueError("room is not present in the encrypted catalog")
    if expected_snapshots is not None:
        expected = [value for value in expected_snapshots.split(",") if value]
        current = list(project.get("snapshots", {}))
        if current != expected:
            raise ValueError("recovery point inventory changed after deletion confirmation; reload and confirm again")


def _add_serve_options(parser):
    parser.add_argument("--fileserver-port", type=int, default=0, help="0 selects an automatic fileserver port")
    parser.add_argument("--registry-port", type=int, default=0, help="0 selects an automatic registry port")
    parser.add_argument("--startup-file", type=Path, help="private JAT endpoint startup receipt")


def _serve_options(args):
    return {name: getattr(args, name, default) for name, default in (
        ("fileserver_port", 0), ("registry_port", 0), ("startup_file", None))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josh-room")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("agents", help="inspect and manage opt-in isolated native agent profiles")
    workspace = commands.add_parser("workspace", help="inspect or repair workspace metadata")
    workspace_commands = workspace.add_subparsers(dest="workspace_command", required=True)
    for action in ("init", "exclude"):
        operation = workspace_commands.add_parser(action)
        operation.add_argument("--workspace", type=Path, required=True)
        if action == "init":
            operation.add_argument("--editor", choices=("vscode", "none"), default="vscode")
            operation.add_argument("--policy-only", action="store_true", help=argparse.SUPPRESS)
            files = operation.add_mutually_exclusive_group()
            files.add_argument("--editor-file", dest="editor_files", action="append",
                               choices=("tasks.json", "launch.json", "extensions.json", "settings.json"))
            files.add_argument("--no-editor-files", action="store_true")
        if action == "exclude":
            operation.add_argument("--rule", action="append", default=[])
            operation.add_argument("--expected-token", required=True)
            operation.add_argument("--source-only", action="store_true")
        _json_option(operation)
    worktree = workspace_commands.add_parser("worktree-repair", help="propose or apply stale Git worktree record cleanup")
    worktree.add_argument("--workspace", type=Path, required=True)
    worktree.add_argument("--approve", action="store_true", help="apply the exact dry-run proposal")
    worktree.add_argument("--proposal-digest")
    _json_option(worktree)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    doctor.add_argument("--dimension")
    doctor.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    _json_option(doctor)
    connections = commands.add_parser("connections", help="legacy compatibility alias for provider connection")
    connection_commands = connections.add_subparsers(dest="connection_command", required=True)
    connection_list = connection_commands.add_parser("list")
    _json_option(connection_list)
    connection_setup = connection_commands.add_parser("setup")
    connection_setup.add_argument("provider", nargs="?", choices=("minio", "s3", "r2"))
    connection_setup.add_argument("--provider", dest="provider_option", choices=("minio", "s3", "r2"))
    connection_setup.add_argument("--connection", "--id", dest="connection_id")
    connection_setup.add_argument("--display-name")
    connection_setup.add_argument("--credential-profile", "--profile", dest="credential_profile")
    connection_setup.add_argument("--endpoint")
    connection_setup.add_argument("--region")
    connection_setup.add_argument("--path-style", action="store_true")
    connection_setup.add_argument("--virtual-host-style", action="store_true")
    _json_option(connection_setup)
    buckets = commands.add_parser("buckets", help="legacy compatibility alias for provider bucket")
    bucket_commands = buckets.add_subparsers(dest="bucket_command", required=True)
    bucket_list = bucket_commands.add_parser("list")
    bucket_list.add_argument("--connection", required=True)
    _json_option(bucket_list)
    bucket_create = bucket_commands.add_parser("create")
    bucket_create.add_argument("--connection", required=True)
    bucket_create.add_argument("--bucket", required=True)
    _json_option(bucket_create)
    bucket_check = bucket_commands.add_parser("check")
    bucket_check.add_argument("--connection", required=True)
    bucket_check.add_argument("--bucket", required=True)
    _json_option(bucket_check)
    provider = commands.add_parser("provider", help="canonical provider-connection and bucket boundary")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_connection = provider_commands.add_parser("connection")
    provider_connection_commands = provider_connection.add_subparsers(dest="provider_connection_command", required=True)
    provider_connection_create = provider_connection_commands.add_parser("create")
    provider_connection_create.add_argument("--provider", required=True, choices=("minio", "s3", "r2"))
    provider_connection_create.add_argument("--endpoint")
    provider_connection_create.add_argument("--region")
    provider_connection_create.add_argument("--path-style", action="store_true")
    provider_connection_create.add_argument("--virtual-host-style", action="store_true")
    provider_connection_create.add_argument("--connection", "--id", dest="connection_id")
    provider_connection_create.add_argument("--credential-profile", "--profile", dest="credential_profile")
    _json_option(provider_connection_create)
    provider_connection_list = provider_connection_commands.add_parser("list")
    _json_option(provider_connection_list)
    for action in ("update", "reconnect", "disconnect"):
        provider_connection_edit = provider_connection_commands.add_parser(action, help="stdin carries credentials; legacy alias is also supported")
        provider_connection_edit.add_argument("--connection", required=True)
        provider_connection_edit.add_argument("--endpoint")
        provider_connection_edit.add_argument("--region")
        provider_connection_edit.add_argument("--path-style", action="store_true")
        provider_connection_edit.add_argument("--virtual-host-style", action="store_true")
        provider_connection_edit.add_argument("--credential-profile", "--profile", dest="credential_profile")
        _json_option(provider_connection_edit)
    provider_bucket = provider_commands.add_parser("bucket")
    provider_bucket_commands = provider_bucket.add_subparsers(dest="provider_bucket_command", required=True)
    provider_bucket_list = provider_bucket_commands.add_parser("list")
    provider_bucket_list.add_argument("--connection")
    provider_bucket_list.add_argument("--provider", choices=("r2", "minio", "s3"))
    provider_bucket_list.add_argument("--dimension")
    _json_option(provider_bucket_list)
    provider_bucket_create = provider_bucket_commands.add_parser("create")
    provider_bucket_create.add_argument("--connection")
    provider_bucket_create.add_argument("--provider", choices=("r2", "minio", "s3"))
    provider_bucket_create.add_argument("--dimension")
    provider_bucket_create.add_argument("--bucket", required=True)
    _json_option(provider_bucket_create)
    provider_bucket_check = provider_bucket_commands.add_parser("check")
    provider_bucket_check.add_argument("--connection")
    provider_bucket_check.add_argument("--provider", choices=("r2", "minio", "s3"))
    provider_bucket_check.add_argument("--dimension")
    provider_bucket_check.add_argument("--bucket", required=True)
    _json_option(provider_bucket_check)
    dimensions = commands.add_parser("dimensions")
    dimension_commands = dimensions.add_subparsers(dest="dimension_command", required=True)
    dimension_list = dimension_commands.add_parser("list")
    dimension_list.add_argument("--dimension")
    dimension_list.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    dimension_list.add_argument("--with-hierarchy", action="store_true", help="read Rooms and JATs from each selected Dimension catalog")
    _json_option(dimension_list)
    for action in ("add", "update"):
        dimension_edit = dimension_commands.add_parser(action)
        dimension_edit.add_argument("dimension")
        dimension_edit.add_argument("--display-name")
        dimension_edit.add_argument("--provider", choices=("r2", "minio", "s3"))
        dimension_edit.add_argument("--connection", "--connection-id", dest="connection_id")
        dimension_edit.add_argument("--endpoint")
        dimension_edit.add_argument("--bucket")
        dimension_edit.add_argument("--credential-profile")
        dimension_edit.add_argument("--region")
        dimension_edit.add_argument("--catalog-key")
        _json_option(dimension_edit)
    auth = commands.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_start = auth_commands.add_parser("start")
    auth_start.add_argument("--dimension")
    auth_start.add_argument("--purpose", choices=("encryption", "r2"), default="r2")
    _json_option(auth_start)
    auth_poll = auth_commands.add_parser("poll")
    auth_poll.add_argument("session_id")
    auth_poll.add_argument("--dimension")
    auth_poll.add_argument("--purpose", choices=("encryption", "r2"))
    auth_poll.add_argument("--legacy-source-handoff", type=Path)
    auth_poll.add_argument("--legacy-r2-source", action="store_true", help="explicit legacy R2 source custody; requires --legacy-source-handoff and --purpose r2")
    _json_option(auth_poll)
    auth_wait = auth_commands.add_parser("wait", help="wait for one OAuth session in this process")
    auth_wait.add_argument("session_id")
    auth_wait.add_argument("--dimension")
    auth_wait.add_argument("--purpose", choices=("encryption", "r2"))
    auth_wait.add_argument("--timeout", type=int, default=600)
    auth_wait.add_argument("--poll-interval", type=int, default=2)
    auth_wait.add_argument("--legacy-source-handoff", type=Path)
    auth_wait.add_argument("--legacy-r2-source", action="store_true", help="explicit legacy R2 source custody; requires --legacy-source-handoff and --purpose r2")
    _json_option(auth_wait)
    auth_cancel = auth_commands.add_parser("cancel", help="invalidate one pending OAuth session")
    auth_cancel.add_argument("session_id")
    auth_cancel.add_argument("--preserve-runtime-session", action="store_true")
    _json_option(auth_cancel)
    auth_status = auth_commands.add_parser("status")
    auth_status.add_argument("--dimension")
    _json_option(auth_status)
    auth_logout = auth_commands.add_parser("logout", help="clear the local Cloudflare session")
    auth_logout.add_argument("--dimension")
    auth_logout.add_argument("--purpose", choices=("all", "r2"), default="all")
    _json_option(auth_logout)
    encryption = commands.add_parser("encryption")
    encryption_commands = encryption.add_subparsers(dest="encryption_command", required=True)
    recovery = encryption_commands.add_parser("recovery")
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_generate = recovery_commands.add_parser("generate")
    recovery_generate.add_argument("--output", type=Path, required=True)
    _json_option(recovery_generate)
    for action in ("status", "migrate", "resume"):
        encryption_action = encryption_commands.add_parser(action)
        encryption_action.add_argument("--dimension", required=True)
        if action in {"migrate", "resume"}:
            encryption_action.add_argument("--source-identity", type=Path)
            encryption_action.add_argument("--expected-catalog-etag", help="require the source catalog version shown in the confirmed preview")
            encryption_action.add_argument("--expected-source-binding", help="confirm the legacy Dimension adoption and selected physical catalog shown in the preview")
            encryption_action.add_argument("--recovery-recipient", action="append", dest="recovery_recipients")
            encryption_action.add_argument("--recovery-handoff", type=Path)
        _json_option(encryption_action)
    encryption_initialize = encryption_commands.add_parser("initialize")
    encryption_initialize.add_argument("--dimension", required=True)
    encryption_initialize.add_argument("--recovery-recipient", action="append", dest="recovery_recipients")
    encryption_initialize.add_argument("--recovery-handoff", type=Path)
    encryption_initialize.add_argument("--material-handoff", type=Path)
    _json_option(encryption_initialize)
    harvest = commands.add_parser("harvest")
    harvest_commands = harvest.add_subparsers(dest="harvest_command", required=True)
    harvest_hooks = harvest_commands.add_parser("hooks")
    harvest_hook_commands = harvest_hooks.add_subparsers(dest="harvest_hooks_command", required=True)
    for action in ("install", "status", "repair", "remove"):
        hook_action = harvest_hook_commands.add_parser(action)
        hook_action.add_argument("--tool", required=True, choices=("codex",))
        _json_option(hook_action)
    for action in ("plan", "run", "drain", "status", "inspect", "retry", "quarantine", "discard", "reconcile"):
        command = harvest_commands.add_parser(action)
        command.add_argument("--outbox-root", type=Path)
        if action in {"run", "drain", "reconcile"}:
            command.add_argument("--limit", type=int, default=1 if action != "reconcile" else 1000)
            command.add_argument("--drain", action="store_true", help="deliver prepared records after offline preparation")
            command.add_argument("--max-seconds", type=float)
        if action == "run":
            command.add_argument("--offline", action="store_true")
            command.add_argument("--tool", choices=("codex",), default="codex", help="host source tool")
            command.add_argument("--profile", required=False, help="host-owned capture/device profile")
            command.add_argument("--policy-config", type=Path, help="explicit private policy file")
            command.add_argument("--config-home", type=Path, help="private XDG config home containing josh-room/policy.json")
            command.add_argument("--codex-active-root", type=Path, required=False, help="explicit Codex active transcript root")
            command.add_argument("--codex-archived-root", type=Path, required=False, help="explicit Codex archived transcript root")
            command.add_argument("--scheduler-context-id", help="opaque installed scheduler context identifier")
            command.add_argument("--workspace-id")
            command.add_argument("--workspace-path")
            command.add_argument("--repository", help="host-observed repository remote (credential-free form)")
            command.add_argument("--path-kind", choices=("directory", "worktree", "remote", "wsl", "symlink", "unknown"), default="unknown")
            command.add_argument("--age-executable", type=Path)
        if action == "drain":
            command.add_argument("--profile", help="optional host-owned capture/device profile")
            command.add_argument("--policy-config", type=Path)
            command.add_argument("--config-home", type=Path)
            command.add_argument("--codex-active-root", type=Path)
            command.add_argument("--codex-archived-root", type=Path)
            command.add_argument("--workspace-id")
            command.add_argument("--workspace-path")
            command.add_argument("--repository")
            command.add_argument("--path-kind", default="unknown")
            command.add_argument("--age-executable", type=Path)
            command.add_argument("--dimension")
            command.add_argument("--index-file", type=Path, help="encrypted #11 R2 index object")
        if action in {"plan", "status"}:
            command.add_argument("--profile")
            command.add_argument("--policy-config", type=Path)
            command.add_argument("--config-home", type=Path)
            command.add_argument("--codex-active-root", type=Path)
            command.add_argument("--codex-archived-root", type=Path)
            command.add_argument("--workspace-id")
            command.add_argument("--workspace-path")
            command.add_argument("--repository")
            command.add_argument("--path-kind", default="unknown")
        if action in {"plan", "inspect"}:
            command.add_argument("event_id", nargs="?")
        elif action in {"retry", "quarantine", "discard"}:
            command.add_argument("event_id", nargs="?" if action in {"retry", "quarantine"} else None)
        if action == "retry":
            command.add_argument("--all-retryable", action="store_true")
        if action == "quarantine":
            command.add_argument("--inspect", action="store_true")
            command.add_argument("--list", action="store_true")
        if action in {"retry", "quarantine"}:
            command.add_argument("--reason", default=None)
        _json_option(command)
    schedule = harvest_commands.add_parser("schedule")
    schedule_commands = schedule.add_subparsers(dest="schedule_command", required=True)
    for action in ("install", "status", "remove"):
        schedule_action = schedule_commands.add_parser(action)
        schedule_action.add_argument("--interval", type=int, default=900)
        schedule_action.add_argument("--platform", dest="platform_name")
        schedule_action.add_argument("--home", type=Path)
        schedule_action.add_argument("--executable")
        schedule_action.add_argument("--profile")
        schedule_action.add_argument("--codex-active-root", type=Path)
        schedule_action.add_argument("--codex-archived-root", type=Path)
        schedule_action.add_argument("--policy-config", type=Path)
        schedule_action.add_argument("--config-home", type=Path)
        schedule_action.add_argument("--workspace-id")
        schedule_action.add_argument("--workspace-path")
        schedule_action.add_argument("--repository")
        schedule_action.add_argument("--path-kind", choices=("directory", "worktree", "remote", "wsl", "symlink", "unknown"), default="unknown")
        schedule_action.add_argument("--age-executable", type=Path)
        _json_option(schedule_action)
    replay = commands.add_parser("replay", help="bounded PCC raw-evidence reader/export")
    replay_commands = replay.add_subparsers(dest="replay_command", required=True)
    for action in ("inspect", "export"):
        replay_action = replay_commands.add_parser(action)
        replay_action.add_argument("--profile", required=True)
        replay_action.add_argument("--destination", required=True)
        replay_action.add_argument("--workspace-id")
        replay_action.add_argument("--policy-config", type=Path)
        replay_action.add_argument("--config-home", type=Path)
        replay_action.add_argument("--dimension")
        replay_action.add_argument("--cursor")
        replay_action.add_argument("--limit", type=int, default=8)
        replay_action.add_argument("--page-size", type=int, default=8)
        replay_action.add_argument("--max-indexes", type=int, default=1000)
        replay_action.add_argument("--identity", type=Path, action="append")
        replay_action.add_argument("--age-executable", type=Path)
        if action == "export":
            replay_action.add_argument("--max-scan-bytes", type=int, default=ReplayLimits().max_scan_bytes)
            replay_action.add_argument("--jsonl", action="store_true", help="emit one contract object per line")
        _json_option(replay_action)
    hook = commands.add_parser("hook")
    hook_commands = hook.add_subparsers(dest="hook_command", required=True)
    hook_codex = hook_commands.add_parser("codex")
    _json_option(hook_codex)
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    status.add_argument("--include-context", action="store_true")
    _json_option(status)
    check = commands.add_parser("workspace-check", help="validate workspace metadata without changing it")
    check.add_argument("--workspace", type=Path, default=Path.cwd())
    _json_option(check)
    composition = commands.add_parser("composition", help="explicit catalog capture, pinned entry and offline discovery")
    composition_commands = composition.add_subparsers(dest="composition_command", required=True)
    composition_plan = composition_commands.add_parser("plan")
    composition_plan.add_argument("--workspace", type=Path, required=True)
    composition_plan.add_argument("--child", action="append", default=[])
    _json_option(composition_plan)
    inventory = composition_commands.add_parser("inventory", help="local paged JSON-lines discovery session")
    inventory.add_argument("--workspace", type=Path, required=True)
    _json_option(inventory)
    composition_reconcile = composition_commands.add_parser("reconcile", help="inspect offline evidence; never repair")
    composition_reconcile.add_argument("--evidence-file", type=Path, required=True)
    _json_option(composition_reconcile)
    for action in ('capture-plan', 'save', 'restore-parent', 'enter-child'):
        operation = composition_commands.add_parser(action)
        operation.add_argument('--dimension', required=True)
        operation.add_argument('--namespace', required=True)
        operation.add_argument('--parent-room', required=True)
        if action in ('capture-plan', 'save'):
            operation.add_argument('--expected-domain', help='encryption domain from the existing enrollment receipt; required if not cached in config')
            operation.add_argument('--workspace', type=Path, required=True)
            operation.add_argument('--assign', action='append', default=[], help='relative-path=child|parent|local-only; subtree choice, no implicit default')
        if action == 'save':
            operation.add_argument('--approve', required=True, help='exact capture-plan approval_sha256')
            operation.add_argument('--ack-local-only', action='store_true')
        if action in ('restore-parent', 'enter-child'):
            operation.add_argument('--parent-snapshot', required=True)
        if action == 'restore-parent':
            operation.add_argument('--destination', type=Path, required=True)
        if action == 'enter-child':
            operation.add_argument('--workspace', type=Path, required=True)
            operation.add_argument('--child-path', required=True)
        _json_option(operation)
    context = commands.add_parser("context", help="read offline workspace linkage context")
    context.add_argument("--workspace", type=Path, required=True)
    _json_option(context)
    for action in ("link", "repair"):
        state_command = commands.add_parser(action)
        state_command.add_argument("--workspace", type=Path, default=Path.cwd())
        state_command.add_argument("--dimension")
        state_command.add_argument("--project")
        state_command.add_argument("--snapshot")
        state_command.add_argument("--workspace-fingerprint")
        state_command.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
        _json_option(state_command)
    projects = commands.add_parser("projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    project_list.add_argument("--dimension")
    _json_option(project_list)
    rooms = commands.add_parser("rooms")
    room_commands = rooms.add_subparsers(dest="room_command", required=True)
    room_remove = room_commands.add_parser("remove")
    room_remove.add_argument("project")
    room_remove.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    room_remove.add_argument("--dimension")
    room_remove.add_argument("--expected-revision", type=int)
    room_remove.add_argument("--expected-snapshots", help="comma-separated snapshot IDs from the confirmed inventory")
    _json_option(room_remove)
    snapshots = commands.add_parser("snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshots_command", required=True)
    snapshot_list = snapshot_commands.add_parser("list")
    snapshot_list.add_argument("project")
    snapshot_list.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    snapshot_list.add_argument("--dimension")
    _json_option(snapshot_list)
    snapshot_remove = snapshot_commands.add_parser("remove")
    snapshot_remove.add_argument("project")
    snapshot_remove.add_argument("snapshot")
    snapshot_remove.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    snapshot_remove.add_argument("--dimension")
    snapshot_remove.add_argument("--expected-revision", type=int)
    snapshot_remove.add_argument("--expected-snapshots", help="comma-separated snapshot IDs from the confirmed inventory")
    _json_option(snapshot_remove)
    snapshot = commands.add_parser("snapshot")
    homebrew = commands.add_parser("homebrew")
    homebrew_commands = homebrew.add_subparsers(dest="homebrew_command", required=True)
    for action in ("plan-restore", "restore"):
        command = homebrew_commands.add_parser(action)
        command.add_argument("--archive", type=Path, required=True)
        command.add_argument("--prefix", type=Path, required=True)
        if action == "restore":
            command.add_argument("--confirm-manifest-sha256", required=True)
        _json_option(command)
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_commands.add_parser("create")
    snapshot_create.add_argument("project")
    snapshot_create.add_argument("--source", type=Path)
    snapshot_create.add_argument("--image", dest="images", action="append", default=[])
    snapshot_create.add_argument("--all-images", action="store_true")
    snapshot_create.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    snapshot_create.add_argument("--dimension")
    snapshot_create.add_argument("--editor-dirty", action="store_true", help=argparse.SUPPRESS)
    snapshot_create.add_argument(
        "--confirm-deletion",
        help="confirm the exact mass-deletion plan returned by snapshot preview",
    )
    snapshot_create.add_argument("--component-plan-token", help="validate reviewed component inputs and target")
    _json_option(snapshot_create)
    snapshot_preview = snapshot_commands.add_parser("preview")
    snapshot_preview.add_argument("project")
    snapshot_preview.add_argument("--source", type=Path)
    snapshot_preview.add_argument("--image", dest="images", action="append", default=[])
    snapshot_preview.add_argument("--all-images", action="store_true")
    snapshot_preview.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    snapshot_preview.add_argument("--dimension")
    _json_option(snapshot_preview)
    for command in (snapshot_create, snapshot_preview):
        command.add_argument("--hauler-manifest", dest="hauler_manifests", type=Path, action="append", default=[])
        command.add_argument("--hauler-file", dest="hauler_files", nargs=2, action="append", default=[], metavar=("PATH", "NAME"))
        command.add_argument("--brew-installed-prefix", type=Path, help="explicit qualified installed Homebrew closure; not a Brewfile recipe")
        command.add_argument("--brew-formula", help="qualified installed Hello or jq with its declared library/runtime closure")
        command.add_argument("--brew-directory", type=Path, help="Brewfile-only recipe export directory; never installed binaries")
        command.add_argument("--component-selection", help="versioned explicit Save component selection JSON")
        command.add_argument("--brew-archive", type=Path, help="reuse a saved Homebrew recovery archive in the native Room Store")
    installed_capabilities = snapshot_commands.add_parser("homebrew-capabilities")
    _json_option(installed_capabilities)
    brew_export = snapshot_commands.add_parser("export-homebrew")
    brew_export.add_argument("project")
    brew_export.add_argument("--snapshot", required=True)
    brew_export.add_argument("--dimension", required=True)
    brew_export.add_argument("--backend", choices=("r2","minio","s3"), default="minio")
    brew_export.add_argument("--output", type=Path, required=True)
    _json_option(brew_export)
    for action in ("inspect", "export", "serve", "extract"):
        snapshot_action = snapshot_commands.add_parser(action)
        snapshot_action.add_argument("project")
        snapshot_action.add_argument("--snapshot", default="latest")
        snapshot_action.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
        snapshot_action.add_argument("--dimension")
        if action == "export":
            snapshot_action.add_argument("--output", type=Path, required=True)
        elif action == "serve":
            snapshot_action.add_argument("--mode", choices=("auto", "files", "registry", "both"), default="auto")
            _add_serve_options(snapshot_action)
        elif action == "extract":
            snapshot_action.add_argument("reference")
            snapshot_action.add_argument("--destination", type=Path, required=True)
        _json_option(snapshot_action)
    snapshot_copy = snapshot_commands.add_parser("copy")
    snapshot_copy.add_argument("project", nargs="?")
    snapshot_copy.add_argument("--source-folder", type=Path)
    snapshot_copy.add_argument("--source-dimension", "--from-dimension", dest="source_dimension")
    snapshot_copy.add_argument("--destination-dimension", "--to-dimension", dest="destination_dimension", required=True)
    snapshot_copy.add_argument("--destination-room", "--destination-project", dest="destination_project", required=True)
    snapshot_copy.add_argument("--snapshot", default="latest")
    _json_option(snapshot_copy)
    hydration = commands.add_parser("hydrate")
    hydration.add_argument("project")
    hydration.add_argument("--snapshot", default="latest")
    hydration.add_argument("--destination", type=Path, required=True)
    hydration.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="terminal")
    hydration.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    hydration.add_argument("--dimension")
    _json_option(hydration)
    enter = commands.add_parser("enter")
    enter.add_argument("project", nargs="?")
    enter.add_argument("--snapshot", default="latest")
    enter.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    enter.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    enter.add_argument("--dimension")
    _json_option(enter)
    serve = commands.add_parser("serve")
    serve.add_argument("project")
    serve.add_argument("--snapshot", default="latest")
    serve.add_argument("--backend", choices=("local", "r2", "minio", "s3"), default="r2")
    serve.add_argument("--dimension")
    _add_serve_options(serve)
    _json_option(serve)
    room_store = commands.add_parser("room-store")
    room_store_commands = room_store.add_subparsers(dest="room_store_command", required=True)
    for action in ("verify", "optimize", "reconcile"):
        room_store_action = room_store_commands.add_parser(action)
        room_store_action.add_argument("--dimension", required=True)
        if action == "verify":
            room_store_action.add_argument("--read-data", action="store_true")
            room_store_action.add_argument("--read-data-subset")
        elif action == "optimize":
            room_store_action.add_argument("--confirm", action="store_true")
        _json_option(room_store_action)
    jat = commands.add_parser("jat")
    jat_commands = jat.add_subparsers(dest="jat_command", required=True)
    jat_build = jat_commands.add_parser("build")
    jat_build.add_argument("--source", type=Path, required=True)
    jat_build.add_argument("--output", type=Path, required=True)
    jat_build.add_argument("--image", dest="images", action="append", default=[])
    jat_build.add_argument("--all-images", action="store_true")
    jat_build.add_argument("--images-file", dest="images_files", action="append", default=[])
    jat_build.add_argument("--hauler-manifest", dest="hauler_manifests", action="append", default=[])
    jat_build.add_argument("--chunk-size", dest="chunk_size")
    jat_build.add_argument("--exclude-extras", action="store_true")
    jat_build.add_argument("--retries", type=int)
    _json_option(jat_build)
    jat_restore = jat_commands.add_parser("restore")
    jat_restore.add_argument("--haul", type=Path, required=True)
    jat_restore.add_argument("--destination", type=Path, required=True)
    _json_option(jat_restore)
    jat_inspect = jat_commands.add_parser("inspect")
    jat_inspect.add_argument("--haul", type=Path, required=True)
    _json_option(jat_inspect)
    jat_extract = jat_commands.add_parser("extract")
    jat_extract.add_argument("--haul", type=Path, required=True)
    jat_extract.add_argument("--reference", required=True)
    jat_extract.add_argument("--destination", type=Path, required=True)
    _json_option(jat_extract)
    jat_serve = jat_commands.add_parser("serve")
    jat_serve.add_argument("--haul", type=Path, required=True)
    jat_serve.add_argument("--mode", choices=("auto", "files", "registry", "both"), default="auto")
    _add_serve_options(jat_serve)
    _json_option(jat_serve)
    jat_export = jat_commands.add_parser("export")
    jat_export.add_argument("--haul", type=Path, required=True)
    jat_export.add_argument("--output", type=Path, required=True)
    _json_option(jat_export)
    jat_copy = jat_commands.add_parser("copy")
    jat_copy.add_argument("--haul", type=Path, required=True)
    jat_copy.add_argument("--to", required=True)
    jat_copy.add_argument("--retries", type=int)
    jat_copy.add_argument("--plain-http", dest="plain_http", action="store_true")
    jat_copy.add_argument("--insecure", action="store_true")
    _json_option(jat_copy)
    setup = commands.add_parser("setup")
    setup.add_argument("--profile", required=True)
    setup.add_argument("--age-profile", required=True)
    _json_option(setup)
    device = commands.add_parser("device", help="secure device enrollment and recipient authority")
    device_commands = device.add_subparsers(dest="device_command", required=True)
    device_inspect = device_commands.add_parser("inspect")
    device_inspect.add_argument("--profile")
    _json_option(device_inspect)
    device_doctor = device_commands.add_parser("doctor")
    _json_option(device_doctor)
    for action in ("setup", "enroll"):
        device_enroll = device_commands.add_parser(action, help="stdin carries credentials and age identity")
        device_enroll.add_argument("--profile")
        device_enroll.add_argument("--credential-profile", "--r2-credential-profile", dest="credential_profile")
        device_enroll.add_argument("--age-profile")
        device_enroll.add_argument("--recovery-profile")
        device_enroll.add_argument("--device-id")
        _json_option(device_enroll)
    device_rotate = device_commands.add_parser("rotate-new-writes", help="activate a recipient set for new writes")
    device_rotate.add_argument("--profile", required=True)
    device_rotate.add_argument("--credential-profile")
    _json_option(device_rotate)
    device_remove = device_commands.add_parser("remove-local", help="remove local enrollment and native-store secrets")
    device_remove.add_argument("--profile")
    _json_option(device_remove)
    view = commands.add_parser(
        "view",
        help="localhost read-only Room View dashboard and single-file offline HTML export",
    )
    view_commands = view.add_subparsers(dest="view_command", required=True)
    view_serve = view_commands.add_parser("serve", help="serve a localhost-only read-only Room View dashboard")
    view_serve.add_argument(
        "--provider",
        required=True,
        help="provider name (room, capsule-manifest, synthetic-unix, synthetic, entry point, or module:attr)",
    )
    view_serve.add_argument("--port", type=int, default=0, help="localhost port (0 = auto)")
    view_serve.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    view_serve.add_argument("--workspace", type=Path, help="Room workspace root (required for --provider room; also sensitivity policy)")
    view_serve.add_argument("--manifest", type=Path, help="LogicalJat JSON manifest path (required for --provider capsule-manifest)")
    view_serve.add_argument("--require-token", action="store_true", help="require a random per-session URL token")
    view_serve.add_argument("--max-seconds", type=float, help=argparse.SUPPRESS)
    _json_option(view_serve)
    view_export = view_commands.add_parser("export", help="export a single self-contained offline HTML view snapshot")
    view_source = view_export.add_mutually_exclusive_group(required=True)
    view_source.add_argument(
        "--provider",
        help="provider name (room, capsule-manifest, synthetic-unix, synthetic, entry point, or module:attr)",
    )
    view_source.add_argument("--from-state", type=Path, dest="from_state", help="offline/CI state JSON file")
    view_export.add_argument("--attachments-dir", type=Path, dest="attachments_dir", help="attachments directory for --from-state")
    view_export.add_argument("--output", type=Path, required=True, help="destination .html file path")
    view_export.add_argument("--no-attachments", action="store_true", help="omit attachment payloads")
    view_export.add_argument("--max-size", default="50MB", help="maximum allowed HTML snapshot size (e.g. 50MB)")
    view_export.add_argument("--sensitivity-ack", action="store_true", help="acknowledge exporting sensitive Room data")
    view_export.add_argument("--workspace", type=Path, help="Room workspace root (required for --provider room; also sensitivity policy)")
    view_export.add_argument("--manifest", type=Path, help="LogicalJat JSON manifest path (required for --provider capsule-manifest)")
    view_export.add_argument("--room-id", dest="room_id", help="optional sanitized Room identifier")
    _json_option(view_export)
    view_inspect = view_commands.add_parser("inspect", help="inspect an offline view snapshot without rendering")
    view_inspect.add_argument("file", type=Path, help="exported .html view snapshot")
    _json_option(view_inspect)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["agents"]:
        from .native_agents_cli import main as agents_main

        return agents_main(argv[1:])
    if argv[:1] == ["composition"] and argv[1:2] not in (["save"], ["restore-parent"], ["enter-child"]):
