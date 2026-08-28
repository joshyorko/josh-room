import argparse
import json
import stat
import subprocess
import sys

import pytest

from josh_room.cli import (
    _effective_dimension,
    _requires_oauth,
    _tar_capable,
    _workspace_root,
    build_parser,
    emit,
    main,
)


def test_clean_cli_import_does_not_mutate_global_ssl_context():
    script = """
import ssl
original = ssl.SSLContext
import josh_room.cli
assert ssl.SSLContext is original
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_extension_controller_writes_a_private_result_receipt(tmp_path, monkeypatch, capsys):
    receipt = tmp_path / "controller-result.json"
    monkeypatch.setenv("JOSH_ROOM_RESULT_FILE", str(receipt))
    monkeypatch.setattr("josh_room.cli.initialize_system_trust", lambda: None)

    assert main(["auth", "status", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert json.loads(receipt.read_text())["ok"] is True
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_r2_command_initializes_system_trust_before_auth_and_dispatch(monkeypatch, capsys):
    events = []
    monkeypatch.setattr("josh_room.cli.initialize_system_trust", lambda: events.append("tls"))
    monkeypatch.setattr("josh_room.cli.ensure_runtime_session", lambda **_kwargs: events.append("auth"))
    monkeypatch.setattr(
        "josh_room.cli.dispatch",
        lambda *_args: events.append("dispatch") or {"ok": True, "projects": []},
    )

    assert main(["projects", "list", "--backend", "r2", "--json"]) == 0
    assert events == ["tls", "auth", "dispatch"]
    assert json.loads(capsys.readouterr().out)["projects"] == []


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


def test_extension_doctor_uses_managed_rcc_and_jat_doctor_not_host_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", str(tmp_path / "runtime" / "rcc"))
    monkeypatch.setenv("JOSH_ROOM_JAT_ROOT", str(tmp_path / "jat"))
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda name: "/managed/age" if name == "age" else None)
    monkeypatch.setattr("josh_room.cli._jat_contract", lambda _root: {"robot": True, "tasks": True, "interactive": True})
    calls = []
    monkeypatch.setattr("josh_room.cli.run_doctor", lambda root: calls.append(root) or {"success": True})

    report = __import__("josh_room.cli", fromlist=["_doctor"])._doctor(tmp_path, "local", "vscode-insiders")

    assert calls == [tmp_path / "jat"]
    checks = {check["name"]: check["ok"] for check in report["checks"]}
    assert checks["rcc"] is False
    assert checks["age"] is True
    assert checks["hauler"] is True
    assert checks["tar"] is True
    assert checks["ide"] is True


def test_cli_tar_capability_does_not_probe_homebrew(monkeypatch):
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda name: "/tools/brew" if name == "brew" else None)
    monkeypatch.setattr("josh_room.cli.subprocess.run", lambda *_args, **_kwargs: pytest.fail("must not invoke brew"))

    assert __import__("josh_room.cli", fromlist=["_tar_capable"])._tar_capable() is False


def test_status_does_not_load_identity_or_keyring(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.json").write_text(
        '{"age_identity_profile": "synthetic-age-profile"}'
    )
    monkeypatch.setattr(
        "josh_room.cli.lookup_keyring_value",
        lambda *_args: pytest.fail("status must not access the keyring"),
    )
    assert main(["status", "--workspace", str(tmp_path / "workspace"), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "unlinked"


def test_dimension_add_rejects_duplicate_named_dimension(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path))
    config = {
        "dimensions": {
            "archive": {
                "display_name": "Archive",
                "provider": "r2",
                "endpoint": "https://archive.example.invalid",
                "bucket": "archive",
                "credential_profile": "archive-profile",
            }
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    assert main([
        "dimensions", "add", "archive", "--display-name", "Other",
        "--provider", "r2", "--endpoint", "https://other.example.invalid",
        "--bucket", "other", "--credential-profile", "other-profile", "--json",
    ]) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]
    assert json.loads((tmp_path / "config.json").read_text()) == config


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
    args = parser.parse_args(["auth", "start", "--dimension", "archive", "--json"])
    assert args.command == "auth" and args.auth_command == "start" and args.dimension == "archive"
    args = parser.parse_args(["auth", "poll", "session-one", "--dimension", "archive", "--json"])
    assert args.command == "auth" and args.auth_command == "poll" and args.session_id == "session-one"
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


def test_native_auth_commands_delegate_to_existing_worker_session_helpers(tmp_path, monkeypatch):
    module = __import__("josh_room.cli", fromlist=["dispatch"])
    monkeypatch.setattr(module, "start_oauth_session", lambda: {
        "session_id": "session-one",
        "authorization_url": "https://example.invalid/auth",
        "expires_in": 600,
    })
    monkeypatch.setattr(module, "poll_oauth_session", lambda session_id, dimension_id=None: {
        "status": "authorized", "session_id": session_id, "dimension_id": dimension_id,
    })

    start = module.dispatch(build_parser().parse_args(["auth", "start", "--dimension", "archive"]), tmp_path)
    poll = module.dispatch(build_parser().parse_args(["auth", "poll", "session-one", "--dimension", "archive"]), tmp_path)

    assert start["authorization_url"] == "https://example.invalid/auth"
    assert poll == {
        "ok": True,
        "status": "authorized",
        "session_id": "session-one",
        "dimension_id": "archive",
    }


def test_runtime_default_r2_is_oauth_routed_before_dimension_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(tmp_path / "config"))
    args = build_parser().parse_args(["snapshot", "create", "demo", "--dimension", "r2"])
    assert _requires_oauth(args) is True
    copy_args = build_parser().parse_args([
        "snapshot", "copy", "source", "--source-dimension", "r2",
        "--destination-dimension", "r2", "--destination-room", "destination",
    ])
    assert _requires_oauth(copy_args) is True

    events = []
    runtime_config = tmp_path / "runtime-config.json"
    runtime_config.write_text(json.dumps({
        "default_backend": "r2",
        "dimensions": {
            "r2": {
                "display_name": "Default",
                "provider": "r2",
                "endpoint": "https://r2.example.invalid",
                "bucket": "room",
                "credential_profile": "oauth-runtime",
            },
        },
        "r2": {
            "endpoint": "https://r2.example.invalid",
            "bucket": "room",
            "credential_profile": "oauth-runtime",
        },
    }))

    def restore_runtime(**kwargs):
        events.append(kwargs)
        monkeypatch.setenv("JOSH_ROOM_RUNTIME_CONFIG", str(runtime_config))

    monkeypatch.setattr("josh_room.cli.initialize_system_trust", lambda: None)
    monkeypatch.setattr("josh_room.cli.ensure_runtime_session", restore_runtime)
    monkeypatch.setattr(
        "josh_room.cli.dispatch",
        lambda parsed, _instance: {"ok": True, "dimension_id": _effective_dimension(parsed).dimension_id},
    )

    assert main(["snapshot", "create", "demo", "--dimension", "r2", "--json"]) == 0
    assert events == [{"dimension_id": "r2"}]


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
    monkeypatch.setattr("josh_room.cli.ensure_runtime_session", lambda **_kwargs: None)
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


def test_tar_capability_finds_capable_host_tar_without_homebrew(monkeypatch):
    paths = {"gtar": "/tools/gtar", "tar": None, "brew": "/brew/bin/brew"}
    monkeypatch.setattr("josh_room.cli.shutil.which", lambda name: paths.get(name))

    def run(argv, **_kwargs):
        if argv[0] == "/tools/gtar":
            return __import__("subprocess").CompletedProcess(argv, 0, "--zstd", "")
        return __import__("subprocess").CompletedProcess(argv, 0, "BusyBox", "")

    monkeypatch.setattr("josh_room.cli.subprocess.run", run)
    assert _tar_capable() is True
