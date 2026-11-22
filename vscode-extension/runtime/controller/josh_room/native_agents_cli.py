"""Offline CLI boundary for explicitly authorized native agent profiles."""

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="josh-room agents")
    commands = parser.add_subparsers(dest="action", required=True)
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("--json", action="store_true")
    for action in ("plan", "status", "configure", "launch", "capture", "verify", "restore", "materialize-session"):
        command = commands.add_parser(action)
        command.add_argument("--profile-root", type=Path, required=True)
        command.add_argument("--dimension", required=True)
        command.add_argument("--room", required=True)
        command.add_argument("--profile", required=True)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--json", action="store_true")
        if action in {"configure", "launch", "capture", "verify", "materialize-session"}:
            command.add_argument("--tool", choices=("codex", "claude", "t3"), default="codex")
        if action == "launch":
            command.add_argument("--port", type=int, default=3773)
        if action in {"configure", "capture"}:
            command.add_argument("--recipient", action="append", required=True)
        if action == "capture":
            command.add_argument("--output", type=Path, required=True)
        if action in {"verify", "restore", "materialize-session"}:
            command.add_argument("--source", type=Path, required=True)
            command.add_argument("--identity", type=Path, action="append", required=True)
        if action == "materialize-session":
            command.add_argument("--session-id", required=True)
    return parser


def dispatch(args):
    from . import native_agents

    if args.action == "capabilities":
        return {"ok": True, **native_agents.capabilities()}
    binding = {"dimension": args.dimension, "room": args.room, "profile": args.profile, "workspace": args.workspace}
    action = "status" if args.action == "plan" else args.action
    options = {}
    if hasattr(args, "tool"):
        options["tool"] = args.tool
    if action == "launch":
        options["port"] = args.port
    elif action == "configure":
        options["recipients"] = args.recipient
    elif action == "capture":
        options.update(output=args.output, recipients=args.recipient)
    elif action in {"verify", "restore", "materialize-session"}:
        options.update(source=args.source, identity_paths=args.identity)
    if action == "materialize-session":
        options["session_id"] = args.session_id
        action = "materialize_session"
    return getattr(native_agents, action)(args.profile_root, **binding, **options)


def main(argv=None) -> int:
    from .cancellation import CLICancelled
    from .native_agents import NativeAgentError

    args = build_parser().parse_args(argv)
    try:
        result = dispatch(args)
    except NativeAgentError as error:
        result = {"ok": False, "state": "commit-uncertain" if error.committed else "blocked", "error": str(error)}
        if error.committed:
            result.update(committed=True, native_resume_verified=False)
    except (OSError, RuntimeError, ValueError, TypeError):
        result = {"ok": False, "state": "blocked", "error": "Native agent operation refused unsafe or unavailable input"}
    except (KeyboardInterrupt, CLICancelled):
        result = {"ok": False, "state": "cancelled"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") else 1
