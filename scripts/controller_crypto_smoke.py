#!/usr/bin/env python3
"""Prove managed age encryption and decryption through the controller runtime."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from josh_room.crypto import _managed_executable, decrypt_file, encrypt_file


def _recipient(keygen: Path, identity: Path) -> str:
    created = subprocess.run(
        [str(keygen), "-o", str(identity)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode:
        raise RuntimeError(f"age-keygen failed: {created.stderr.strip()}")
    identity.chmod(0o600)
    derived = subprocess.run(
        [str(keygen), "-y", str(identity)],
        capture_output=True,
        text=True,
        check=False,
    )
    if derived.returncode or not derived.stdout.strip().startswith("age1"):
        raise RuntimeError(f"age-keygen recipient derivation failed: {derived.stderr.strip()}")
    return derived.stdout.strip()


def main() -> int:
    age = _managed_executable("age")
    keygen = _managed_executable("age-keygen")
    payload = b"josh-room-controller-managed-age-smoke\n"
    with tempfile.TemporaryDirectory(prefix="josh-room-controller-crypto-") as directory:
        root = Path(directory)
        identity_one = root / "identity-one.txt"
        identity_two = root / "identity-two.txt"
        recipients = [_recipient(keygen, identity_one), _recipient(keygen, identity_two)]
        source = root / "source.bin"
        encrypted = root / "source.bin.age"
        decrypted = root / "decrypted.bin"
        source.write_bytes(payload)
        encrypt_file(source, recipients, encrypted)
        size, digest = decrypt_file(encrypted, [identity_one], decrypted)
        if decrypted.read_bytes() != payload or size != len(payload) or digest != hashlib.sha256(payload).hexdigest():
            raise RuntimeError("managed age round trip did not preserve the controller payload")
    print(json.dumps({"age": str(age), "age_keygen": str(keygen), "ok": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
