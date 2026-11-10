#!/usr/bin/env python3
"""Plan or explicitly migrate a quiesced MinIO Dimension's Room Store authority."""
import argparse
import json

from josh_room import auth
from josh_room import portable_room_store as portable
from josh_room.config import private_config, resolve_dimension
from josh_room.encryption_domain import KEYSET_CONTROL_KEY, EncryptionKeyset
from josh_room.minio import MinioBackend, MinioConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "migrate", "rollback"))
    parser.add_argument("--dimension", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    if args.action != "plan" and not (args.execute and args.quiesced):
        parser.error("migration and rollback require --execute --quiesced after stopping all clients")
    selected = resolve_dimension(private_config(), args.dimension)
    if selected.provider != "minio":
        parser.error("only explicitly configured MinIO Dimensions can migrate")
    backend = MinioBackend(MinioConfig.from_dimension(selected))
    raw, etag = backend.read_control(KEYSET_CONTROL_KEY, 65536)
    if raw is None or not etag:
        raise RuntimeError("a versioned encryption keyset is required")
    body = json.loads(raw)
    marker = body.get("format") == portable.MARKER_FORMAT
    if args.action == "plan":
        print(json.dumps({"authority": "portable-do" if marker else "legacy", "target": "portable-do" if not marker else "sealed-legacy-rollback", "physicalDomain": portable.domain_id(selected), "requiresQuiescence": True}))
    elif args.action == "migrate":
        if marker:
            raise RuntimeError("already selected; verify with read-only Room Store operations")
        keyset = EncryptionKeyset.from_json(raw, provider="minio", endpoint=selected.endpoint, bucket=selected.bucket)
        material = auth._material_for_keyset(keyset)
        portable.migrate(selected, backend, material)
        print('{"status":"migrated"}')
    else:
        portable.rollback(selected, backend)
        print('{"status":"rolled-back-authority-permanently-sealed"}')


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SystemExit("Room Store authority operation failed; no automatic fallback was attempted") from None
