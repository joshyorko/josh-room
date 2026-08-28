import hashlib
import json
import os
import resource
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import cli, workspace_state
from josh_room.auth import _write_runtime
from josh_room.catalog import Catalog
from josh_room.local_store import ImmutableLocalStore, ObjectRef
from josh_room.operations import copy_snapshot_stream, create_snapshot

BASE_HEAD = "e6456abd6ed7d64ddcf2c400d82a5a33172d97fb"


def _dimension(provider, endpoint, bucket, profile):
    return {
        "display_name": provider.title(),
        "provider": provider,
        "endpoint": endpoint,
        "bucket": bucket,
        "credential_profile": profile,
    }


def _snapshot(*, origin=None):
    payload = b"ciphertext"
    body = {
        "snapshot_id": "jat-1",
        "created_at": "2026-08-26T00:00:00+00:00",
        "object_key": "objects/sha256/" + hashlib.sha256(payload).hexdigest(),
        "ciphertext_sha256": hashlib.sha256(payload).hexdigest(),
        "ciphertext_size": len(payload),
        "workspace_fingerprint": "a" * 64,
    }
    if origin is not None:
        body["origin_project_id"] = origin
    return body


def test_clean_bootstrap_uses_exact_repaired_candidate_and_cli_contract():
    lock = json.loads(Path("release-lock.json").read_text())
    assert lock["josh_room"]["git_sha"] == BASE_HEAD
    assert "josh-room = \"josh_room.cli:main\"" in Path("pyproject.toml").read_text()
    for path in (Path(".devcontainer/bootstrap.sh"), Path("templates/room/.devcontainer/bootstrap.sh")):
        body = path.read_text()
        assert "joshyorko.josh-room-0.1.6" in body
        assert "uv tool install" not in body
        assert "brew" not in body.lower()


def test_oauth_runtime_overlay_preserves_named_dimensions_and_default_routing(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    persisted = {
        "default_dimension": "minio",
        "dimensions": {
            "archive": _dimension("r2", "https://archive.example.invalid", "archive-bucket", "archive-profile"),
            "minio": _dimension("minio", "https://minio.example.invalid", "minio-bucket", "minio-profile"),
        },
    }
    (config_dir / "config.json").write_text(json.dumps(persisted))
    monkeypatch.setenv("JOSH_ROOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    _write_runtime({
        "accessKeyId": "temporary-id",
        "secretAccessKey": "temporary-secret",
        "sessionToken": "temporary-session",
        "endpoint": "https://oauth.example.invalid",
        "bucket": "oauth-bucket",
        "ageIdentity": "AGE-SECRET-KEY-1X",
        "ageRecipients": ["age1daily", "age1recovery"],
        "expiresIn": 600,
    }, dimension_id="archive")

    runtime = json.loads(Path(os.environ["JOSH_ROOM_RUNTIME_CONFIG"]).read_text())
    assert runtime["default_dimension"] == "minio"
    assert runtime["dimensions"]["minio"] == persisted["dimensions"]["minio"]
    assert runtime["dimensions"]["archive"]["provider"] == "r2"
    assert runtime["dimensions"]["archive"]["endpoint"] == "https://oauth.example.invalid"
    assert "temporary-secret" not in json.dumps(runtime)
    assert "AGE-SECRET" not in json.dumps(runtime)
    for name in ("JOSH_ROOM_RUNTIME_CREDENTIALS", "JOSH_ROOM_RUNTIME_CONFIG", "JOSH_ROOM_IDENTITY"):
        os.environ.pop(name, None)


def test_named_minio_default_resolves_before_backend_and_does_not_request_oauth(monkeypatch, tmp_path):
    config = {"default_dimension": "minio", "dimensions": {"minio": _dimension("minio", "https://minio.example.invalid", "bucket", "profile")}}
    monkeypatch.setattr(cli, "private_config", lambda: config)
    args = cli.build_parser().parse_args(["projects", "list"])
    assert cli._requires_oauth(args) is False
    captured = {}

    def fake_backend(name, instance, dimension=None):
        captured.update(name=name, dimension=dimension)
        return object()

    monkeypatch.setattr(cli, "_backend", fake_backend)
    cli._backend_for_args(args, tmp_path)
    assert captured == {"name": "minio", "dimension": "minio"}


