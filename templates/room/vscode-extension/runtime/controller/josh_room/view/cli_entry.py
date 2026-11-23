"""CLI command builders and dispatchers for `josh-room view`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .contract import load_provider
from .exporter import (
    DEFAULT_MAX_SIZE_BYTES,
    FromStateProvider,
    export_view_snapshot,
    parse_size_bytes,
)
from .inspector import inspect_view_snapshot
from .server import serve_forever_cli


def register_view_subparser(commands, json_option) -> None:
    """Register `josh-room view {serve,export,inspect}` subcommands."""
    view = commands.add_parser(
        "view",
        help="localhost read-only Room View dashboard and single-file offline HTML export",
    )
    view_commands = view.add_subparsers(dest="view_command", required=True)

    serve = view_commands.add_parser(
        "serve",
        help="serve a localhost-only read-only Room View dashboard",
    )
    serve.add_argument(
        "--provider",
        required=True,
        help="provider name (room, capsule-manifest, synthetic, synthetic-unix, entry point, or module:attr)",
    )
    serve.add_argument("--port", type=int, default=0, help="localhost port (0 = auto)")
    serve.add_argument("--interval", type=float, default=5.0, help="poll interval in seconds")
    serve.add_argument("--workspace", type=Path, help="Room workspace root (required for --provider room; also used for sensitivity policy)")
    serve.add_argument("--manifest", type=Path, help="LogicalJat JSON manifest path (required for --provider capsule-manifest)")
    serve.add_argument("--require-token", action="store_true", help="require a random per-session URL token")
    serve.add_argument("--stop-on-stdin-eof", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--expected-workspace-binding", type=json.loads, help=argparse.SUPPRESS)
    serve.add_argument("--expected-room-id", help="require this live Room identity before serving")
    serve.add_argument("--expected-dimension-id", help="require this live Dimension identity before serving")
    serve.add_argument("--max-seconds", type=float, help=argparse.SUPPRESS)
    json_option(serve)

    export = view_commands.add_parser(
        "export",
        help="export a single self-contained offline HTML view snapshot",
    )
    source_group = export.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--provider",
        help="provider name (room, capsule-manifest, synthetic, synthetic-unix, entry point, or module:attr)",
    )
    source_group.add_argument(
        "--from-state",
        type=Path,
        dest="from_state",
        help="offline/CI state JSON file containing descriptor and state",
    )
    export.add_argument(
        "--attachments-dir",
        type=Path,
        dest="attachments_dir",
        help="directory holding attachment payloads for --from-state",
    )
    export.add_argument("--output", type=Path, required=True, help="destination .html file path")
    export.add_argument("--no-attachments", action="store_true", help="omit attachment payloads")
    export.add_argument(
        "--max-size",
        default="50MB",
        help="maximum allowed exported HTML size (e.g. 50MB, 25MiB)",
    )
    export.add_argument(
        "--sensitivity-ack",
        action="store_true",
        help="explicitly acknowledge exporting non-public/sensitive Room data",
    )
    export.add_argument("--workspace", type=Path, help="Room workspace root (required for --provider room; also used for sensitivity policy)")
    export.add_argument("--manifest", type=Path, help="LogicalJat JSON manifest path (required for --provider capsule-manifest)")
    export.add_argument("--room-id", dest="room_id", help="optional sanitized Room identifier")
    json_option(export)

    inspect_cmd = view_commands.add_parser(
        "inspect",
        help="inspect an offline view snapshot without rendering it",
    )
    inspect_cmd.add_argument("file", type=Path, help="exported .html view snapshot")
    json_option(inspect_cmd)


def dispatch_view(args) -> dict[str, object]:
    """Execute a parsed `josh-room view` subcommand."""
    sub = args.view_command
    if sub == "inspect":
        return inspect_view_snapshot(args.file)

    if sub == "export":
        if args.from_state is not None:
            provider = FromStateProvider(args.from_state, args.attachments_dir)
        else:
            provider = load_provider(
                args.provider,
                workspace=args.workspace,
                manifest=getattr(args, "manifest", None),
            )
        max_bytes = parse_size_bytes(args.max_size) if args.max_size else DEFAULT_MAX_SIZE_BYTES
        return export_view_snapshot(
            provider,
            args.output,
            include_attachments=not args.no_attachments,
            max_size_bytes=max_bytes,
            sensitivity_ack=bool(args.sensitivity_ack),
            workspace=args.workspace,
            room_id=args.room_id,
        )

    if sub == "serve":
        provider = load_provider(
            args.provider,
            workspace=args.workspace,
            manifest=getattr(args, "manifest", None),
            expected_room_id=args.expected_room_id,
            expected_dimension_id=args.expected_dimension_id,
            expected_binding=args.expected_workspace_binding,
        )
        return serve_forever_cli(
            provider,
            port=args.port,
            interval=args.interval,
            workspace=args.workspace,
            require_token=bool(args.require_token),
            stop_on_stdin_eof=bool(args.stop_on_stdin_eof),
            json_mode=bool(getattr(args, "json", False)),
            max_seconds=getattr(args, "max_seconds", None),
        )

    raise ValueError(f"unsupported view command: {sub}")
