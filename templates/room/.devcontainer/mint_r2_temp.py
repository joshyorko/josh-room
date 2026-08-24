#!/usr/bin/env python3
"""Mint Cloudflare R2 temporary credentials from a parent S3 credential."""

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse


def _b64url(body: bytes) -> str:
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode()


def main(input_path: Path, output_path: Path) -> None:
    request = json.loads(input_path.read_text())
    required = {"endpoint", "bucket", "account_id", "access_key_id", "secret_access_key", "ttl_seconds"}
    if set(request) != required:
        raise ValueError("temporary credential input fields are invalid")
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode())
    claims = {
        "aud": urlparse(request["endpoint"]).netloc,
        "bucket": request["bucket"],
        "exp": now + int(request["ttl_seconds"]),
        "iat": now,
        "iss": request["access_key_id"],
        "scope": "object-read-write",
        "sub": request["account_id"],
    }
    payload = _b64url(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64url(
        hmac.new(request["secret_access_key"].encode(), signing_input, hashlib.sha256).digest()
    )
    jwt = f"{header}.{payload}.{signature}"
    result = {
        "access-key-id": request["access_key_id"],
        "secret-access-key": hashlib.sha256(jwt.encode()).hexdigest(),
        "session-token": base64.b64encode(f"jwt/{jwt}".encode()).decode(),
    }
    output_path.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n")
    output_path.chmod(0o600)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: mint_r2_temp.py INPUT OUTPUT")
    main(Path(sys.argv[1]), Path(sys.argv[2]))

