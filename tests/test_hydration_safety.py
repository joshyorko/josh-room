import os
import subprocess
from pathlib import Path

import pytest

from josh_room.catalog import Catalog, CatalogFile
from josh_room.crypto import encrypt
from josh_room.envelope import build_envelope
from josh_room.local_store import ImmutableLocalStore
from josh_room.operations import hydrate, remove_room, serve_snapshot


def _encrypted_instance(tmp_path):
    identities = []
    recipients = []
    for name in ("daily", "recovery"):
        identity = tmp_path / f"{name}.agekey"
        subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
        os.chmod(identity, 0o600)
        identities.append(identity)
        recipients.append(next(line.removeprefix("# public key: ") for line in identity.read_text().splitlines() if line.startswith("# public key: ")))
    instance = tmp_path / "instance"
    instance.mkdir()
    payload = b"synthetic payload"
    manifest = {"format_version": 1, "project_id": "demo", "snapshot_id": "snap", "created_at": "2026-08-24T00:00:00Z", "payload": {"format": "jat-hauler", "sha256": __import__("hashlib").sha256(payload).hexdigest(), "size": len(payload), "producer_version": "test"}, "source": {"dirty": False}}
    encrypted = tmp_path / "snapshot.age"
    encrypt(build_envelope(manifest, payload), recipients, encrypted)
    ref = ImmutableLocalStore(instance).put(encrypted.read_bytes())
    catalog = Catalog.empty().add_snapshot("demo", "Demo", {"snapshot_id": "snap", "object_key": ref.key, "ciphertext_sha256": ref.sha256, "ciphertext_size": ref.size})
    CatalogFile(instance / "catalog.jroom.age", identities[0]).write(catalog, recipients)
    return instance, identities[0]


def test_hydrate_promotes_adjacent_stage_and_handles_known_empty_destination(tmp_path, monkeypatch):
    instance, identity = _encrypted_instance(tmp_path)

    def fake_restore(_jat, _haul, destination):
        (destination / "workspace" / "source").mkdir(parents=True)
        (destination / "workspace" / "source" / "file.txt").write_text("restored")
        return {"argv": ["synthetic-jat"], "exit_status": 0}

    monkeypatch.setattr("josh_room.operations.run_restore", fake_restore)
    destination = tmp_path / "workspace"
    destination.mkdir()
    result = hydrate(instance, "demo", destination, identity, tmp_path)
    assert (destination / "file.txt").read_text() == "restored"
    marker = __import__("json").loads((destination / ".josh-room.json").read_text())
    assert marker == {"display_name": "Demo", "format_version": 1, "project_id": "demo"}
    assert Path(result["receipt"]).read_text().find('"status": "success"') >= 0
    assert not list(tmp_path.glob(".workspace.josh-room-*"))


def test_hydrate_preserves_nonempty_destination(tmp_path):
    instance, identity = _encrypted_instance(tmp_path)
    destination = tmp_path / "workspace"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("existing")
    with pytest.raises(FileExistsError):
        hydrate(instance, "demo", destination, identity, tmp_path)
    assert sentinel.read_text() == "existing"


def test_hydrate_cleans_owned_stage_and_records_interrupt(tmp_path, monkeypatch):
    instance, identity = _encrypted_instance(tmp_path)

    def interrupted(_jat, _haul, _destination):
        raise KeyboardInterrupt

    monkeypatch.setattr("josh_room.operations.run_restore", interrupted)
    with pytest.raises(KeyboardInterrupt):
        hydrate(instance, "demo", tmp_path / "workspace", identity, tmp_path)
    assert not list(tmp_path.glob(".workspace.josh-room-*"))
    receipts = list((instance / "receipts").glob("*.json"))
    assert len(receipts) == 1
    assert '"status": "failed"' in receipts[0].read_text()


def test_remove_room_updates_catalog_before_deleting_unreferenced_objects(tmp_path, monkeypatch):
    snapshot = {"snapshot_id": "one", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1}
    catalog = Catalog.empty().add_snapshot("demo", "Demo", snapshot)
    calls = []

    class Backend:
        def conditional_catalog_put(self, body, etag):
            calls.append(("catalog", body, etag))

        def delete_object(self, key):
            calls.append(("delete", key))

    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args: (catalog, '"etag"'))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"encrypted-catalog")

    result = remove_room(tmp_path / "instance", "demo", tmp_path / "identity", ["age1one", "age1two"], Backend())

    assert calls == [
        ("catalog", b"encrypted-catalog", '"etag"'),
        ("delete", snapshot["object_key"]),
    ]
    assert result == {"deleted_objects": 1, "project_id": "demo", "snapshot_count": 1}


def test_serve_snapshot_materializes_private_haul_for_foreground_jat(tmp_path, monkeypatch):
    snapshot = {"snapshot_id": "one", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1}
    catalog = Catalog.empty().add_snapshot("demo", "Demo", snapshot)
    captured = {}

    class Backend:
        def download_file(self, _key, destination, _digest, _size):
            destination.write_bytes(b"encrypted")

    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args: (catalog, None))
    monkeypatch.setattr("josh_room.operations.decrypt_file", lambda _source, _identities, destination: destination.write_bytes(b"envelope"))

    def read_envelope(_envelope, haul):
        haul.write_bytes(b"haul")
        return {"project_id": "demo"}

    monkeypatch.setattr("josh_room.operations.read_envelope_file", read_envelope)
    monkeypatch.setattr("josh_room.operations.run_serve", lambda _jat, haul: captured.update(path=haul, body=haul.read_bytes()) or {"success": True})

    result = serve_snapshot(tmp_path / "instance", "demo", "latest", tmp_path / "identity", tmp_path / "jat", Backend())

    assert result["success"] is True
    assert captured["body"] == b"haul"
    assert not captured["path"].exists()
