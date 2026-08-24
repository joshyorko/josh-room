import os
import shutil
import subprocess
from pathlib import Path

import pytest

from josh_room.cli import list_projects
from josh_room.operations import create_snapshot, hydrate
from josh_room.r2 import R2Backend, R2Config


@pytest.mark.r2
def test_secret_gated_r2_create_readback_catalog_and_fresh_hydrate(tmp_path, monkeypatch):
    if os.environ.get("JOSH_ROOM_R2_LIVE") != "1":
        pytest.skip("real R2 acceptance is secret-gated")
    required = ["JOSH_ROOM_R2_ENDPOINT", "JOSH_ROOM_R2_BUCKET", "JOSH_ROOM_R2_PROFILE", "JOSH_ROOM_R2_JAT_ROOT"]
    if any(not os.environ.get(name) for name in required) or not shutil.which("age-keygen"):
        pytest.skip("R2 acceptance configuration or tools are unavailable")
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    payload = source / "nested" / "file.txt"
    payload.write_text("synthetic R2 acceptance fixture\n")
    os.chmod(payload, 0o640)
    identities = []
    recipients = []
    for name in ("daily", "recovery"):
        identity = tmp_path / f"{name}.agekey"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        identity.chmod(0o600)
        identities.append(identity)
        recipients.append(next(line.removeprefix("# public key: ") for line in identity.read_text().splitlines() if line.startswith("# public key: ")))
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identities[0]))
    backend = R2Backend(R2Config(os.environ["JOSH_ROOM_R2_ENDPOINT"], os.environ["JOSH_ROOM_R2_BUCKET"], os.environ["JOSH_ROOM_R2_PROFILE"]), receipt_dir=tmp_path / "receipts")
    multipart_backend = R2Backend(
        R2Config(
            os.environ["JOSH_ROOM_R2_ENDPOINT"],
            os.environ["JOSH_ROOM_R2_BUCKET"],
            os.environ["JOSH_ROOM_R2_PROFILE"],
            multipart_threshold=1024 * 1024,
            multipart_chunk_size=5 * 1024 * 1024,
        ),
        receipt_dir=tmp_path / "receipts",
    )
    multipart_body = os.urandom(6 * 1024 * 1024)
    multipart_digest = __import__("hashlib").sha256(multipart_body).hexdigest()
    multipart_ref = multipart_backend.put_bytes(f"objects/sha256/{multipart_digest}", multipart_body)
    assert multipart_ref.size == len(multipart_body)
    instance = tmp_path / "instance"
    result = create_snapshot(instance, "r2-demo", source, Path(os.environ["JOSH_ROOM_R2_JAT_ROOT"]), recipients, backend)
    assert len(result["ciphertext_sha256"]) == 64
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert list_projects(fresh, backend) == [("r2-demo", "r2-demo")]
    destination = tmp_path / "hydrated"
    hydrate(fresh, "r2-demo", destination, identities[0], Path(os.environ["JOSH_ROOM_R2_JAT_ROOT"]), backend)
    restored = destination / "workspace" / "source" / "nested" / "file.txt"
    assert restored.read_text() == "synthetic R2 acceptance fixture\n"
    assert restored.stat().st_mode & 0o777 == 0o640
    backend.client.delete_objects(
        Bucket=backend.config.bucket,
        Delete={
            "Objects": [
                {"Key": multipart_ref.key},
                {"Key": result["object_key"]},
                {"Key": backend.config.catalog_key},
            ],
            "Quiet": True,
        },
    )
