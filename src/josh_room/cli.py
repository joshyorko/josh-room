import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .catalog import Catalog
from .config import auth_status, private_config, save_private_config
from .crypto import decrypt
from .jat import _jat_contract
from .keyring import lookup_value as lookup_keyring_value
from .keyring import store as store_keyring
from .keyring import store_value as store_keyring_value
from .operations import create_snapshot, hydrate
from .r2 import R2Backend, R2Config


def _json_option(parser):
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josh-room")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--backend", choices=("local", "r2"), default="r2")
    doctor.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    _json_option(doctor)
    projects = commands.add_parser("projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--backend", choices=("local", "r2"), default="r2")
    _json_option(project_list)
    snapshots = commands.add_parser("snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshots_command", required=True)
    snapshot_list = snapshot_commands.add_parser("list")
    snapshot_list.add_argument("project")
    snapshot_list.add_argument("--backend", choices=("local", "r2"), default="r2")
    _json_option(snapshot_list)
    snapshot = commands.add_parser("snapshot")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_commands.add_parser("create")
    snapshot_create.add_argument("project")
    snapshot_create.add_argument("--source", type=Path, required=True)
    snapshot_create.add_argument("--backend", choices=("local", "r2"), default="r2")
    _json_option(snapshot_create)
    hydration = commands.add_parser("hydrate")
    hydration.add_argument("project")
    hydration.add_argument("--snapshot", default="latest")
    hydration.add_argument("--destination", type=Path, required=True)
    hydration.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="terminal")
    hydration.add_argument("--backend", choices=("local", "r2"), default="r2")
    _json_option(hydration)
    enter = commands.add_parser("enter")
    enter.add_argument("project", nargs="?")
    enter.add_argument("--snapshot", default="latest")
    enter.add_argument("--ide", choices=("vscode-insiders", "vscode", "terminal"), default="vscode-insiders")
    enter.add_argument("--backend", choices=("local", "r2"), default="r2")
    _json_option(enter)
    setup = commands.add_parser("setup")
    setup.add_argument("--profile", required=True)
    setup.add_argument("--age-profile", required=True)
    _json_option(setup)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    instance = _instance_root()
    try:
        identity_context = nullcontext() if args.command == "setup" else _identity_environment()
        with identity_context:
            result = dispatch(args, instance)
    except (OSError, RuntimeError, ValueError) as error:
        result = {"ok": False, "error": str(error)}
    emit(result, getattr(args, "json", False))
    return 0 if result["ok"] else 2


def dispatch(args, instance: Path) -> dict:
    if args.command == "doctor":
        return _doctor(instance, args.backend, args.ide)
    if args.command == "projects":
        projects = list_projects(instance, _backend(args.backend, instance))
        return {"ok": True, "projects": [{"id": project_id, "display_name": name} for project_id, name in projects]}
    if args.command == "snapshots":
        catalog = load_catalog(instance, _backend(args.backend, instance))
        project = catalog.body["projects"].get(args.project)
        if not project:
            raise ValueError("project is not present in the encrypted catalog")
        return {"ok": True, "project": args.project, "latest": project["latest"], "snapshots": list(project["snapshots"].values())}
    if args.command == "snapshot":
        recipients = _recipients()
        jat_root = _jat_root()
        if len(recipients) < 2:
            raise ValueError("snapshot create requires JOSH_ROOM_JAT_ROOT and two age recipients")
        return {"ok": True, **create_snapshot(instance, args.project, args.source, jat_root, recipients, _backend(args.backend, instance))}
    if args.command == "hydrate":
        return hydrate_command(args, instance)
    if args.command == "enter":
        backend = _backend(args.backend, instance)
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
    if args.command == "setup":
        credentials = json.load(sys.stdin)
        required = {
            "access-key-id",
            "secret-access-key",
            "endpoint",
            "bucket",
            "age-identity",
            "age-recipients",
            "cloudflare-api-token",
            "cloudflare-account-id",
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
    if args.snapshot != "latest":
        raise ValueError("only --snapshot latest is supported by the local MVP")
    identity = os.environ.get("JOSH_ROOM_IDENTITY")
    jat_root = _jat_root()
    if not identity:
        raise ValueError("hydrate requires JOSH_ROOM_IDENTITY and JOSH_ROOM_JAT_ROOT")
    if backend is None:
        backend = _backend(args.backend, instance)
    return {"ok": True, **hydrate(instance, args.project, args.destination, Path(identity), jat_root, backend)}


def _backend(name: str, instance: Path):
    if name == "local":
        return None
    return R2Backend(R2Config.from_private(private_config()), receipt_dir=instance / "receipts")


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
    return Path(value) if value else Path.cwd()


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


def _doctor(instance: Path, backend_name: str, ide: str) -> dict:
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
    if backend_name == "r2":
        r2_ok = False
        try:
            backend = _backend("r2", instance)
            encrypted, _etag = backend.read_catalog()
            r2_ok = True
            if encrypted is not None and identity_ok:
                catalog = load_catalog(instance, backend)
                catalog_ok = bool(catalog.body.get("projects"))
        except (OSError, RuntimeError, ValueError):
            r2_ok = False
        record("r2", r2_ok, "Run josh-room setup, unlock the host keyring, and verify the private R2 endpoint and bucket.", detail="private R2 reachable" if r2_ok else None)
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
        "selected_backend": backend_name,
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


def _launch_ide(executable: str, destination: Path) -> None:
    subprocess.Popen([executable, str(destination)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _identity() -> Path:
    value = os.environ.get("JOSH_ROOM_IDENTITY")
    if not value:
        raise ValueError("encrypted catalog requires JOSH_ROOM_IDENTITY")
    return Path(value)


def load_catalog(instance: Path, backend=None) -> Catalog:
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
        return Catalog(json.loads(decrypt(path, [_identity()])))
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
    elif result["ok"]:
        print("ok: " + json.dumps(result, sort_keys=True))
    else:
        message = result.get("error")
        if not message:
            failed = [check for check in result.get("checks", []) if not check["ok"]]
            message = "; ".join(f"{check['name']}: {check['remediation']}" for check in failed)
        print("error: " + message)
