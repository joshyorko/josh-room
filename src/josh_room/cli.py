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
    ensure_runtime_session,
    poll_oauth_session,
    runtime_session_state,
    start_oauth_session,
)
from .catalog import Catalog
from .config import DimensionRegistry, auth_status, private_config, save_private_config
from .crypto import decrypt
from .jat import _jat_contract, run_build, run_restore, run_serve
from .keyring import lookup_value as lookup_keyring_value
from .keyring import store as store_keyring
from .keyring import store_value as store_keyring_value
from .local_store import ImmutableLocalStore
from .minio import MinioBackend, MinioConfig
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
    dimensions = commands.add_parser("dimensions")
    dimension_commands = dimensions.add_subparsers(dest="dimension_command", required=True)
    dimension_list = dimension_commands.add_parser("list")
    _json_option(dimension_list)
    for action in ("add", "update"):
        dimension_edit = dimension_commands.add_parser(action)
        dimension_edit.add_argument("dimension")
        dimension_edit.add_argument("--display-name", required=action == "add")
        dimension_edit.add_argument("--provider", choices=("r2", "minio"), required=action == "add")
        dimension_edit.add_argument("--endpoint", required=action == "add")
        dimension_edit.add_argument("--bucket", required=action == "add")
        dimension_edit.add_argument("--credential-profile", required=action == "add")
        dimension_edit.add_argument("--region")
        dimension_edit.add_argument("--catalog-key")
        _json_option(dimension_edit)
    auth = commands.add_parser("auth")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_start = auth_commands.add_parser("start")
    auth_start.add_argument("--dimension")
    _json_option(auth_start)
    auth_poll = auth_commands.add_parser("poll")
    auth_poll.add_argument("session_id")
    auth_poll.add_argument("--dimension")
    _json_option(auth_poll)
    auth_status = auth_commands.add_parser("status")
    auth_status.add_argument("--dimension")
    _json_option(auth_status)
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    _json_option(status)
    for action in ("link", "repair"):
        state_command = commands.add_parser(action)
        state_command.add_argument("--workspace", type=Path, default=Path.cwd())
        state_command.add_argument("--dimension")
        state_command.add_argument("--project")
        state_command.add_argument("--snapshot")
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
    _json_option(jat_build)
    jat_restore = jat_commands.add_parser("restore")
    jat_restore.add_argument("--haul", type=Path, required=True)
    jat_restore.add_argument("--destination", type=Path, required=True)
    _json_option(jat_restore)
    jat_serve = jat_commands.add_parser("serve")
    jat_serve.add_argument("--haul", type=Path, required=True)
    _json_option(jat_serve)
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
        if _requires_oauth(args):
            selected = None if getattr(args, "snapshot_command", None) == "copy" else _effective_dimension(args)
            ensure_runtime_session(dimension_id=selected.dimension_id if selected else None)
        identity_context = nullcontext() if args.command in {"auth", "setup", "status"} else _identity_environment()
        with identity_context:
            result = dispatch(args, instance)
    except (OSError, RuntimeError, ValueError) as error:
        result = {"ok": False, "error": str(error)}
        if isinstance(getattr(error, "result", None), dict):
            result.update(error.result)
    emit(result, getattr(args, "json", False))
    return 0 if result["ok"] else 2


def _requires_oauth(args) -> bool:
    if args.command not in {"projects", "rooms", "snapshots", "snapshot", "hydrate", "enter", "serve", "link", "repair"}:
        return False
    if getattr(args, "snapshot_command", None) == "copy":
        registry = DimensionRegistry(private_config() or {})
        try:
            source_dimension = _copy_source_dimension(args)
            dimensions = [dimension for dimension in (source_dimension, args.destination_dimension) if dimension]
            return any(registry.select(dimension).provider == "r2" for dimension in dimensions)
        except ValueError:
            return False
    dimension = getattr(args, "dimension", None)
    if dimension:
        try:
            return DimensionRegistry(private_config() or {}).select(dimension).provider == "r2"
        except ValueError:
            return False
    try:
        selected = _effective_dimension(args)
    except ValueError:
        selected = None
    if selected is not None:
        return selected.provider == "r2"
    return getattr(args, "backend", "r2") == "r2"


