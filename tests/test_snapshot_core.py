import hashlib
import io
import json
import resource
import tarfile
import threading
from pathlib import Path

import pytest

from josh_room.catalog import Catalog, CatalogConflict, CatalogFile
from josh_room.crypto import CryptoError, decrypt, decrypt_file, encrypt_file
from josh_room.envelope import (
    EnvelopeError,
    build_envelope,
    build_envelope_file,
    read_envelope,
    read_envelope_file,
)
from josh_room.keyring import lookup
from josh_room.local_store import ImmutableLocalStore
from josh_room.operations import (
    _display_name,
    _snapshot_id,
    _source_metadata,
    create_snapshot,
)
from josh_room.workspace_state import read_workspace_marker, workspace_fingerprint


def test_environment_artifact_receipt_shape_is_accepted():
    manifest = {"format_version": 1, "project_id": "demo", "snapshot_id": "s", "created_at": "now", "payload": {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}, "source": {}, "environment_artifact": {"artifact": "sha256:" + "b" * 64, "specification_digest": "sha256:" + "c" * 64, "legacy_blueprint_key": "legacy", "archive": "jat-runtime.rcca", "archive_sha256": "d" * 64, "archive_size": 1, "rcc_version": "v18.19.2", "robot": "robot.yaml", "provider": "local", "acquired": False}}
    build_envelope(manifest, b"x")


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


def test_file_envelope_preserves_format_and_streams_payload(tmp_path):
    payload = tmp_path / "payload.haul.tar.zst"
    payload.write_bytes(b"abc")
    manifest = {
        "format_version": 1,
        "project_id": "demo-project",
        "snapshot_id": "snap-1",
        "created_at": "2026-08-24T00:00:00Z",
        "payload": {"format": "jat-hauler", "sha256": hashlib.sha256(b"abc").hexdigest(), "size": 3, "producer_version": "synthetic"},
        "source": {},
    }
    envelope = tmp_path / "snapshot.tar"
    build_envelope_file(manifest, payload, envelope)
    assert envelope.read_bytes() == build_envelope(manifest, b"abc")
    restored = tmp_path / "restored.haul.tar.zst"
    assert read_envelope_file(envelope, restored) == manifest
    assert restored.read_bytes() == b"abc"
    assert restored.stat().st_mode & 0o777 == 0o600


def test_file_crypto_uses_paths_without_subprocess_input(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    encrypted = tmp_path / "encrypted"
    decrypted = tmp_path / "decrypted"
    identity = tmp_path / "identity"
    identity.write_text("synthetic")
    identity.chmod(0o600)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = Path(argv[argv.index("-o") + 1])
        output.write_bytes(b"ciphertext" if "--decrypt" not in argv else b"payload")
        return __import__("subprocess").CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("josh_room.crypto.subprocess.run", fake_run)
    encrypt_file(source, ["age1daily", "age1recovery"], encrypted)
    size, digest = decrypt_file(encrypted, [identity], decrypted)
    assert all("input" not in kwargs for _argv, kwargs in calls)
    assert str(source) in calls[0][0]
    assert str(encrypted) in calls[1][0]
    assert decrypted.read_bytes() == b"payload"
    assert size == 7
    assert digest == hashlib.sha256(b"payload").hexdigest()


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


def test_runtime_secret_file_is_ephemeral_keyring_source(tmp_path, monkeypatch):
    runtime = tmp_path / "r2.json"
    runtime.write_text(
        '{"access-key-id":"temporary","secret-access-key":"temporary-secret","session-token":"session"}'
    )
    runtime.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(runtime))
    monkeypatch.setattr("josh_room.keyring.available", lambda: False)
    assert lookup("oauth-runtime") == {
        "access-key-id": "temporary",
        "secret-access-key": "temporary-secret",
        "session-token": "session",
    }


def test_runtime_secret_file_allows_static_minio_credentials(tmp_path, monkeypatch):
    runtime = tmp_path / "minio.json"
    runtime.write_text(
        '{"access-key-id":"temporary","secret-access-key":"temporary-secret"}'
    )
    runtime.chmod(0o600)
    monkeypatch.setenv("JOSH_ROOM_RUNTIME_CREDENTIALS", str(runtime))
    monkeypatch.setattr("josh_room.keyring.available", lambda: False)
    assert lookup("oauth-runtime") == {
        "access-key-id": "temporary",
        "secret-access-key": "temporary-secret",
    }


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


def test_local_store_streams_file_publication_and_download(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"streamed-body" * 100_000)
    store = ImmutableLocalStore(tmp_path / "store")
    ref = store.put_file(source)
    restored = tmp_path / "restored.bin"
    store.download_file(ref.key, restored, ref.sha256, ref.size)
    assert restored.read_bytes() == source.read_bytes()
    assert restored.stat().st_mode & 0o777 == 0o600


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


