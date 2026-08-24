import hashlib
import os
import shutil
from pathlib import Path

import pytest

from josh_room.crypto import decrypt
from josh_room.envelope import read_envelope
from josh_room.operations import create_snapshot, hydrate


def _tooling():
    jat_root = os.environ.get("JOSH_ROOM_JAT_ROOT")
    required = [shutil.which(tool) for tool in ("age", "age-keygen", "hauler", "tar", "zstd")]
    if not jat_root or not Path(jat_root, "robot.yaml").is_file() or not Path(jat_root, "tasks.py").is_file() or "  JAT:" not in Path(jat_root, "robot.yaml").read_text() or not all(required) or not shutil.which("rcc"):
        pytest.skip("real RCC-first JAT/Hauler/age/tar/zstd tooling is unavailable")
    return Path(jat_root)


@pytest.mark.integration
def test_real_jat_age_dual_recipient_fresh_state_hydration(tmp_path):
    jat_root = _tooling()
    source = tmp_path / "source"
    source.mkdir()
    payload = source / "nested" / "file.txt"
    payload.parent.mkdir()
    payload.write_text("synthetic Josh Room fixture\n")
    os.chmod(payload, 0o640)
    daily_identity = tmp_path / "daily.agekey"
    recovery_identity = tmp_path / "recovery.agekey"
    for identity in (daily_identity, recovery_identity):
        result = __import__("subprocess").run(["age-keygen", "-o", str(identity)], capture_output=True, text=True, check=True)
        assert result.returncode == 0
        os.chmod(identity, 0o600)
    daily_recipient = next(line.removeprefix("# public key: ") for line in daily_identity.read_text().splitlines() if line.startswith("# public key: "))
    recovery_recipient = next(line.removeprefix("# public key: ") for line in recovery_identity.read_text().splitlines() if line.startswith("# public key: "))

    instance = tmp_path / "instance"
    result = create_snapshot(instance, "demo-project", source, jat_root, [daily_recipient, recovery_recipient])
    assert result["producer"]["exit_status"] == 0
    assert result["producer"]["argv"][0:6] == ["rcc", "run", "-r", str(jat_root / "robot.yaml"), "-t", "Build"]
    assert result["producer"]["payload_size"] > 0
    assert len(result["producer"]["payload_sha256"]) == 64
    object_path = instance / result["object_key"]
    encrypted = object_path.read_bytes()
    assert hashlib.sha256(encrypted).hexdigest() == result["ciphertext_sha256"]
    for identity in (daily_identity, recovery_identity):
        manifest, plain_payload = read_envelope(decrypt(object_path, [identity]))
        assert manifest["project_id"] == "demo-project"
        assert hashlib.sha256(plain_payload).hexdigest() == manifest["payload"]["sha256"]

    fresh_instance = tmp_path / "fresh-instance"
    shutil.copytree(instance, fresh_instance)
    daily_destination = tmp_path / "daily-workspace"
    recovery_destination = tmp_path / "recovery-workspace"
    hydrate(fresh_instance, "demo-project", daily_destination, daily_identity, jat_root)
    hydrate(fresh_instance, "demo-project", recovery_destination, recovery_identity, jat_root)
    for destination in (daily_destination, recovery_destination):
        restored = destination / "nested" / "file.txt"
        assert restored.read_text() == "synthetic Josh Room fixture\n"
        assert restored.stat().st_mode & 0o777 == 0o640
