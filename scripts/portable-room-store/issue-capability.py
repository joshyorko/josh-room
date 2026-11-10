#!/usr/bin/env python3
"""Issue a short-lived scoped capability into a private file, never argv or stdout."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from josh_room.private_paths import verify_private_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signing-key-file", required=True, type=Path)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--access", choices=("read", "write"), default="read")
    parser.add_argument("--expires-in", type=int, default=600)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--keyring-profile")
    args = parser.parse_args()
    import re
    if not re.fullmatch(r"[0-9a-f]{64}", args.domain) or not 1 <= args.expires_in <= 3600:
        parser.error("a SHA-256 domain and a lifetime of 1..3600 seconds are required")
    verify_private_path(args.signing_key_file, directory=False)
    encoded = args.signing_key_file.read_text().strip()
    key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(key) != 32 or base64.urlsafe_b64encode(key).decode().rstrip("=") != encoded:
        parser.error("signing key is invalid")
    def b64(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    now = int(time.time())
    payload = b64(json.dumps({"domain": args.domain, "access": args.access, "iat": now, "exp": now + args.expires_in}, separators=(",", ":")).encode())
    token = payload + "." + b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token)
    if args.keyring_profile:
        from josh_room.keyring import secure_store
        secure_store(args.keyring_profile, "room-store-capability", token)


if __name__ == "__main__":
    main()