def test_workspace_fingerprint_includes_mode_changes_and_empty_directories(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "nested").mkdir()
    payload = root / "nested" / "file.txt"
    payload.write_text("one")
    baseline = workspace_fingerprint(root)

    payload.chmod(payload.stat().st_mode ^ 0o111)
    assert workspace_fingerprint(root) != baseline

    payload.chmod(payload.stat().st_mode ^ 0o111)
    (root / "empty").mkdir()
    assert workspace_fingerprint(root) != baseline


def test_workspace_fingerprint_streams_large_files_with_bounded_memory(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    payload = root / "large.bin"
    size = 64 * 1024 * 1024 + 1
    with payload.open("wb") as handle:
        handle.truncate(size)
        handle.seek(size - 1)
        handle.write(b"x")
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    fingerprint = workspace_fingerprint(root)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert len(fingerprint) == 64
    assert after - before < 16 * 1024


def test_workspace_marker_v1_accepts_legacy_display_name_without_snapshot_id(tmp_path):
    marker = {"format_version": 1, "project_id": "demo", "display_name": "Demo"}
    (tmp_path / ".josh-room.json").write_text(json.dumps(marker))
    assert read_workspace_marker(tmp_path) == marker


def test_create_snapshot_rejects_source_mutation_during_build(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("original")
    instance = tmp_path / "instance"

    class Backend:
        def __init__(self):
            self.config = type("Config", (), {"dimension_id": "archive"})()
            self.calls = []

        def put_file(self, *args, **kwargs):
            self.calls.append("put_file")
            raise AssertionError("publish should not happen")

        def conditional_catalog_put(self, *args, **kwargs):
            self.calls.append("catalog_put")
            raise AssertionError("publish should not happen")

        def record_orphan(self, ref):
            self.calls.append(("orphan", ref.key))

    backend = Backend()

    def fake_run_build(_jat_root, source_root, haul, **_kwargs):
        (source_root / "file.txt").write_text("mutated")
        haul.write_bytes(b"haul")
        return {"version": "synthetic"}

    monkeypatch.setattr("josh_room.operations.run_build", fake_run_build)
    monkeypatch.setattr("josh_room.operations.build_envelope_file", lambda manifest, haul, envelope: envelope.write_bytes(b"envelope"))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda envelope, recipients, encrypted: encrypted.write_bytes(b"ciphertext"))
    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty(dimension_id="archive"), None))

    with pytest.raises(ValueError, match="source workspace changed during snapshot capture"):
        create_snapshot(instance, "room", source, tmp_path / "jat", ["age1daily", "age1recovery"], backend, display_name="Room")

    assert backend.calls == []
    assert not (source / ".josh-room.json").exists()


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
    assert catalog.resolve_snapshot("demo", "snap-1")["snapshot_id"] == "snap-1"
    assert catalog.resolve_snapshot("demo", "latest")["snapshot_id"] == "snap-1"
    with pytest.raises(CatalogConflict):
        catalog.update_if_revision(0, {"format_version": 1, "revision": 99, "projects": {}})


def test_catalog_remove_room_returns_only_unreferenced_objects():
    shared = {"snapshot_id": "shared", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1}
    unique = {"snapshot_id": "unique", "object_key": "objects/sha256/" + "b" * 64, "ciphertext_sha256": "b" * 64, "ciphertext_size": 1}
    catalog = Catalog.empty().add_snapshot("one", "One", shared).add_snapshot("one", "One", unique)
    catalog = catalog.add_snapshot("two", "Two", shared)

    updated, removable, snapshot_count = catalog.remove_project("one")

    assert set(updated.body["projects"]) == {"two"}
    assert removable == [unique["object_key"]]
    assert snapshot_count == 2
    assert updated.body["revision"] == catalog.body["revision"] + 1


def test_catalog_remove_latest_snapshot_promotes_previous_and_keeps_shared_objects():
    shared = {"snapshot_id": "one", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1}
    latest = {"snapshot_id": "two", "object_key": "objects/sha256/" + "b" * 64, "ciphertext_sha256": "b" * 64, "ciphertext_size": 2}
    catalog = Catalog.empty().add_snapshot("demo", "Demo", shared).add_snapshot("demo", "Demo", latest)
    catalog = catalog.add_snapshot("other", "Other", latest)

    updated, removable, room_removed = catalog.remove_snapshot("demo", "two")

    assert updated.body["projects"]["demo"]["latest"] == "one"
    assert set(updated.body["projects"]["demo"]["snapshots"]) == {"one"}
    assert removable == []
    assert room_removed is False
    assert updated.body["revision"] == catalog.body["revision"] + 1


def test_catalog_remove_final_snapshot_removes_the_room():
    snapshot = {"snapshot_id": "only", "object_key": "objects/sha256/" + "a" * 64, "ciphertext_sha256": "a" * 64, "ciphertext_size": 1}
    catalog = Catalog.empty().add_snapshot("demo", "Demo", snapshot)
    updated, removable, room_removed = catalog.remove_snapshot("demo", "only")
    assert "demo" not in updated.body["projects"]
    assert removable == [snapshot["object_key"]]
    assert room_removed is True


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
