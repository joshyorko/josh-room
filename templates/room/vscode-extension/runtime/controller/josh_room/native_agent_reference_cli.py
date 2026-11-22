"""Explicit native-keyring CLI authority for historical agent sidecars."""

import os
from contextlib import contextmanager
from types import SimpleNamespace

from . import native_agent_reference as references
from . import native_agents as native
from .config import DimensionRegistry, private_config


@contextmanager
def _context(args):
    from . import cli

    native._linux()
    if args.snapshot == "latest" or not native._LABEL.fullmatch(args.snapshot):
        native._fail("native-reference-exact-history-required")
    if any(os.environ.get(name) for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_PROVIDER_CREDENTIALS", "JOSH_ROOM_ENCRYPTION_MATERIAL")):
        native._fail("native-reference-native-keyring-required")
    dimension = DimensionRegistry(private_config() or {}).select(args.dimension)
    if dimension.provider not in {"minio", "s3"}:
        native._fail("native-admission-provider-unqualified")
    selected = SimpleNamespace(command="snapshot", snapshot_command="inspect", backend=dimension.provider,
                               dimension=args.dimension)
    instance = cli._instance_root()
    with cli._selected_encryption_environment(selected, instance) as material, cli._open_room_store_context(
        selected, instance, project_id=args.room, snapshot_id=args.snapshot, material=material,
    ) as context:
        yield context


def dispatch(args):
    grant = None
    filters = [getattr(args, field, None) for field in ("workspace", "profile_ref", "tool")]
    if args.action == "recovery-status" and any(value is not None for value in filters) and not all(value is not None for value in filters):
        native._fail("native-profile-selector-incomplete")
    if args.action != "recovery-status" or all(value is not None for value in filters):
        from .native_agent_discovery import resolve_profile

        grant = resolve_profile(dimension=args.dimension, room=args.room, workspace=args.workspace,
                                profile_ref=args.profile_ref, tool=args.tool)
    with _context(args) as context:
        if args.action == "recovery-status":
            if grant is not None:
                current, _, _ = references._grant(context, grant["profile"], args.tool, grant["workspace"], "room-verify")
                if current != grant:
                    native._fail("native-profile-selector-stale-or-unavailable")
            result = references.inspect_reference(context, profile=None if grant is None else grant["profile"],
                                                   tool=None if grant is None else args.tool)
            return {"ok": True, **(result or {"state": "reference-absent", "entries": [], "history_only": True,
                                              "contract_version": 2, "format": None,
                                              "native_resume_verified": False, "original_t3_identity_restored": False})}
        if args.action == "recovery-capture":
            return references.capture_reference(context, grant=grant, tool=args.tool)
        if args.action == "recovery-verify":
            return references.verify_reference(context, grant=grant, tool=args.tool, capture_id=args.capture_id, export_path=args.export)
        return references.restore_reference(context, grant=grant, tool=args.tool,
                                             session_id=args.session_id, capture_id=args.capture_id, export_path=args.export)
