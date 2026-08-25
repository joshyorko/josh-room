import argparse
import json
import subprocess
import sys

from josh_room.cli import (
    _requires_oauth,
    _tar_capable,
    _workspace_root,
    build_parser,
    emit,
    main,
)


def test_clean_cli_import_injects_truststore_before_botocore():
    script = """
import builtins
events = []
real_import = builtins.__import__

def tracking_import(name, *args, **kwargs):
    if name == 'truststore' or name == 'botocore.config':
        events.append(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = tracking_import
import josh_room.cli
assert events.index('truststore') < events.index('botocore.config'), events
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_human_snapshot_receipt_is_concise_while_json_stays_complete(capsys):
    result = {
        "ok": True,
        "project_id": "heather-mk1-room",
        "snapshot_id": "snapshot-one",
        "ciphertext_size": 2_151_210,
        "producer": {"argv": ["rcc", "run"]},
    }

    emit(result, False)
    human = capsys.readouterr().out
    assert human == (
        'Saved "heather-mk1-room".\n'
        "Encrypted snapshot: 2.1 MiB\n"
        "Restore it with Josh: Enter Room.\n"
    )
    emit(result, True)
    assert json.loads(capsys.readouterr().out) == result


def test_doctor_json_is_stable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("JOSH_ROOM_JAT_ROOT", str(tmp_path / "jat-isolated"))
    monkeypatch.delenv("JOSH_ROOM_IDENTITY", raising=False)
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda _name: None)
    assert main(["doctor", "--backend", "r2", "--ide", "vscode-insiders", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert report["interactive_cloudflare_login"] is False
    missing = {check["name"] for check in report["checks"] if not check["ok"]}
    assert {"age", "hauler", "tar", "rcc", "jat-robot", "jat-python", "jat-interactive", "identity", "r2", "catalog", "ide"} <= missing
    assert all(check.get("remediation") for check in report["checks"] if not check["ok"])


def test_enter_requires_a_project_or_lists_projects(capsys):
    assert main(["enter", "--backend", "local", "--json"]) == 2
    assert "project" in json.loads(capsys.readouterr().out)["error"]


def test_documented_subcommands_and_options_parse_as_typed_arguments(tmp_path):
    parser = build_parser()
    args = parser.parse_args(["snapshot", "create", "demo", "--source", str(tmp_path), "--backend", "local", "--json"])
    assert args.command == "snapshot"
    assert args.snapshot_command == "create"
    assert args.project == "demo"
    assert args.source == tmp_path
    assert args.backend == "local"
    args = parser.parse_args(["hydrate", "demo", "--snapshot", "latest", "--destination", str(tmp_path), "--ide", "terminal", "--json"])
    assert args.command == "hydrate"
    assert args.snapshot == "latest"
    assert args.ide == "terminal"
    args = parser.parse_args(["projects", "list", "--backend", "r2", "--json"])
    assert args.backend == "r2"
    args = parser.parse_args(["rooms", "remove", "demo", "--backend", "r2", "--json"])
    assert args.command == "rooms" and args.room_command == "remove" and args.project == "demo"
    args = parser.parse_args(["snapshots", "remove", "demo", "snapshot-one", "--backend", "r2", "--json"])
    assert args.snapshots_command == "remove" and args.project == "demo" and args.snapshot == "snapshot-one"
    args = parser.parse_args(["serve", "demo", "--snapshot", "latest", "--backend", "r2"])
    assert args.command == "serve" and args.project == "demo" and args.snapshot == "latest"
    args = parser.parse_args(["jat", "build", "--source", str(tmp_path), "--output", str(tmp_path / "haul.tar.zst"), "--all-images"])
    assert args.command == "jat" and args.jat_command == "build" and args.all_images is True
    assert _requires_oauth(args) is False
    args = parser.parse_args(["jat", "restore", "--haul", str(tmp_path / "haul.tar.zst"), "--destination", str(tmp_path / "restored")])
    assert args.jat_command == "restore"
    args = parser.parse_args(["jat", "serve", "--haul", str(tmp_path / "haul.tar.zst")])
    assert args.jat_command == "serve"
    assert parser.parse_args(["enter", "hive"]).backend == "r2"
    assert parser.parse_args(["snapshot", "create", "demo"]).source is None
    image_args = parser.parse_args(["snapshot", "create", "demo", "--image", "example/image:tag"])
    assert image_args.images == ["example/image:tag"] and image_args.all_images is False
    assert parser.parse_args(["snapshot", "create", "demo", "--all-images"]).all_images is True


def test_one_off_jat_commands_use_typed_service_without_room_backend(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("josh_room.cli._jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr(
        "josh_room.cli.run_build",
        lambda jat, source, output, **options: calls.append(("build", jat, source, output, options)) or {"operation": "build"},
    )
    monkeypatch.setattr(
        "josh_room.cli.run_restore",
        lambda jat, haul, destination: calls.append(("restore", jat, haul, destination)) or {"operation": "restore"},
    )
    monkeypatch.setattr(
        "josh_room.cli.run_serve",
        lambda jat, haul: calls.append(("serve", jat, haul)) or {"operation": "serve"},
    )
    module = __import__("josh_room.cli", fromlist=["dispatch"])

    build = build_parser().parse_args(["jat", "build", "--source", str(tmp_path), "--output", str(tmp_path / "haul"), "--all-images"])
    restore = build_parser().parse_args(["jat", "restore", "--haul", str(tmp_path / "haul"), "--destination", str(tmp_path / "restored")])
    serve = build_parser().parse_args(["jat", "serve", "--haul", str(tmp_path / "haul")])

    assert module.dispatch(build, tmp_path / "instance")["operation"] == "build"
    assert module.dispatch(restore, tmp_path / "instance")["operation"] == "restore"
    assert module.dispatch(serve, tmp_path / "instance")["operation"] == "serve"
    assert [call[0] for call in calls] == ["build", "restore", "serve"]


def test_hydrate_passes_explicit_snapshot_to_operations(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(tmp_path / "identity"))
    monkeypatch.setattr("josh_room.cli._jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr("josh_room.cli._backend", lambda *_args: object())
    monkeypatch.setattr(
        "josh_room.cli.hydrate",
        lambda *_args, **kwargs: captured.update(kwargs) or {"snapshot_id": kwargs["snapshot_id"]},
    )
    args = build_parser().parse_args([
        "hydrate", "demo", "--snapshot", "older", "--destination", str(tmp_path / "dest"), "--backend", "r2",
    ])

    result = __import__("josh_room.cli", fromlist=["hydrate_command"]).hydrate_command(args, tmp_path / "instance")

    assert result["snapshot_id"] == "older"
    assert captured["snapshot_id"] == "older"


def test_human_enter_uses_terminal_picker(monkeypatch, capsys):
    monkeypatch.setattr("josh_room.cli.list_projects", lambda _instance, _backend=None: [("demo", "Demo Project")])
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    result = main(["enter", "--backend", "local", "--ide", "terminal"])
    assert result == 2
    assert "hydrate" in capsys.readouterr().out


def test_documented_argv_forms_have_stable_json_exit_contract(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_INSTANCE", str(tmp_path / "instance"))
    monkeypatch.setattr("josh_room.cli.ensure_runtime_session", lambda: None)
    cases = [
        (["doctor", "--json"], 2),
        (["projects", "list", "--json"], 2),
        (["rooms", "remove", "demo", "--json"], 2),
        (["snapshots", "list", "demo", "--json"], 2),
        (["snapshot", "create", "demo", "--source", str(tmp_path), "--backend", "r2", "--json"], 2),
        (["hydrate", "demo", "--destination", str(tmp_path / "dest"), "--ide", "terminal", "--json"], 2),
        (["enter", "demo", "--ide", "terminal", "--json"], 2),
    ]
    for argv, expected in cases:
        assert main(argv) == expected
        assert isinstance(json.loads(capsys.readouterr().out), dict)
    assert main(["doctor"]) == 2
    assert capsys.readouterr().out.startswith("error: ")


def test_enter_launches_selected_ide_after_hydration(tmp_path, monkeypatch):
    launched = []
    monkeypatch.setenv("JOSH_ROOM_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("josh_room.cli.hydrate_command", lambda _args, _instance, _backend=None: {"ok": True, "destination": str(tmp_path / "demo")})
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("josh_room.cli._launch_ide", lambda executable, destination: launched.append((executable, destination)))
    args = argparse.Namespace(command="enter", project="demo", snapshot="latest", ide="vscode-insiders", backend="local", json=False)
    result = __import__("josh_room.cli", fromlist=["dispatch"]).dispatch(args, tmp_path / "instance")
    assert result["launch"] == "code-insiders"
    assert launched == [("/usr/bin/code-insiders", tmp_path / "demo")]


def test_projects_list_passes_selected_backend_to_catalog(tmp_path, monkeypatch):
    selected = object()
    monkeypatch.setattr("josh_room.cli._backend", lambda name, _instance: selected if name == "r2" else None)
    monkeypatch.setattr("josh_room.cli.list_projects", lambda _instance, backend=None: [("demo", "Demo")] if backend is selected else [])
    args = argparse.Namespace(command="projects", project_command="list", backend="r2", json=True)
    result = __import__("josh_room.cli", fromlist=["dispatch"]).dispatch(args, tmp_path)
    assert result["projects"] == [{"id": "demo", "display_name": "Demo"}]


def test_snapshot_create_preserves_human_room_name(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("josh_room.cli._recipients", lambda: ["age1daily", "age1recovery"])
    monkeypatch.setattr("josh_room.cli._jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr("josh_room.cli._backend", lambda _name, _instance: object())

    def create(_instance, project_id, _source, _jat, _recipients, _backend, display_name=None, **_kwargs):
        captured.update(project_id=project_id, display_name=display_name)
        return {"project_id": project_id, "snapshot_id": "synthetic"}

    monkeypatch.setattr("josh_room.cli.create_snapshot", create)
    args = build_parser().parse_args(
        ["snapshot", "create", "RCC Action Server Quattro 4-2", "--source", str(tmp_path), "--backend", "r2"]
    )
    result = __import__("josh_room.cli", fromlist=["dispatch"]).dispatch(args, tmp_path / "instance")
    assert result["ok"] is True
    assert captured == {
        "project_id": "rcc-action-server-quattro-4-2",
        "display_name": "RCC Action Server Quattro 4-2",
    }


def test_snapshot_create_defaults_source_to_current_workspace(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("josh_room.cli._recipients", lambda: ["age1daily", "age1recovery"])
    monkeypatch.setattr("josh_room.cli._jat_root", lambda: tmp_path / "jat")
    monkeypatch.setattr("josh_room.cli._backend", lambda _name, _instance: object())
    monkeypatch.setattr(
        "josh_room.cli.create_snapshot",
        lambda _instance, _project, source, *_args, **_kwargs: captured.update(source=source) or {"snapshot_id": "one"},
    )

    __import__("josh_room.cli", fromlist=["dispatch"]).dispatch(
        build_parser().parse_args(["snapshot", "create", "Demo", "--backend", "r2"]),
        tmp_path / "instance",
    )

    assert captured["source"] == tmp_path


def test_workspace_root_detects_clean_room_parent(tmp_path, monkeypatch):
    room = tmp_path / "room"
    room.mkdir()
    monkeypatch.chdir(room)
    monkeypatch.delenv("JOSH_ROOM_WORKSPACE_ROOT", raising=False)
    monkeypatch.setattr("josh_room.cli._configured", dict)

    assert _workspace_root() == tmp_path


def test_tar_capability_finds_linuxbrew_keg_tar(monkeypatch):
    paths = {"gtar": None, "tar": "/usr/bin/tar", "brew": "/brew/bin/brew"}
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda name: paths.get(name))

    def run(argv, **_kwargs):
        if argv == ["/brew/bin/brew", "--prefix", "gnu-tar"]:
            return __import__("subprocess").CompletedProcess(argv, 0, "/brew/Cellar/gnu-tar/1.35\n", "")
        if argv[0] == "/brew/Cellar/gnu-tar/1.35/bin/tar":
            return __import__("subprocess").CompletedProcess(argv, 0, "--zstd", "")
        return __import__("subprocess").CompletedProcess(argv, 0, "BusyBox", "")

    monkeypatch.setattr("josh_room.cli.subprocess.run", run)
    assert _tar_capable() is True
