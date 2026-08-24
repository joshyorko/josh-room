import hashlib
import io
import tarfile
import threading

import pytest

from josh_room.catalog import Catalog, CatalogConflict, CatalogFile
from josh_room.crypto import CryptoError, decrypt
from josh_room.envelope import EnvelopeError, build_envelope, read_envelope
from josh_room.local_store import ImmutableLocalStore
from josh_room.operations import _display_name, _snapshot_id, _source_metadata


def test_envelope_round_trip_accepts_exact_manifest_and_payload():
    manifest = {
        "format_version": 1,
        "project_id": "demo-project",
        "snapshot_id": "snap-1",
        "created_at": "2026-08-24T00:00:00Z",
        "payload": {"format": "jat-hauler", "sha256": hashlib.sha256(b"abc").hexdigest(), "size": 3, "producer_version": "synthetic"},
        "source": {"dirty": False},
    }
    envelope = build_envelope(manifest, b"abc")
    assert read_envelope(envelope) == (manifest, b"abc")


def test_envelope_rejects_symlink_member():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("manifest.json")
        body = b"{}"
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo("payload.haul.tar.zst")
        link.type = tarfile.SYMTYPE
        link.linkname = "/outside"
        archive.addfile(link)
    with pytest.raises(EnvelopeError, match="unsupported member type"):
        read_envelope(stream.getvalue())


def test_envelope_rejects_oversized_payload_member(monkeypatch):
    monkeypatch.setattr("josh_room.envelope.MAX_PAYLOAD_SIZE", 1)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        manifest = tarfile.TarInfo("manifest.json")
        manifest_body = b'{"format_version":1,"project_id":"demo","snapshot_id":"one","created_at":"now","payload":{"format":"jat-hauler","sha256":"' + hashlib.sha256(b"xx").hexdigest().encode() + b'","size":2,"producer_version":"test"},"source":{"dirty":false}}'
        manifest.size = len(manifest_body)
        archive.addfile(manifest, io.BytesIO(manifest_body))
        payload = tarfile.TarInfo("payload.haul.tar.zst")
        payload.size = 2
        archive.addfile(payload, io.BytesIO(b"xx"))
    with pytest.raises(EnvelopeError, match="payload too large"):
        read_envelope(stream.getvalue())


def test_identity_permissions_are_restrictive(tmp_path):
    identity = tmp_path / "identity"
    identity.write_text("not a real identity")
    identity.chmod(0o644)
    with pytest.raises(CryptoError, match="permissions"):
        decrypt(tmp_path / "ciphertext", [identity])


def test_local_store_is_immutable_and_content_addressed(tmp_path):
    store = ImmutableLocalStore(tmp_path)
    first = store.put(b"ciphertext")
    assert first.key == "objects/sha256/" + first.sha256
    assert store.get(first.key) == b"ciphertext"
    with pytest.raises(FileExistsError):
        store.put_at_key(first.key, b"ciphertext")


def test_local_store_object_key_is_relative_to_instance_root(tmp_path):
    store = ImmutableLocalStore(tmp_path)
    ref = store.put(b"body")
    assert (tmp_path / ref.key).read_bytes() == b"body"


def test_local_store_rejects_key_digest_mismatch_and_untrusted_paths(tmp_path):
    store = ImmutableLocalStore(tmp_path)
    with pytest.raises(ValueError):
        store.put_at_key("objects/sha256/" + "a" * 64, b"different")
    with pytest.raises(ValueError):
        store.get("objects/../catalog.jroom.age")


def test_local_store_create_only_race_leaves_one_complete_object(tmp_path):
    store = ImmutableLocalStore(tmp_path)
    results = []

    def publish():
        try:
            results.append(store.put(b"same"))
        except FileExistsError:
            results.append("exists")

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(str(result) for result in results).count("exists") == 1
    ref = next(result for result in results if result != "exists")
    assert store.get(ref.key) == b"same"
    assert not list(tmp_path.rglob("*.partial"))


def test_catalog_file_rejects_stale_revision_and_preserves_old_on_interruption(tmp_path, monkeypatch):
    path = tmp_path / "catalog.jroom.age"
    identities = tmp_path / "identities"
    identities.mkdir()
    monkeypatch.setenv("JOSH_ROOM_IDENTITY", str(identities / "daily"))
    monkeypatch.setattr("josh_room.catalog.encrypt", lambda body, _recipients, output: output.write_bytes(body))
    monkeypatch.setattr("josh_room.catalog.decrypt", lambda path, _identities: path.read_bytes())
    catalog_file = CatalogFile(path, identities / "daily")
    initial = Catalog.empty().add_snapshot("demo", "Demo", {"snapshot_id": "one", "object_key": "objects/sha256/" + "1" * 64, "ciphertext_sha256": "1" * 64, "ciphertext_size": 1})
    catalog_file.write(initial, ["age1synthetic", "age1recovery"])
    current = catalog_file.read()
    newer = current.add_snapshot("demo", "Demo", {"snapshot_id": "two", "object_key": "objects/sha256/" + "2" * 64, "ciphertext_sha256": "2" * 64, "ciphertext_size": 2})
    with pytest.raises(CatalogConflict):
        catalog_file.update_if_revision(current.body["revision"] - 1, newer, ["age1synthetic", "age1recovery"])
    old_ciphertext = path.read_bytes()
    monkeypatch.setattr("josh_room.catalog.os.replace", lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        catalog_file.update_if_revision(current.body["revision"], newer, ["age1synthetic", "age1recovery"])
    assert path.read_bytes() == old_ciphertext
    assert not [item for item in tmp_path.glob(".catalog.jroom.*") if item.name != ".catalog.jroom.lock"]


def test_catalog_resolves_explicit_latest_and_rejects_stale_revision():
    catalog = Catalog.empty()
    catalog = catalog.add_snapshot("demo", "Demo Project", {"snapshot_id": "snap-1", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1})
    assert catalog.latest("demo")["snapshot_id"] == "snap-1"
    with pytest.raises(CatalogConflict):
        catalog.update_if_revision(0, {"format_version": 1, "revision": 99, "projects": {}})


def test_catalog_rejects_untrusted_object_key():
    with pytest.raises(ValueError, match="object key"):
        Catalog({"format_version": 1, "revision": 1, "projects": {"demo": {"display_name": "Demo", "latest": "one", "snapshots": {"one": {"snapshot_id": "one", "object_key": "objects/../secret", "ciphertext_sha256": "0" * 64, "ciphertext_size": 1}}}}})


def test_non_git_source_does_not_claim_clean(tmp_path):
    assert _source_metadata(tmp_path) == {}


def test_git_source_reports_commit_and_dirty_state(tmp_path):
    subprocess = __import__("subprocess")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Synthetic"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "synthetic@example.invalid"], check=True)
    (tmp_path / "file.txt").write_text("one")
    subprocess.run(["git", "-C", str(tmp_path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "synthetic"], check=True)
    clean = _source_metadata(tmp_path)
    assert clean["dirty"] is False
    assert len(clean["git_commit"]) == 40
    (tmp_path / "file.txt").write_text("two")
    assert _source_metadata(tmp_path)["dirty"] is True


def test_snapshot_ids_identify_events_not_payload_content():
    first = _snapshot_id()
    second = _snapshot_id()
    assert first != second
    assert len(first) == 32


def test_logical_project_ids_get_human_display_names():
    assert _display_name("hive") == "Hive"
    assert _display_name("room-of-requirement") == "Room Of Requirement"
