import base64
import hashlib
import hmac
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_local_r2_temporary_credential_is_scoped_signed_and_short_lived(tmp_path):
    parent_secret = "synthetic-parent-secret"
    request = {
        "endpoint": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        "bucket": "synthetic-room",
        "account_id": "0123456789abcdef0123456789abcdef",
        "access_key_id": "synthetic-parent-access",
        "secret_access_key": parent_secret,
        "ttl_seconds": 21600,
    }
    source = tmp_path / "input.json"
    output = tmp_path / "output.json"
    source.write_text(json.dumps(request))
    source.chmod(0o600)
    subprocess.run(
        ["python3", str(ROOT / ".devcontainer/mint_r2_temp.py"), str(source), str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    credentials = json.loads(output.read_text())
    assert credentials["access-key-id"] == request["access_key_id"]
    assert len(credentials["secret-access-key"]) == 64
    jwt = base64.b64decode(credentials["session-token"]).decode().removeprefix("jwt/")
    header, payload, signature = jwt.split(".")
    claims = json.loads(_decode_url(payload))
    assert json.loads(_decode_url(header)) == {"alg": "HS256", "typ": "JWT"}
    assert claims["bucket"] == "synthetic-room"
    assert claims["scope"] == "object-read-write"
    assert claims["exp"] - claims["iat"] == 21600
    expected = _encode_url(hmac.new(parent_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    assert hmac.compare_digest(signature, expected)
    assert credentials["secret-access-key"] == hashlib.sha256(jwt.encode()).hexdigest()


def _decode_url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _encode_url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