def _copy_source_dimension(args) -> str | None:
    source_folder = getattr(args, "source_folder", None)
    if not source_folder:
        return getattr(args, "source_dimension", None)
    status = local_status(source_folder)
    if not status.get("ok") or status.get("state") != "clean" or not status.get("dimension_id"):
        return None
    return status["dimension_id"]


def dispatch(args, instance: Path) -> dict:
    if args.command == "auth":
        if args.auth_command == "start":
            return {"ok": True, **start_oauth_session()}
        if args.auth_command == "poll":
            return {"ok": True, **poll_oauth_session(args.session_id, dimension_id=args.dimension)}
        return {"ok": True, "state": runtime_session_state(), "dimension_id": args.dimension}
    if args.command == "dimensions":
        config = private_config() or {}
        if args.dimension_command == "list":
            return {"ok": True, "dimensions": [{"id": key, "display_name": value.display_name, "provider": value.provider, "bucket": value.bucket, "endpoint": value.endpoint} for key, value in DimensionRegistry(config)]}
        records = dict(config.get("dimensions", {}))
        if args.dimension_command == "add" and args.dimension in records:
            raise ValueError(f"Dimension {args.dimension} already exists")
        current = records.get(args.dimension, {})
        values = {**current}
        for field, key in (("display_name", "display_name"), ("provider", "provider"), ("endpoint", "endpoint"), ("bucket", "bucket"), ("credential_profile", "credential_profile"), ("region", "region"), ("catalog_key", "catalog_key")):
            value = getattr(args, field.replace("-", "_"), None)
            if value is not None:
                values[key] = value
        records[args.dimension] = values
        DimensionRegistry({"dimensions": records}).get(args.dimension)
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
        operation = link_workspace if args.command == "link" else repair_workspace
        return operation(args.workspace, catalog, object_evidence, project_id=project_id, snapshot_id=snapshot_id, dimension_id=dimension_id)
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
        if len(recipients) < 2:
            raise ValueError("snapshot create requires JOSH_ROOM_JAT_ROOT and two age recipients")
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
                ),
            }
        if args.jat_command == "restore":
            return {"ok": True, **run_restore(jat_root, args.haul, args.destination)}
        if args.jat_command == "serve":
            return {"ok": True, **run_serve(jat_root, args.haul)}
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
        store_keyring(args.profile, credentials)
        store_keyring_value(args.age_profile, "age-identity", credentials["age-identity"], label="Josh Room age identity")
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
    record("ide", executable is None or shutil.which(executable), f"Install {executable} or use --ide terminal.")
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
    brew = shutil.which("brew")
    if brew:
        prefix = subprocess.run([brew, "--prefix", "gnu-tar"], capture_output=True, text=True, check=False)
        if prefix.returncode == 0:
            candidates.append(str(Path(prefix.stdout.strip()) / "bin/tar"))
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
    if backend is None:
        path = instance / "catalog.jroom.age"
        if not path.is_file():
            raise ValueError("encrypted catalog is unavailable")
        return Catalog(json.loads(decrypt(path, [_identity()])))
    encrypted, _etag = backend.read_catalog()
    if encrypted is None:
        raise ValueError("encrypted catalog is unavailable")
    instance.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".catalog-read.", delete=False) as handle:
        path = Path(handle.name)
        handle.write(encrypted)
    try:
        catalog = Catalog.from_body(json.loads(decrypt(path, [_identity()])), getattr(getattr(backend, "config", None), "dimension_id", None))
        report_progress("catalog", "Room catalog is ready")
        return catalog
    finally:
        path.unlink(missing_ok=True)


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
        message = result.get("error")
        if not message:
            failed = [check for check in result.get("checks", []) if not check["ok"]]
            message = "; ".join(f"{check['name']}: {check['remediation']}" for check in failed)
        print("error: " + message)