def test_doctor_dimension_minio_inspects_named_minio_backend(monkeypatch, tmp_path):
    config = {"default_dimension": "r2", "dimensions": {"minio": _dimension("minio", "https://minio.example.invalid", "bucket", "profile")}}
    monkeypatch.setattr(cli, "private_config", lambda: config)
    calls = []

    class Backend:
        config = SimpleNamespace(dimension_id="minio")

        def read_catalog(self):
            calls.append("read_catalog")
            return None, None

    monkeypatch.setattr(cli, "_backend", lambda name, instance, dimension=None: (calls.append((name, dimension)) or Backend()))
    result = cli._doctor(tmp_path, "r2", "terminal", dimension="minio")
    assert result["selected_backend"] == "minio"
    assert ("minio", "minio") in calls


def test_doctor_explicit_minio_uses_minio_check_label_and_provider(monkeypatch, tmp_path):
    config = {"dimensions": {"minio": _dimension("minio", "https://minio.example.invalid", "bucket", "profile")}}
    monkeypatch.setattr(cli, "private_config", lambda: config)
    calls = []

    class Backend:
        config = SimpleNamespace(dimension_id="minio")

        def read_catalog(self):
            calls.append("read_catalog")
            return None, None

    def fake_backend(name, instance, dimension=None):
        calls.append((name, dimension))
        return Backend()

    monkeypatch.setattr(cli, "_backend", fake_backend)
    result = cli._doctor(tmp_path, "r2", "terminal", dimension="minio")
    check_names = {check["name"] for check in result["checks"]}
    assert "minio" in check_names
    assert "r2" not in check_names
    assert ("minio", "minio") in calls


def test_link_remote_verification_uses_bounded_streaming_contract(monkeypatch, tmp_path):
    snapshot = _snapshot()
    catalog = Catalog.empty("minio").add_snapshot("room", "Room", snapshot)
    calls = []
    monkeypatch.setattr(cli, "private_config", lambda: {"dimensions": {"minio": _dimension("minio", "https://minio.example.invalid", "bucket", "profile")}})

    class Backend:
        config = SimpleNamespace(dimension_id="minio")

        def verify_object(self, key, digest, size):
            calls.append((key, digest, size))

        def get_bytes(self, *_args):
            raise AssertionError("Link/Repair must not buffer the remote object")

    monkeypatch.setattr(cli, "_backend", lambda *_args: Backend())
    monkeypatch.setattr(cli, "load_catalog", lambda *_args: catalog)
    monkeypatch.setattr(cli, "link_workspace", lambda *args, **kwargs: {"ok": True})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args = cli.build_parser().parse_args([
        "link", "--workspace", str(workspace), "--dimension", "minio", "--project", "room", "--snapshot", "jat-1",
    ])
    assert cli.dispatch(args, tmp_path / "instance")["ok"] is True
    assert calls and calls[0][2] == len(b"ciphertext")


def test_local_verification_streams_without_read_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"large enough to hash")
    store = ImmutableLocalStore(tmp_path / "store")
    ref = store.put_file(source)
    monkeypatch.setattr(Path, "read_bytes", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("read_bytes is forbidden")))
    assert store.verify(ref.key, ref.sha256, ref.size) == ref


def test_v1_cross_room_copy_preserves_source_origin_provenance(tmp_path):
    source = _snapshot()
    source_catalog = Catalog({
        "format_version": 1,
        "revision": 1,
        "projects": {"source-room": {"display_name": "Source", "latest": "jat-1", "snapshots": {"jat-1": source}}},
    })
    destination_catalog = Catalog.empty("backup")
    calls = []

    class Store:
        def download_file(self, _key, destination, _digest, _size):
            destination.write_bytes(b"ciphertext")

        def put_file(self, key, _path):
            calls.append("put")
            return ObjectRef(key, source["ciphertext_sha256"], source["ciphertext_size"])

    result = copy_snapshot_stream(
        tmp_path / "instance", source_catalog, destination_catalog, Store(), Store(),
        "source-room", "restored-room", "latest", ["age1daily", "age1recovery"],
    )
    copied = result["catalog"].resolve_snapshot("restored-room", result["snapshot_id"])
    assert calls == ["put"]
    assert copied["origin_project_id"] == "source-room"


