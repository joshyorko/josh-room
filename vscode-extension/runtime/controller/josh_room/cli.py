import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

from . import r2 as _r2
from .auth import (
    EncryptionStateError,
    _valid_identity,
    cancel_oauth_session,
    ensure_minio_domain,
    ensure_runtime_session,
    load_runtime_session,
    logout_runtime_session,
    poll_oauth_session,
    r2_session_state,
    resolve_encryption_material,
    runtime_capabilities,
    runtime_session_state,
    start_oauth_session,
    wait_oauth_session,
)
from .catalog import Catalog
from .config import (
    DimensionRegistry,
    auth_status,
    connection_configs,
    persisted_config,
    private_config,
    save_private_config,
)
from .crypto import CryptoError, _managed_executable, decrypt
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
from .keyring import lookup_value as lookup_keyring_value
from .keyring import store as store_keyring
from .keyring import store_value as store_keyring_value
from .local_store import ImmutableLocalStore
from .minio import MinioBackend, MinioConfig
from .minio import check_bucket_access as check_minio_bucket
from .minio import create_bucket as create_minio_bucket
from .minio import list_buckets as list_minio_buckets
from .operations import (
    _read_remote_catalog,
    copy_snapshot_stream,
    create_snapshot,
    hydrate,
    link_workspace,
    remove_room,
    remove_snapshot,
    repair_workspace,
    serve_snapshot,
)
from .progress import report_progress
from .tls import initialize_system_trust
from .workspace_state import local_status

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josh-room")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    doctor.add_argument("--dimension")
    doctor.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    _json_option(doctor)
    connections = commands.add_parser("connections", help="legacy compatibility alias for provider connection")
    connection_commands = connections.add_subparsers(dest="connection_command", required=True)
    connection_list = connection_commands.add_parser("list")
    _json_option(connection_list)
    connection_setup = connection_commands.add_parser("setup")
    connection_setup.add_argument("provider", nargs="?", choices=("minio", "r2"))
    connection_setup.add_argument("--provider", dest="provider_option", choices=("minio", "r2"))
    connection_setup.add_argument("--connection", "--id", dest="connection_id")
    connection_setup.add_argument("--display-name")
    connection_setup.add_argument("--credential-profile", "--profile", dest="credential_profile")
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
    provider_connection_create.add_argument("--provider", required=True, choices=("minio", "r2"))
    provider_connection_create.add_argument("--endpoint", required=True)
    provider_connection_create.add_argument("--connection", "--id", dest="connection_id")
    provider_connection_create.add_argument("--credential-profile", "--profile", dest="credential_profile")
    _json_option(provider_connection_create)
    provider_connection_list = provider_connection_commands.add_parser("list")
    _json_option(provider_connection_list)
    for action in ("update", "reconnect", "disconnect"):
        provider_connection_edit = provider_connection_commands.add_parser(action, help="stdin carries credentials; legacy alias is also supported")
        provider_connection_edit.add_argument("--connection", required=True)
        provider_connection_edit.add_argument("--endpoint")
        provider_connection_edit.add_argument("--credential-profile", "--profile", dest="credential_profile")
        _json_option(provider_connection_edit)
    provider_bucket = provider_commands.add_parser("bucket")
    provider_bucket_commands = provider_bucket.add_subparsers(dest="provider_bucket_command", required=True)
    provider_bucket_list = provider_bucket_commands.add_parser("list")
    provider_bucket_list.add_argument("--connection")
    provider_bucket_list.add_argument("--provider", choices=("r2", "minio"))
    provider_bucket_list.add_argument("--dimension")
    _json_option(provider_bucket_list)
    provider_bucket_create = provider_bucket_commands.add_parser("create")
    provider_bucket_create.add_argument("--connection")
    provider_bucket_create.add_argument("--provider", choices=("r2", "minio"))
    provider_bucket_create.add_argument("--dimension")
    provider_bucket_create.add_argument("--bucket", required=True)
    _json_option(provider_bucket_create)
    provider_bucket_check = provider_bucket_commands.add_parser("check")
    provider_bucket_check.add_argument("--connection")
    provider_bucket_check.add_argument("--provider", choices=("r2", "minio"))
    provider_bucket_check.add_argument("--dimension")
    provider_bucket_check.add_argument("--bucket", required=True)
    _json_option(provider_bucket_check)
    dimensions = commands.add_parser("dimensions")
    dimension_commands = dimensions.add_subparsers(dest="dimension_command", required=True)
    dimension_list = dimension_commands.add_parser("list")
    dimension_list.add_argument("--dimension")
    dimension_list.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    dimension_list.add_argument("--with-hierarchy", action="store_true", help="read Rooms and JATs from each selected Dimension catalog")
    _json_option(dimension_list)
    for action in ("add", "update"):
        dimension_edit = dimension_commands.add_parser(action)
        dimension_edit.add_argument("dimension")
        dimension_edit.add_argument("--display-name")
        dimension_edit.add_argument("--provider", choices=("r2", "minio"))
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
    _json_option(auth_poll)
    auth_wait = auth_commands.add_parser("wait", help="wait for one OAuth session in this process")
    auth_wait.add_argument("session_id")
    auth_wait.add_argument("--dimension")
    auth_wait.add_argument("--purpose", choices=("encryption", "r2"))
    auth_wait.add_argument("--timeout", type=int, default=600)
    auth_wait.add_argument("--poll-interval", type=int, default=2)
    _json_option(auth_wait)
    auth_cancel = auth_commands.add_parser("cancel", help="invalidate one pending OAuth session")
    auth_cancel.add_argument("session_id")
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
    for action in ("status", "migrate", "resume"):
        encryption_action = encryption_commands.add_parser(action)
        encryption_action.add_argument("--dimension", required=True)
        _json_option(encryption_action)
    encryption_initialize = encryption_commands.add_parser("initialize")
    encryption_initialize.add_argument("--dimension", required=True)
    encryption_initialize.add_argument("--recovery-recipient", action="append", dest="recovery_recipients")
    encryption_initialize.add_argument("--recovery-handoff", type=Path)
    _json_option(encryption_initialize)
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    _json_option(status)
    for action in ("link", "repair"):
        state_command = commands.add_parser(action)
        state_command.add_argument("--workspace", type=Path, default=Path.cwd())
        state_command.add_argument("--dimension")
        state_command.add_argument("--project")
        state_command.add_argument("--snapshot")
        state_command.add_argument("--workspace-fingerprint")
        state_command.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
        _json_option(state_command)
    projects = commands.add_parser("projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    project_list.add_argument("--dimension")
    _json_option(project_list)
    rooms = commands.add_parser("rooms")
    room_commands = rooms.add_subparsers(dest="room_command", required=True)
    room_remove = room_commands.add_parser("remove")
    room_remove.add_argument("project")
    room_remove.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    room_remove.add_argument("--dimension")
    _json_option(room_remove)
    snapshots = commands.add_parser("snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshots_command", required=True)
    snapshot_list = snapshot_commands.add_parser("list")
    snapshot_list.add_argument("project")
    snapshot_list.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    snapshot_list.add_argument("--dimension")
    _json_option(snapshot_list)
    snapshot_remove = snapshot_commands.add_parser("remove")
    snapshot_remove.add_argument("project")
    snapshot_remove.add_argument("snapshot")
    snapshot_remove.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    snapshot_remove.add_argument("--dimension")
    _json_option(snapshot_remove)
    snapshot = commands.add_parser("snapshot")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_commands.add_parser("create")
    snapshot_create.add_argument("project")
    snapshot_create.add_argument("--source", type=Path)
    snapshot_create.add_argument("--image", dest="images", action="append", default=[])
    snapshot_create.add_argument("--all-images", action="store_true")
    snapshot_create.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    snapshot_create.add_argument("--dimension")
    _json_option(snapshot_create)
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
    hydration.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    hydration.add_argument("--dimension")
    _json_option(hydration)
    enter = commands.add_parser("enter")
    enter.add_argument("project", nargs="?")
    enter.add_argument("--snapshot", default="latest")
    enter.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    enter.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    enter.add_argument("--dimension")
    _json_option(enter)
    serve = commands.add_parser("serve")
    serve.add_argument("project")
    serve.add_argument("--snapshot", default="latest")
    serve.add_argument("--backend", choices=("local", "r2", "minio"), default="r2")
    serve.add_argument("--dimension")
    _json_option(serve)
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
    return parser


def main(argv=None):
    initialize_system_trust()
    args = build_parser().parse_args(argv)
    instance = _instance_root()
    try:
        runtime_loaded = False
        scoped_minio = _uses_minio_encryption(args)
        identity_context = nullcontext() if args.command in {"auth", "setup", "status", "encryption"} or scoped_minio else _identity_environment()
        with identity_context:
            if args.command not in {"auth", "setup", "encryption"} and not scoped_minio:
                runtime_loaded = load_runtime_session()
            with _selected_encryption_environment(args, instance) if scoped_minio else nullcontext():
                if _requires_oauth(args):
                    requested_dimension = getattr(args, "dimension", None)
                    selected = None
                    if getattr(args, "snapshot_command", None) != "copy":
                        try:
                            selected = _effective_dimension(args)
                        except ValueError:
                            if requested_dimension != "r2":
                                raise
                    ensure_runtime_session(dimension_id=selected.dimension_id if selected else requested_dimension)
                elif _requires_encryption(args) and args.command != "dimensions" and not runtime_loaded and not _encryption_material_ready():
                    raise _encryption_authorization_required()
                result = dispatch(args, instance)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        result = {"ok": False, "error": str(error)}
        if isinstance(getattr(error, "result", None), dict):
            result.update(error.result)
    _write_runtime_result(result)
    emit(result, getattr(args, "json", False))
    return 0 if result["ok"] else 2


def _write_runtime_result(result):
    target_value = os.environ.get("JOSH_ROOM_RESULT_FILE")
    if not target_value:
        return
    target = Path(target_value)
    if target.is_symlink():
        raise RuntimeError("runtime result path must not be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(result, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _requires_oauth(args) -> bool:
    if args.command == "provider" and args.provider_command == "bucket":
        if getattr(args, "provider", None) == "r2":
            return True
        if getattr(args, "dimension", None):
            try:
                return DimensionRegistry(private_config() or {}).select(args.dimension).provider == "r2"
            except ValueError:
                return args.dimension == "r2"
        return False
    if args.command == "dimensions":
        if not getattr(args, "with_hierarchy", False):
            return False
        try:
            registry = DimensionRegistry(private_config() or {})
            if getattr(args, "dimension", None):
                return registry.select(args.dimension).provider == "r2"
            return any(dimension.provider == "r2" for dimension in registry.dimensions.values())
        except ValueError:
            return getattr(args, "backend", "r2") == "r2"
    if getattr(args, "snapshot_command", None) == "copy" and _copy_has_minio_dimension(args):
        return False
    if args.command not in {"projects", "rooms", "snapshots", "snapshot", "hydrate", "enter", "serve", "link", "repair"}:
        return False
    if getattr(args, "snapshot_command", None) == "copy":
        source_dimension = _copy_source_dimension(args)
        dimensions = [dimension for dimension in (source_dimension, args.destination_dimension) if dimension]
        try:
            registry = DimensionRegistry(private_config() or {})
            return any(registry.select(dimension).provider == "r2" for dimension in dimensions)
        except ValueError:
            return "r2" in dimensions
    dimension = getattr(args, "dimension", None)
    if dimension:
        try:
            return DimensionRegistry(private_config() or {}).select(dimension).provider == "r2"
        except ValueError:
            return dimension == "r2"
    try:
        selected = _effective_dimension(args)
    except ValueError:
        selected = None
    if selected is not None:
        return selected.provider == "r2"
    return getattr(args, "backend", "r2") == "r2"


def _requires_encryption(args) -> bool:
    if args.command == "dimensions":
        return getattr(args, "with_hierarchy", False) and _dimensions_have_provider(args, "minio")
    if args.command not in {"projects", "rooms", "snapshots", "snapshot", "hydrate", "enter", "serve", "link", "repair"}:
        return False
    if args.command == "snapshot" and getattr(args, "snapshot_command", None) not in {"create", "copy"}:
        return False
    if getattr(args, "snapshot_command", None) == "copy":
        return False
    if args.command == "snapshots" and getattr(args, "snapshots_command", None) not in {"list", "remove"}:
        return False
    dimension = getattr(args, "dimension", None)
    if dimension:
        try:
            return DimensionRegistry(private_config() or {}).select(dimension).provider == "minio"
        except ValueError:
            return dimension == "minio"
    if getattr(args, "snapshot_command", None) == "copy":
        dimensions = [_copy_source_dimension(args), getattr(args, "destination_dimension", None)]
        try:
            registry = DimensionRegistry(private_config() or {})
            return any(value and registry.select(value).provider == "minio" for value in dimensions)
        except ValueError:
            return "minio" in dimensions
    try:
        selected = _effective_dimension(args)
    except ValueError:
        selected = None
    if selected is not None:
        return selected.provider == "minio"
    return getattr(args, "backend", "r2") == "minio"


def _uses_minio_encryption(args) -> bool:
    if args.command == "encryption":
        return False
    if args.command == "dimensions":
        return False
    if getattr(args, "snapshot_command", None) == "copy":
        return False
    if not _requires_encryption(args):
        return False
    try:
        selected = _effective_dimension(args)
    except ValueError:
        return False
    return selected is not None and selected.provider == "minio"


@contextmanager
def _selected_encryption_environment(args, instance: Path):
    selected = _effective_dimension(args)
    backend = _backend_for_args(args, instance)
    material = resolve_encryption_material(selected, backend)
    with _encryption_material_environment(material):
        yield material


@contextmanager
def _encryption_material_environment(material):
    previous = {
        name: os.environ.get(name)
        for name in ("JOSH_ROOM_IDENTITY", "JOSH_ROOM_ENCRYPTION_MATERIAL", "JOSH_ROOM_SELECTED_RECIPIENTS", "JOSH_ROOM_SELECTED_DOMAIN")
    }
    os.environ["JOSH_ROOM_IDENTITY"] = str(material.identity)
    os.environ["JOSH_ROOM_ENCRYPTION_MATERIAL"] = str(material.identity)
    os.environ["JOSH_ROOM_SELECTED_RECIPIENTS"] = ",".join(
        (material.recipient, *material.keyset.recovery_recipients)
    )
    os.environ["JOSH_ROOM_SELECTED_DOMAIN"] = material.encryption_domain_id
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _encryption_material_ready() -> bool:
    identity = os.environ.get("JOSH_ROOM_IDENTITY")
    if not identity:
        return False
    try:
        path = Path(identity)
        identity_ready = path.is_file() and not path.is_symlink() and not (path.stat().st_mode & 0o077) \
            and _valid_identity(path.read_text())
    except OSError:
        identity_ready = False
    recipients = _recipients()
    return identity_ready and len(recipients) >= 2 and len(set(recipients)) >= 2


def _copy_source_dimension(args) -> str | None:
    source_folder = getattr(args, "source_folder", None)
    if not source_folder:
        return getattr(args, "source_dimension", None)
    status = local_status(source_folder)
    if not status.get("ok") or status.get("state") != "clean" or not status.get("dimension_id"):
        return None
    return status["dimension_id"]


def _copy_dimension_ids(args) -> list[str]:
    return [value for value in (_copy_source_dimension(args), getattr(args, "destination_dimension", None)) if value]


def _copy_has_minio_dimension(args) -> bool:
    dimensions = _copy_dimension_ids(args)
    try:
        registry = DimensionRegistry(private_config() or {})
        return any(registry.select(value).provider == "minio" for value in dimensions)
    except ValueError:
        return "minio" in dimensions


def _reject_minio_copy(args):
    dimensions = _copy_dimension_ids(args)
    if not dimensions:
        return
    try:
        registry = DimensionRegistry(private_config() or {})
        selected = [registry.select(value) for value in dimensions]
    except ValueError:
        selected = []
    if any(dimension.provider == "minio" for dimension in selected) or "minio" in dimensions:
        raise EncryptionStateError(
            "copy across encryption domains is not supported yet",
            error_code="unsupported-mixed-domain",
            state="unsupported-mixed-domain",
            dimension_id=dimensions[0],
            dimension_ids=dimensions,
        )


def _dimensions_have_provider(args, provider: str) -> bool:
    try:
        registry = DimensionRegistry(private_config() or {})
        if getattr(args, "dimension", None):
            return registry.select(args.dimension).provider == provider
        return any(dimension.provider == provider for dimension in registry.dimensions.values())
    except ValueError:
        return getattr(args, "backend", None) == provider


def _marker_dimension(args) -> str | None:
    if getattr(args, "command", None) not in {"link", "repair"} or getattr(args, "dimension", None):
        return None
    status = local_status(args.workspace)
    if status.get("ok") and status.get("dimension_id"):
        return status["dimension_id"]
    return None


def _bucket_target(args, config):
    dimension_id = getattr(args, "dimension", None)
    if dimension_id:
        dimension = DimensionRegistry(config).select(dimension_id)
        if getattr(args, "provider", None) and args.provider != dimension.provider:
            raise ValueError("bucket provider does not match the selected Dimension")
        return dimension.provider, dimension, None
    if getattr(args, "provider", None) == "r2":
        return "r2", DimensionRegistry(config).select("r2"), None
    connection_id = getattr(args, "connection", None)
    if not connection_id:
        raise ValueError("bucket operations require --connection or an R2 --dimension")
    connection = connection_configs(config).get(connection_id)
    if connection is None:
        raise ValueError(f"connection {connection_id} is missing")
    return connection.provider, None, connection


def _bucket_operation(args, config):
    provider, dimension, connection = _bucket_target(args, config)
    if provider == "r2":
        if dimension is None:
            raise ValueError("R2 bucket operations require a Dimension")
        target = _r2.R2Config.from_dimension(dimension)
        connection_id = dimension.dimension_id
        if args.provider_bucket_command == "list":
            buckets = _r2.list_buckets(target)
        elif args.provider_bucket_command == "create":
            buckets = _r2.create_bucket(target, args.bucket)
        else:
            buckets = _r2.check_bucket_access(target, args.bucket)
    else:
        connection_id = connection.connection_id
        if args.provider_bucket_command == "list":
            buckets = list_minio_buckets(connection)
        elif args.provider_bucket_command == "create":
            buckets = create_minio_bucket(connection, args.bucket)
        else:
            buckets = check_minio_bucket(connection, args.bucket)
    if args.provider_bucket_command == "list":
        result = {"ok": True, "connection_id": connection_id, "provider": provider, "buckets": buckets}
        if dimension is not None:
            result.update({
                "endpoint": dimension.endpoint,
                "credential_profile": dimension.credential_profile,
                "region": dimension.region,
            })
        return result
    return {
        "ok": True,
        "connection_id": connection_id,
        "provider": provider,
        "bucket": buckets,
        "created": args.provider_bucket_command == "create",
        "accessible": args.provider_bucket_command == "check",
    }


def dispatch(args, instance: Path) -> dict:
    if args.command == "encryption":
        config = private_config() or {}
        dimension = DimensionRegistry(config).select(args.dimension)
        backend = _backend(dimension.provider, instance, dimension.dimension_id)
        if args.encryption_command == "status":
            from .auth import encryption_status

            return {"ok": True, **encryption_status(dimension, backend)}
        if args.encryption_command == "initialize":
            if dimension.provider != "minio":
                raise ValueError("encryption initialization is only available for MinIO Dimensions")
            recipients = args.recovery_recipients or []
            material = ensure_minio_domain(
                dimension,
                backend,
                recovery_recipients=recipients,
                recovery_handoff=args.recovery_handoff,
            )
            os.environ["JOSH_ROOM_IDENTITY"] = str(material.identity)
            os.environ["JOSH_ROOM_ENCRYPTION_MATERIAL"] = str(material.identity)
            os.environ["JOSH_ROOM_SELECTED_RECIPIENTS"] = ",".join(
                (material.recipient, *material.keyset.recovery_recipients)
            )
            return {
                "ok": True,
                "state": "ready",
                "provider": dimension.provider,
                "dimension_id": dimension.dimension_id,
                "encryption_domain_id": material.encryption_domain_id,
                "key_generation": material.key_generation,
            }
        raise EncryptionStateError(
            "encryption migration actions are handled by the migration workflow",
            error_code="encryption-migration-not-implemented",
            state="legacy",
            dimension_id=dimension.dimension_id,
        )
    if args.command == "auth":
        if args.auth_command == "start":
            started = start_oauth_session() if args.purpose == "r2" else start_oauth_session(args.purpose)
            return {"ok": True, **started}
        if args.auth_command == "poll":
            kwargs = {"dimension_id": args.dimension}
            if args.purpose is not None:
                kwargs["purpose"] = args.purpose
            return {"ok": True, **poll_oauth_session(args.session_id, **kwargs)}
        if args.auth_command == "wait":
            kwargs = {
                "timeout": args.timeout,
                "poll_interval": args.poll_interval,
                "dimension_id": args.dimension,
            }
            if args.purpose is not None:
                kwargs["purpose"] = args.purpose
            return {"ok": True, **wait_oauth_session(args.session_id, **kwargs)}
        if args.auth_command == "cancel":
            return {"ok": True, **cancel_oauth_session(args.session_id)}
        if args.auth_command == "logout":
            return {"ok": True, **logout_runtime_session(args.purpose), "logged_out": True, "dimension_id": args.dimension}
        state = runtime_session_state()
        capabilities = list(runtime_capabilities()) if state == "connected" else []
        return {
            "ok": True,
            "state": state,
            "encryption_state": state,
            "r2_state": r2_session_state(),
            "capabilities": capabilities,
            "dimension_id": args.dimension,
        }
    if args.command == "provider":
        config = private_config() or {}
        if args.provider_command == "connection":
            action = args.provider_connection_command
            if action == "list":
                return {"ok": True, "connections": [_connection_metadata(key, value) for key, value in connection_configs(config).items()]}
            config = persisted_config() or {}
            if action == "create":
                payload = json.load(sys.stdin)
                if not isinstance(payload, dict) or args.provider != "minio":
                    raise ValueError("provider connection create supports MinIO JSON credentials only")
                endpoint = args.endpoint
                connection_id = args.connection_id or _minio_connection_id(endpoint)
                profile = args.credential_profile or f"josh-room-{connection_id}"
                credentials = _connection_credentials(payload)
                records = dict(config.get("connections", {}))
                connection = {
                    "display_name": payload.get("display_name", "MinIO"),
                    "provider": "minio",
                    "endpoint": endpoint,
                    "credential_profile": profile,
                    "region": payload.get("region", "us-east-1"),
                }
                connection_configs({"connections": {connection_id: connection}})
                _store_credentials(profile, credentials)
                config["connections"] = {**records, connection_id: connection}
                save_private_config(config)
                return {"ok": True, "connection": {"id": connection_id, **connection}, "stored": True}
            connection = connection_configs(config).get(args.connection)
            if connection is None:
                raise ValueError(f"connection {args.connection} is missing")
            if action == "disconnect":
                records = dict(config.get("connections", {}))
                records[connection.connection_id] = {**connection.to_private(), "auth_state": "disconnected"}
                config["connections"] = records
                save_private_config(config)
                return {"ok": True, "connection": connection.connection_id, "disconnected": True}
            payload = json.load(sys.stdin)
            credentials = _connection_credentials(payload)
            endpoint = args.endpoint or connection.endpoint
            profile = args.credential_profile or connection.credential_profile
            updated = {
                **connection.to_private(),
                "endpoint": endpoint,
                "credential_profile": profile,
                "auth_state": "configured",
            }
            connection_configs({"connections": {connection.connection_id: updated}})
            _store_credentials(profile, credentials)
            records = dict(config.get("connections", {}))
            records[connection.connection_id] = updated
            config["connections"] = records
            save_private_config(config)
            return {"ok": True, "connection": connection.connection_id, "reconnected": action == "reconnect", "updated": action == "update"}
        if args.provider_bucket_command == "list":
            return _bucket_operation(args, config)
        return _bucket_operation(args, config) | ({"accessible": True} if args.provider_bucket_command == "check" else {})
    if args.command == "connections":
        config = private_config() or {}
        if args.connection_command == "list":
            return {
                "ok": True,
                "connections": [
                    {
                        "id": key,
                        "display_name": value.display_name,
                        "provider": value.provider,
                        "endpoint": value.endpoint,
                        "credential_profile": value.credential_profile,
                        "auth_state": value.auth_state,
                    }
                    for key, value in connection_configs(config).items()
                ],
            }
        config = persisted_config() or {}
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("connection setup input must be an object")
        provider = args.provider_option or args.provider or payload.get("provider")
        if provider != "minio":
            raise ValueError("connections setup currently supports MinIO only")
        connection_id = args.connection_id or payload.get("connection_id") or payload.get("id")
        profile = args.credential_profile or payload.get("credential_profile") or payload.get("profile")
        endpoint = payload.get("endpoint")
        access_key = payload.get("access-key-id", payload.get("access_key_id"))
        secret_key = payload.get("secret-access-key", payload.get("secret_access_key"))
        if not all(isinstance(value, str) and value for value in (connection_id, profile, endpoint, access_key, secret_key)):
            raise ValueError("MinIO connection setup requires id, credential profile, endpoint, access key, and secret key")
        records = dict(config.get("connections", {}))
        if connection_id in records:
            raise ValueError(f"connection {connection_id} already exists")
        connection = {
            "display_name": args.display_name or payload.get("display_name") or "MinIO",
            "provider": "minio",
            "endpoint": endpoint,
            "credential_profile": profile,
            "region": payload.get("region", "us-east-1"),
        }
        connection_configs({"connections": {connection_id: connection}})
        _store_credentials(profile, {"access-key-id": access_key, "secret-access-key": secret_key})
        config["connections"] = {**records, connection_id: connection}
        save_private_config(config)
        return {"ok": True, "connection": connection_id, "stored": True}
    if args.command == "buckets":
        connection = connection_configs(private_config() or {}).get(args.connection)
        if connection is None:
            raise ValueError(f"connection {args.connection} is missing")
        if connection.provider != "minio":
            raise ValueError("bucket operations currently support MinIO connections only")
        if args.bucket_command == "list":
            return {"ok": True, "connection_id": connection.connection_id, "buckets": list_minio_buckets(connection)}
        if args.bucket_command == "check":
            return {
                "ok": True,
                "connection_id": connection.connection_id,
                "bucket": check_minio_bucket(connection, args.bucket),
                "accessible": True,
            }
        return {
            "ok": True,
            "connection_id": connection.connection_id,
            "bucket": create_minio_bucket(connection, args.bucket),
            "created": True,
        }
    if args.command == "dimensions":
        config = private_config() or {}
        if args.dimension_command == "list":
            registry = DimensionRegistry(config)
            dimensions = list(registry)
            if args.with_hierarchy:
                if args.dimension:
                    dimensions = [(args.dimension, registry.select(args.dimension))]
                return {
                    "ok": True,
                    "dimensions": [
                        _dimension_hierarchy_with_encryption(instance, dimension)
                        for dimension_id, dimension in dimensions
                    ],
                }
            return {"ok": True, "dimensions": [_dimension_metadata(key, value) for key, value in dimensions]}
        config = persisted_config() or {}
        records = dict(config.get("dimensions", {}))
        if args.dimension_command == "add" and args.dimension in records:
            raise ValueError(f"Dimension {args.dimension} already exists")
        current = records.get(args.dimension, {})
        values = {**current}
        for field, key in (("display_name", "display_name"), ("provider", "provider"), ("connection_id", "connection_id"), ("endpoint", "endpoint"), ("bucket", "bucket"), ("credential_profile", "credential_profile"), ("region", "region"), ("catalog_key", "catalog_key")):
            value = getattr(args, field.replace("-", "_"), None)
            if value is not None:
                values[key] = value
        records[args.dimension] = values
        DimensionRegistry({**config, "dimensions": records}).get(args.dimension)
        config["dimensions"] = records
        save_private_config(config)
        return {"ok": True, "dimension": args.dimension, "updated": True}
    if args.command == "status":
        return {"ok": True, **local_status(args.workspace)}
    if args.command in {"link", "repair"}:
        from .workspace_state import read_workspace_marker
        marker = None
        marker_path = Path(args.workspace) / ".josh-room.json"
        if marker_path.is_file():
            try:
                marker = read_workspace_marker(args.workspace)
            except ValueError:
                if not all((args.project, args.snapshot, args.dimension)):
                    raise
        selected = _effective_dimension(args)
        selected_dimension = args.dimension or (marker or {}).get("dimension_id") or (selected.dimension_id if selected else None)
        backend = _backend(args.backend, instance, selected_dimension)
        catalog = load_catalog(instance, backend)
        project_id = args.project or (marker or {}).get("project_id")
        snapshot_id = args.snapshot or (marker or {}).get("snapshot_id")
        dimension_id = selected_dimension or catalog.dimension_id
        if not project_id or not snapshot_id or not dimension_id:
            raise ValueError("Link and Repair require project, snapshot, and Dimension evidence")
        snapshot = catalog.resolve_snapshot(project_id, snapshot_id)
        object_evidence = {
            "project_id": project_id,
            "snapshot_id": snapshot["snapshot_id"],
            "object_key": snapshot["object_key"],
            "ciphertext_sha256": snapshot["ciphertext_sha256"],
            "ciphertext_size": snapshot["ciphertext_size"],
        }
        if backend:
            backend.verify_object(object_evidence["object_key"], object_evidence["ciphertext_sha256"], object_evidence["ciphertext_size"])
        else:
            ImmutableLocalStore(instance).verify(object_evidence["object_key"], object_evidence["ciphertext_sha256"], object_evidence["ciphertext_size"])
        if args.command == "link":
            return link_workspace(
                args.workspace,
                catalog,
                object_evidence,
                project_id=project_id,
                snapshot_id=snapshot_id,
                dimension_id=dimension_id,
                verified_workspace_fingerprint=args.workspace_fingerprint,
            )
        return repair_workspace(
            args.workspace,
            catalog,
            object_evidence,
            project_id=project_id,
            snapshot_id=snapshot_id,
            dimension_id=dimension_id,
        )
    if args.command == "doctor":
        selected = _effective_dimension(args)
        return _doctor(instance, args.backend, args.ide, dimension=selected.dimension_id if selected else None)
    if args.command == "projects":
        backend = _backend_for_args(args, instance)
        projects = list_projects(instance, backend)
        dimension_id = getattr(getattr(backend, "config", None), "dimension_id", None) or getattr(args, "dimension", None) or ("local" if backend is None else None)
        return {"ok": True, "dimension_id": dimension_id, "projects": [{"id": project_id, "display_name": name} for project_id, name in projects]}
    if args.command == "rooms":
        recipients = _recipients()
        if len(recipients) < 2:
            raise ValueError("room removal requires two age recipients")
        identity = os.environ.get("JOSH_ROOM_IDENTITY")
        if not identity:
            raise ValueError("room removal requires an age identity")
        return {
            "ok": True,
            **remove_room(instance, args.project, Path(identity), recipients, _backend_for_args(args, instance)),
        }
    if args.command == "snapshots":
        if args.snapshots_command == "remove":
            recipients = _recipients()
            identity = os.environ.get("JOSH_ROOM_IDENTITY")
            if len(recipients) < 2 or not identity:
                raise ValueError("snapshot removal requires an age identity and two recipients")
            return {
                "ok": True,
                **remove_snapshot(
                    instance,
                    args.project,
                    args.snapshot,
                    Path(identity),
                    recipients,
                    _backend_for_args(args, instance),
                ),
            }
        backend = _backend_for_args(args, instance)
        catalog = load_catalog(instance, backend)
        project = catalog.body["projects"].get(args.project)
        if not project:
            raise ValueError("project is not present in the encrypted catalog")
        dimension_id = catalog.dimension_id or getattr(getattr(backend, "config", None), "dimension_id", None) or getattr(args, "dimension", None) or ("local" if backend is None else None)
        return {"ok": True, "dimension_id": dimension_id, "project": args.project, "latest": project["latest"], "snapshots": list(project["snapshots"].values())}
    if args.command == "snapshot":
        if args.snapshot_command == "copy":
            _reject_minio_copy(args)
            identity = _identity()
            recipients = _recipients()
            source_project = args.project
            source_snapshot = args.snapshot
            source_dimension = args.source_dimension
            if args.source_folder:
                if args.project or args.source_dimension or args.snapshot != "latest":
                    raise ValueError("source-folder cannot be combined with source project or dimension")
                folder_status = local_status(args.source_folder)
                if not folder_status.get("ok") or folder_status.get("state") != "clean" or not folder_status.get("dimension_id"):
                    raise ValueError("source folder must have a clean saved v2 workspace marker")
                source_project = folder_status["project_id"]
                source_snapshot = folder_status["snapshot_id"]
                source_dimension = folder_status["dimension_id"]
            elif not source_project or not source_dimension:
                raise ValueError("source project and source dimension are required unless --source-folder is used")
            source_backend = _backend("r2", instance, source_dimension)
            destination_backend = _backend("r2", instance, args.destination_dimension)
            source_catalog, _source_etag = _read_remote_catalog(source_backend, identity, instance)
            destination_catalog, destination_etag = _read_remote_catalog(destination_backend, identity, instance)
            return copy_snapshot_stream(instance, source_catalog, destination_catalog, source_backend, destination_backend, source_project, args.destination_project, source_snapshot, recipients, destination_etag=destination_etag)
        recipients = _recipients()
        jat_root = _jat_root()
        if len(recipients) < 2 or len(set(recipients)) < 2:
            raise ValueError("snapshot create requires encryption authorization with two age recipients")
        project_id, display_name = _room_identity(args.project)
        return {
            "ok": True,
            **create_snapshot(
                instance,
                project_id,
                args.source or Path.cwd(),
                jat_root,
                recipients,
                _backend_for_args(args, instance),
                display_name=display_name,
                images=args.images,
                all_images=args.all_images,
            ),
        }
    if args.command == "hydrate":
        return hydrate_command(args, instance)
    if args.command == "enter":
        backend = _backend_for_args(args, instance)
        project = args.project or choose_project(instance, backend)
        destination = _workspace_root() / project
        result = hydrate_command(argparse.Namespace(project=project, snapshot=args.snapshot, destination=destination, ide=args.ide, backend=args.backend), instance, backend)
        if result["ok"] and args.ide != "terminal":
            executable = "code-insiders" if args.ide == "vscode-insiders" else "code"
            if not shutil.which(executable):
                return {"ok": False, "error": f"IDE executable unavailable: {executable}", "hydrated": True}
            resolved = shutil.which(executable)
            _launch_ide(resolved, destination)
            result["launch"] = executable
        return result
    if args.command == "serve":
        identity = os.environ.get("JOSH_ROOM_IDENTITY")
        if not identity:
            raise ValueError("serve requires an age identity")
        return {
            "ok": True,
            **serve_snapshot(
                instance,
                args.project,
                args.snapshot,
                Path(identity),
                _jat_root(),
                _backend_for_args(args, instance),
            ),
        }
    if args.command == "jat":
        jat_root = _jat_root()
        if args.jat_command == "build":
            return {
                "ok": True,
                **run_build(
                    jat_root,
                    args.source,
                    args.output,
                    images=args.images,
                    all_images=args.all_images,
                    images_files=args.images_files,
                    hauler_manifests=args.hauler_manifests,
                    chunk_size=args.chunk_size,
                    exclude_extras=args.exclude_extras,
                    retries=args.retries,
                ),
            }
        if args.jat_command == "restore":
            return {"ok": True, **run_restore(jat_root, args.haul, args.destination)}
        if args.jat_command == "inspect":
            return {"ok": True, **run_inspect(jat_root, args.haul)}
        if args.jat_command == "extract":
            return {"ok": True, **run_extract(jat_root, args.haul, args.reference, args.destination)}
        if args.jat_command == "serve":
            return {"ok": True, **run_serve(jat_root, args.haul, mode=args.mode)}
        if args.jat_command == "export":
            return {"ok": True, **run_export(jat_root, args.haul, args.output)}
        if args.jat_command == "copy":
            return {
                "ok": True,
                **run_copy(
                    jat_root,
                    args.haul,
                    args.to,
                    retries=args.retries,
                    plain_http=args.plain_http,
                    insecure=args.insecure,
                ),
            }
    if args.command == "setup":
        credentials = json.load(sys.stdin)
        required = {
            "access-key-id",
            "secret-access-key",
            "endpoint",
            "bucket",
            "age-identity",
            "age-recipients",
        }
        if not required.issubset(credentials) or len(credentials["age-recipients"]) < 2:
            raise ValueError("setup input requires R2 credentials, endpoint, bucket, age identity, and two recipients")
        _store_credentials(args.profile, credentials)
        _store_identity(args.age_profile, credentials["age-identity"])
        config = {
            "default_backend": "r2",
            "default_ide": "vscode-insiders",
            "workspace_root": credentials.get("workspace-root", str(Path.home() / "workspaces")),
            "jat_root": credentials.get("jat-root", str(Path.home() / ".local/share/josh-room/josh-all-the-things")),
            "age_identity_profile": args.age_profile,
            "age_recipients": credentials["age-recipients"],
            "r2": {
                "endpoint": credentials["endpoint"],
                "bucket": credentials["bucket"],
                "region": credentials.get("region", "auto"),
                "credential_profile": args.profile,
                "catalog_key": "catalog.jroom.age",
                "temporary_credentials": True,
            },
        }
        save_private_config(config)
        return {"ok": True, "profile": args.profile, "age_profile": args.age_profile, "stored": True}
    raise ValueError("unsupported command")


def _connection_metadata(connection_id, connection):
    return {
        "id": connection_id,
        "display_name": connection.display_name,
        "provider": connection.provider,
        "endpoint": connection.endpoint,
        "credential_profile": connection.credential_profile,
        "auth_state": connection.auth_state,
    }


def _dimension_metadata(dimension_id, dimension):
    return {
        "id": dimension_id,
        "display_name": dimension.display_name,
        "provider": dimension.provider,
        "connection_id": dimension.connection_id,
        "bucket": dimension.bucket,
        "endpoint": dimension.endpoint,
    }


def _dimension_hierarchy(instance, dimension, backend):
    catalog = load_catalog(instance, backend)
    if catalog.dimension_id is not None and catalog.dimension_id != dimension.dimension_id:
        raise ValueError(f"catalog Dimension mismatch: expected {dimension.dimension_id}, received {catalog.dimension_id}")
    rooms = []
    for project_id, project in catalog.body["projects"].items():
        rooms.append({
            "id": project_id,
            "display_name": project["display_name"],
            "latest": project["latest"],
            "jats": list(project["snapshots"].values()),
        })
    return {**_dimension_metadata(dimension.dimension_id, dimension), "rooms": rooms}


def _dimension_hierarchy_with_encryption(instance, dimension):
    backend = _backend(dimension.provider, instance, dimension.dimension_id)
    if dimension.provider != "minio":
        return _dimension_hierarchy(instance, dimension, backend)
    material = resolve_encryption_material(dimension, backend, allow_initialize=False)
    with _encryption_material_environment(material):
        return _dimension_hierarchy(instance, dimension, backend)


def _connection_credentials(payload):
    if not isinstance(payload, dict):
        raise TypeError("connection credentials input must be an object")
    access_key = payload.get("access-key-id", payload.get("access_key_id"))
    secret_key = payload.get("secret-access-key", payload.get("secret_access_key"))
    if not all(isinstance(value, str) and value for value in (access_key, secret_key)):
        raise ValueError("connection input requires access key and secret key")
    return {"access-key-id": access_key, "secret-access-key": secret_key}


def _minio_connection_id(endpoint):
    slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://", "", endpoint.lower())).strip("-")
    return f"minio-{slug or 'connection'}"


def hydrate_command(args, instance: Path, backend=None) -> dict:
    identity = os.environ.get("JOSH_ROOM_IDENTITY")
    jat_root = _jat_root()
    if not identity:
        raise ValueError("hydrate requires JOSH_ROOM_IDENTITY and JOSH_ROOM_JAT_ROOT")
    if backend is None:
        backend = _backend_for_args(args, instance)
    return {
        "ok": True,
        **hydrate(
            instance,
            args.project,
            args.destination,
            Path(identity),
            jat_root,
            backend,
            snapshot_id=args.snapshot,
        ),
    }


def _backend_for_args(args, instance: Path):
    selected = _effective_dimension(args)
    if not selected:
        return _backend(args.backend, instance)
    configured = private_config() or {}
    named = isinstance(configured.get("dimensions"), dict) and selected.dimension_id in configured["dimensions"]
    if not named and not getattr(args, "dimension", None) and selected.provider == args.backend == selected.dimension_id:
        return _backend(args.backend, instance)
    return _backend(selected.provider, instance, selected.dimension_id)


def _effective_dimension(args):
    config = private_config() or {}
    requested = getattr(args, "dimension", None)
    backend = getattr(args, "backend", "r2")
    registry = DimensionRegistry(config)
    if requested:
        return registry.select(requested)
    marker = _marker_dimension(args)
    if marker:
        return registry.select(marker)
    if backend == "local":
        return None
    if backend != "r2":
        return registry.select(backend)
    if config.get("default_dimension") or config.get("dimensions") or config.get("r2"):
        return registry.select()
    return None


def _backend(name: str, instance: Path, dimension: str | None = None):
    config = private_config() or {}
    if dimension:
        selected = DimensionRegistry(config).select(dimension)
        name = selected.provider
    if name == "local":
        return None
    if name == "minio":
        return MinioBackend(MinioConfig.from_private(config, dimension), receipt_dir=instance / "receipts")
    return R2Backend(R2Config.from_private(config, dimension), receipt_dir=instance / "receipts")


def _configured() -> dict:
    return private_config() or {}


def _instance_root() -> Path:
    explicit = os.environ.get("JOSH_ROOM_INSTANCE")
    if explicit:
        return Path(explicit)
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "josh-room"


def _jat_root() -> Path:
    value = os.environ.get("JOSH_ROOM_JAT_ROOT") or _configured().get("jat_root")
    return Path(value) if value else Path.home() / ".local/share/josh-room/josh-all-the-things"


def _recipients() -> list[str]:
    selected = os.environ.get("JOSH_ROOM_SELECTED_RECIPIENTS")
    if selected:
        return [item for item in selected.split(",") if item]
    environment = [item for item in os.environ.get("JOSH_ROOM_RECIPIENTS", "").split(",") if item]
    return environment or list(_configured().get("age_recipients", []))


def _workspace_root() -> Path:
    value = os.environ.get("JOSH_ROOM_WORKSPACE_ROOT") or _configured().get("workspace_root")
    if value:
        return Path(value)
    current = Path.cwd()
    if current.name == "room":
        return current.parent
    return current


@contextmanager
def _identity_environment():
    existing = os.environ.get("JOSH_ROOM_IDENTITY")
    if existing:
        yield
        return
    profile = _configured().get("age_identity_profile")
    if not profile:
        yield
        return
    try:
        identity = lookup_keyring_value(profile, "age-identity")
    except RuntimeError:
        yield
        return
    with tempfile.NamedTemporaryFile(mode="w", prefix="josh-room-age-", delete=False) as handle:
        path = Path(handle.name)
        handle.write(identity)
        handle.write("\n" if not identity.endswith("\n") else "")
    path.chmod(0o600)
    os.environ["JOSH_ROOM_IDENTITY"] = str(path)
    try:
        yield
    finally:
        os.environ.pop("JOSH_ROOM_IDENTITY", None)
        path.unlink(missing_ok=True)


def _doctor(instance: Path, backend_name: str, ide: str, dimension: str | None = None) -> dict:
    checks = []

    def record(name, ok, remediation, detail=None):
        item = {"name": name, "ok": bool(ok)}
        if detail:
            item["detail"] = detail
        if not ok:
            item["remediation"] = remediation
        checks.append(item)

    extension_mode = os.environ.get("JOSH_ROOM_EXTENSION_MODE") == "1"
    if extension_mode:
        managed_rcc = os.environ.get("JOSH_ROOM_RCC_EXE")
        rcc_ok = False
        if managed_rcc:
            try:
                rcc_path = Path(managed_rcc)
                rcc_ok = rcc_path.is_file() and not rcc_path.is_symlink() and os.access(rcc_path, os.X_OK)
            except OSError:
                rcc_ok = False
        record("rcc", rcc_ok, "Reload Josh Room so it can acquire its managed RCC runtime.")
        try:
            managed_age = _managed_executable("age")
        except CryptoError as error:
            record("age", False, "The managed Josh Room controller environment must provide age.", str(error))
        else:
            record("age", True, "The managed Josh Room controller environment must provide age.", str(managed_age))
        jat_ready = False
        jat_error = None
        jat_root = _jat_root()
        try:
            jat_result = run_doctor(jat_root)
            jat_ready = bool(jat_result.get("success"))
            jat_error = jat_result.get("diagnostics")
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            jat_error = str(error)
        record("hauler", jat_ready, "Josh Room could not verify Hauler inside the acquired JAT Holotree.", jat_error)
        record("tar", jat_ready, "Josh Room could not verify GNU tar inside the acquired JAT Holotree.", jat_error)
    else:
        for name in ("age", "hauler", "rcc"):
            record(name, shutil.which(name), f"Run the Josh Room container bootstrap to install {name}.")
        record("tar", _tar_capable(), "Run the Josh Room container bootstrap to install GNU tar with zstd support.")
    jat_root = _jat_root()
    contract = _jat_contract(jat_root)
    record("jat-robot", contract["robot"], "Run the Josh Room bootstrap to pull a JAT checkout containing robot.yaml Build/Restore/Serve tasks.")
    record("jat-python", contract["tasks"], "Run the Josh Room bootstrap to pull the JAT Python task surface (tasks.py).")
    record("jat-interactive", contract["interactive"], "Run the Josh Room bootstrap to pull the canonical JAT interactive task.")
    identity_path = os.environ.get("JOSH_ROOM_IDENTITY")
    identity_ok = False
    if identity_path:
        path = Path(identity_path)
        identity_ok = path.is_file() and path.stat().st_mode & 0o077 == 0
    record("identity", identity_ok, "Run josh-room setup to store Josh's daily age identity in the OS keyring.")

    catalog_ok = False
    selected_dimension = None
    selected_backend = backend_name
    if backend_name in {"r2", "minio"}:
        r2_ok = False
        try:
            selected = _effective_dimension(argparse.Namespace(backend=backend_name, dimension=dimension))
        except ValueError:
            selected = None
        selected_dimension = selected.dimension_id if selected else backend_name
        selected_backend = selected.provider if selected else backend_name
        try:
            if selected:
                backend = _backend(selected.provider, instance, selected_dimension)
            else:
                backend = _backend(backend_name, instance)
            encrypted, _etag = backend.read_catalog()
            r2_ok = True
            if encrypted is not None and identity_ok:
                catalog = load_catalog(instance, backend)
                catalog_ok = bool(catalog.body.get("projects"))
        except (OSError, RuntimeError, ValueError):
            r2_ok = False
        remediation = ("Run josh-room setup, unlock the host keyring, and verify the private R2 endpoint and bucket."
                       if selected_backend == "r2" else "Configure the private object-store endpoint, bucket, and OS keyring profile.")
        record(selected_backend, r2_ok, remediation, detail=f"private {selected_backend} reachable" if r2_ok else None)
    else:
        record("r2", True, "Select --backend local for offline use.", detail="local backend selected")
        if identity_ok:
            try:
                catalog_ok = bool(load_catalog(instance).body.get("projects"))
            except (OSError, RuntimeError, ValueError):
                catalog_ok = False
    record("catalog", catalog_ok, "Create the first snapshot or verify that the encrypted private R2 catalog is readable.")

    executable = None if ide == "terminal" else ("code-insiders" if ide == "vscode-insiders" else "code")
    record("ide", extension_mode or executable is None or shutil.which(executable), f"Install {executable} or use --ide terminal.")
    return {
        "ok": all(check["ok"] for check in checks),
        "product": "josh-room",
        "format_version": 1,
        "selected_backend": selected_backend,
        "selected_dimension": selected_dimension,
        "selected_ide": ide,
        "checks": checks,
        "r2_auth": auth_status(),
        "interactive_cloudflare_login": False,
    }


def _tar_capable() -> bool:
    candidates = [shutil.which("gtar"), shutil.which("tar")]
    for candidate in candidates:
        if not candidate:
            continue
        result = subprocess.run([candidate, "--help"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and "--zstd" in result.stdout:
            return True
    return False


def _room_identity(value: str) -> tuple[str, str]:
    display_name = " ".join(value.split())
    project_id = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    if not project_id:
        raise ValueError("room name must contain letters or numbers")
    if display_name == project_id:
        display_name = project_id.replace("-", " ").title()
    return project_id, display_name


def _launch_ide(executable: str, destination: Path) -> None:
    subprocess.Popen([executable, str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _identity() -> Path:
    value = os.environ.get("JOSH_ROOM_IDENTITY")
    if not value:
        raise ValueError("encrypted catalog requires JOSH_ROOM_IDENTITY")
    return Path(value)


def load_catalog(instance: Path, backend=None) -> Catalog:
    report_progress("catalog", "Reading encrypted Room catalog")
    dimension_id = getattr(getattr(backend, "config", None), "dimension_id", None) if backend else None
    encryption_domain_id = os.environ.get("JOSH_ROOM_SELECTED_DOMAIN")
    if backend is None:
        path = instance / "catalog.jroom.age"
        if not path.is_file():
            return Catalog.empty(dimension_id, encryption_domain_id)
        identity = os.environ.get("JOSH_ROOM_IDENTITY")
        if not identity:
            raise _encryption_authorization_required()
        return Catalog.from_body(json.loads(decrypt(path, [Path(identity)])), dimension_id, encryption_domain_id)
    encrypted, _etag = backend.read_catalog()
    if encrypted is None:
        return Catalog.empty(dimension_id, encryption_domain_id)
    identity = os.environ.get("JOSH_ROOM_IDENTITY")
    if not identity:
        raise _encryption_authorization_required()
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".catalog-read.", delete=False) as handle:
        path = Path(handle.name)
        handle.write(encrypted)
    try:
        catalog = Catalog.from_body(json.loads(decrypt(path, [Path(identity)])), dimension_id, encryption_domain_id)
        report_progress("catalog", "Room catalog is ready")
        return catalog
    finally:
        path.unlink(missing_ok=True)


def _encryption_authorization_required() -> RuntimeError:
    error = RuntimeError("encryption authorization required to read the Room catalog")
    error.result = {
        "error_code": "encryption-authorization-required",
        "authorization_required": True,
        "authorization_purpose": "encryption",
    }
    return error


def list_projects(instance: Path, backend=None) -> list[tuple[str, str]]:
    catalog = load_catalog(instance, backend)
    return [(project_id, project["display_name"]) for project_id, project in catalog.body["projects"].items()]


def choose_project(instance: Path, backend=None) -> str:
    if not sys.stdin.isatty():
        raise ValueError("enter requires a project in non-interactive mode")
    projects = list_projects(instance, backend)
    if not projects:
        raise ValueError("encrypted catalog contains no logical projects")
    print("What do you want to work on?")
    for index, (_, display_name) in enumerate(projects, 1):
        print(f"{index}) {display_name}")
    try:
        selected = int(input("Project: ")) - 1
        return projects[selected][0]
    except (ValueError, EOFError, IndexError) as error:
        raise ValueError("invalid project selection") from error


def _sanitize_human_diagnostic(value) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"(bearer\s+)[^\s,]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"((?:access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|password|oauth[-_ ]?code|authorization|stdin|argv|env)\s*[:=]\s*)[^\s,]+",
        r"\1[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"https?://[^\s]+", "[REDACTED URL]", text, flags=re.IGNORECASE)
    return text[:_HUMAN_DIAGNOSTIC_LIMIT]


def _collect_human_diagnostics(value, key: str = "", in_diagnostic: bool = False) -> list[str]:
    diagnostic_context = in_diagnostic or "diagnostic" in key.lower()
    if isinstance(value, str):
        return [_sanitize_human_diagnostic(value)] if diagnostic_context and value else []
    if isinstance(value, list):
        result = []
        for entry in value:
            result.extend(_collect_human_diagnostics(entry, key, diagnostic_context))
        return result
    if isinstance(value, dict):
        result = []
        for entry_key, entry in value.items():
            result.extend(_collect_human_diagnostics(entry, entry_key, diagnostic_context))
        return result
    return []


def _human_failure_message(result: dict) -> str:
    base = _sanitize_human_diagnostic(result.get("error"))
    diagnostics = []
    for diagnostic in _collect_human_diagnostics(result):
        if diagnostic and diagnostic not in diagnostics:
            diagnostics.append(diagnostic)
    diagnostic = _sanitize_human_diagnostic(" ".join(diagnostics))
    if diagnostic and diagnostic in base:
        parts = base.split(diagnostic)
        base = f"{parts[0]}{diagnostic}{''.join(parts[1:])}".strip()
    if diagnostic and diagnostic not in base:
        if len(diagnostic) + 2 >= _HUMAN_DIAGNOSTIC_LIMIT:
            return diagnostic
        available = _HUMAN_DIAGNOSTIC_LIMIT - len(diagnostic) - 2
        return f"{base[:max(0, available)]}: {diagnostic}" if base else diagnostic
    return base[:_HUMAN_DIAGNOSTIC_LIMIT]


def emit(result: dict, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(result, sort_keys=True))
    elif result["ok"] and {"project_id", "snapshot_id", "ciphertext_size"} <= result.keys():
        size_mib = result["ciphertext_size"] / (1024 * 1024)
        print(f'Saved "{result["project_id"]}".')
        print(f"Encrypted snapshot: {size_mib:.1f} MiB")
        print("Restore it with Josh: Enter Room.")
    elif result["ok"]:
        print("ok: " + json.dumps(result, sort_keys=True))
    else:
        message = _human_failure_message(result)
        if not message:
            failed = [check for check in result.get("checks", []) if not check["ok"]]
            message = "; ".join(f"{check['name']}: {check['remediation']}" for check in failed)
        print("error: " + message)