def test_save_local_transition_precedes_catalog_publication(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("Room")
    events = []

    class Backend:
        config = SimpleNamespace(dimension_id="archive")

        def put_file(self, _key, _path):
            events.append("object")
            return ObjectRef(_key, hashlib.sha256(b"ciphertext").hexdigest(), len(b"ciphertext"))

        def conditional_catalog_put(self, _body, _etag):
            events.append("catalog")

        def record_orphan(self, _ref):
            events.append("orphan")

    def fake_build(_jat_root, _source, output, **_kwargs):
        output.write_bytes(b"haul")
        return {"version": "test"}

    monkeypatch.setattr("josh_room.operations.run_build", fake_build)
    monkeypatch.setattr("josh_room.operations.build_envelope_file", lambda _manifest, _haul, envelope: envelope.write_bytes(b"envelope"))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda _source, _recipients, encrypted: encrypted.write_bytes(b"ciphertext"))
    monkeypatch.setattr("josh_room.operations.workspace_fingerprint", lambda _path: "a" * 64)
    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty("archive"), None))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    monkeypatch.setattr("josh_room.operations.write_workspace_marker", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local transition failed")))
    with pytest.raises(RuntimeError, match="local transition failed"):
        create_snapshot(tmp_path / "instance", "room", source, tmp_path / "jat", ["age1daily", "age1recovery"], Backend())
    assert "catalog" not in events


def test_save_marker_snapshot_failure_after_upload_records_orphan_before_catalog(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    marker_path = source / ".josh-room.json"
    marker_path.write_text('{"format_version": 1, "project_id": "old-room", "display_name": "Old"}\n')
    events = []
    digest = hashlib.sha256(b"ciphertext").hexdigest()

    class Backend:
        config = SimpleNamespace(dimension_id="archive")

        def put_file(self, key, _path):
            events.append("object")
            return ObjectRef(key, digest, len(b"ciphertext"))

        def conditional_catalog_put(self, _body, _etag):
            events.append("catalog")

        def record_orphan(self, _ref):
            events.append("orphan")

    def fake_build(_jat_root, _source, output, **_kwargs):
        output.write_bytes(b"haul")
        return {"version": "test"}

    monkeypatch.setattr("josh_room.operations.run_build", fake_build)
    monkeypatch.setattr("josh_room.operations.build_envelope_file", lambda _manifest, _haul, envelope: envelope.write_bytes(b"envelope"))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda _source, _recipients, encrypted: encrypted.write_bytes(b"ciphertext"))
    monkeypatch.setattr("josh_room.operations.workspace_fingerprint", lambda _path: "a" * 64)
    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty("archive"), None))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    original_open = Path.open

    def fail_marker_read(path, mode="r", *args, **kwargs):
        if path == marker_path and mode == "rb":
            raise OSError("marker snapshot failed")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_marker_read)
    with pytest.raises(OSError, match="marker snapshot failed"):
        create_snapshot(tmp_path / "instance", "room", source, tmp_path / "jat", ["age1daily", "age1recovery"], Backend())
    assert events == ["object", "orphan"]


def test_save_post_publication_verification_failure_keeps_committed_marker_honest(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    workspace_state.write_workspace_marker(
        source,
        dimension_id="archive",
        project_id="old-room",
        display_name="Old Room",
        snapshot_id="old-snapshot",
        workspace_fingerprint="a" * 64,
    )
    digest = hashlib.sha256(b"ciphertext").hexdigest()

    class PublishedVerificationError(RuntimeError):
        published = True

    class Backend:
        config = SimpleNamespace(dimension_id="archive")

        def put_file(self, key, _path):
            return ObjectRef(key, digest, len(b"ciphertext"))

        def conditional_catalog_put(self, _body, _etag):
            raise PublishedVerificationError("catalog read-back verification failed")

        def record_orphan(self, _ref):
            raise AssertionError("published catalog already references the object")

    def fake_build(_jat_root, _source, output, **_kwargs):
        output.write_bytes(b"haul")
        return {"version": "test"}

    monkeypatch.setattr("josh_room.operations.run_build", fake_build)
    monkeypatch.setattr("josh_room.operations.build_envelope_file", lambda _manifest, _haul, envelope: envelope.write_bytes(b"envelope"))
    monkeypatch.setattr("josh_room.operations.encrypt_file", lambda _source, _recipients, encrypted: encrypted.write_bytes(b"ciphertext"))
    monkeypatch.setattr("josh_room.operations.workspace_fingerprint", lambda _path: "a" * 64)
    monkeypatch.setattr("josh_room.operations._read_remote_catalog", lambda *_args, **_kwargs: (Catalog.empty("archive"), None))
    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"catalog")
    with pytest.raises(RuntimeError, match="catalog read-back verification failed") as failure:
        create_snapshot(tmp_path / "instance", "room", source, tmp_path / "jat", ["age1daily", "age1recovery"], Backend())
    marker = workspace_state.read_workspace_marker(source)
    assert marker["project_id"] == "room"
    assert marker["snapshot_id"] != "old-snapshot"
    assert failure.value.result == {
        "ok": False,
        "publication_state": "published_verification_unknown",
        "marker_state": "committed",
        "marker": str(source / ".josh-room.json"),
    }


def test_python_fingerprint_matches_native_noise_exclusions(tmp_path):
    native = Path("vscode-extension/dirty.js").read_text()
    excluded = (".josh-room.json", ".DS_Store", ".git", ".pytest_cache", ".ruff_cache", ".venv", "venv", "node_modules", "__pycache__")
    assert all(literal in native for literal in excluded)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("tracked")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    baseline = workspace_state.workspace_fingerprint(root)
    for relative in (".DS_Store", ".pytest_cache/cache", ".ruff_cache/cache", ".venv/lib", "venv/lib", "node_modules/pkg", "pkg/__pycache__/x.pyc"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("noise")
    assert workspace_state.workspace_fingerprint(root) == baseline


def test_python_fingerprint_excludes_exact_and_nested_pycache_directories(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "tracked.txt").write_text("tracked")
    (root / "package").mkdir()
    baseline = workspace_state.workspace_fingerprint(root)
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "nested").mkdir()
    (root / "__pycache__" / "nested" / "compiled.pyc").write_bytes(b"noise")
    (root / "package" / "__pycache__").mkdir(parents=True)
    (root / "package" / "__pycache__" / "compiled.pyc").write_bytes(b"noise")
    assert workspace_state.workspace_fingerprint(root) == baseline


def test_many_entry_fingerprint_does_not_materialize_global_rglob_list(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(5000):
        (root / f"entry-{index:05d}").write_text(str(index))

    def forbidden_rglob(_self, _pattern):
        raise AssertionError("fingerprint must traverse bounded directory batches")

    monkeypatch.setattr(Path, "rglob", forbidden_rglob)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = workspace_state.workspace_fingerprint(root)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert len(result) == 64
    assert after - before < 32 * 1024


def test_cross_dimension_copy_failure_writes_scrubbed_destination_receipt(tmp_path, monkeypatch):
    source = _snapshot(origin="source-room")
    source_catalog = Catalog.empty("archive").add_snapshot("source-room", "Source", source)
    destination_catalog = Catalog.empty("backup")
    payload = b"ciphertext"

    class SourceStore:
        def download_file(self, _key, destination, _digest, _size):
            destination.write_bytes(payload)

    class DestinationStore:
        config = SimpleNamespace(dimension_id="backup", provider="r2", bucket="backup-bucket", endpoint="https://backup.example.invalid")

        def put_file(self, key, _path):
            return ObjectRef(key, source["ciphertext_sha256"], source["ciphertext_size"])

        def conditional_catalog_put(self, _body, _etag):
            raise RuntimeError("destination catalog conflict")

    monkeypatch.setattr("josh_room.operations._encrypt_catalog", lambda *_args: b"encrypted-catalog")
    with pytest.raises(RuntimeError, match="orphan receipt") as failure:
        copy_snapshot_stream(
            tmp_path / "instance", source_catalog, destination_catalog, SourceStore(), DestinationStore(),
            "source-room", "restored-room", "latest", ["age1daily", "age1recovery"],
        )
    receipt_path = Path(failure.value.result["orphan_receipt"])
    receipt = json.loads(receipt_path.read_text())
    assert receipt["destination"] == {
        "dimension_id": "backup",
        "provider": "r2",
        "bucket": "backup-bucket",
        "endpoint": "https://backup.example.invalid",
    }
    assert receipt["object_key"] == source["object_key"]
    assert "credential" not in json.dumps(receipt).lower()
    assert "secret" not in json.dumps(receipt).lower()
