import hashlib
import json
import signal
from types import SimpleNamespace

import pytest

from josh_room import cli, operations
from josh_room.cancellation import CLICancelled, sigterm_cancellation
from josh_room.catalog import Catalog
from josh_room.local_store import ObjectRef
from josh_room.operations import SavePublicationError, create_snapshot


def _prepare_snapshot(monkeypatch, source):
    digest = hashlib.sha256(b"ciphertext").hexdigest()

    def fake_build(_jat_root, _source, output, **_kwargs):
        output.write_bytes(b"haul")
        return {"version": "test"}

    monkeypatch.setattr("josh_room.operations.run_build", fake_build)
    monkeypatch.setattr(
        "josh_room.operations.build_envelope_file",
        lambda _manifest, _haul, envelope: envelope.write_bytes(b"envelope"),
    )
    monkeypatch.setattr(
        "josh_room.operations.encrypt_file",
        lambda _source, _recipients, encrypted: encrypted.write_bytes(b"ciphertext"),
    )
    monkeypatch.setattr("josh_room.operations.workspace_fingerprint", lambda _path: "a" * 64)
    monkeypatch.setattr("josh_room.operations._source_metadata", lambda *_args: {})
    monkeypatch.setattr("josh_room.operations._snapshot_id", lambda: "snapshot-new")
    monkeypatch.setattr(
        "josh_room.operations._read_remote_catalog",
        lambda *_args, **_kwargs: (Catalog.empty("archive"), None),
    )
    monkeypatch.setattr(
        "josh_room.operations._encrypt_catalog",
        lambda catalog, *_args: json.dumps(catalog.body, sort_keys=True).encode(),
    )
    return digest


class _Backend:
    config = SimpleNamespace(dimension_id="archive")

    def __init__(self, source, digest):
        self.source = source
        self.digest = digest
        self.events = []
        self.catalog = None

    def put_file(self, key, _encrypted):
        self.events.append("object")
        return ObjectRef(key, self.digest, len(b"ciphertext"))

    def conditional_catalog_put(self, body, _etag):
        self.events.append("catalog")
        assert (self.source / ".josh-room.json").read_bytes() == OLD_MARKER
        self.catalog = json.loads(body)

    def record_orphan(self, _ref):
        self.events.append("orphan")


OLD_MARKER = b'{"format_version": 1, "project_id": "old-room", "display_name": "Old"}\n'


def _snapshot(monkeypatch, tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    marker = source / ".josh-room.json"
    marker.write_bytes(OLD_MARKER)
    digest = _prepare_snapshot(monkeypatch, source)
    backend = _Backend(source, digest)
    return source, marker, backend


def _create(source, backend, tmp_path):
    return create_snapshot(
        tmp_path / "instance",
        "new-room",
        source,
        tmp_path / "jat",
        ["age1daily", "age1recovery"],
        backend,
    )


def test_marker_remains_old_until_catalog_commit_returns(monkeypatch, tmp_path):
    source, marker, backend = _snapshot(monkeypatch, tmp_path)
    result = _create(source, backend, tmp_path)

    assert backend.events == ["object", "catalog"]
    assert backend.catalog["projects"]["new-room"]["latest"] == result["snapshot_id"]
    assert json.loads(marker.read_text())["snapshot_id"] == result["snapshot_id"]


def test_sigterm_during_applied_catalog_commit_finalizes_marker_and_receipt(monkeypatch, tmp_path):
    source, marker, backend = _snapshot(monkeypatch, tmp_path)

    def apply_then_signal(body, _etag):
        backend.events.append("catalog")
        assert marker.read_bytes() == OLD_MARKER
        backend.catalog = json.loads(body)
        signal.raise_signal(signal.SIGTERM)

    backend.conditional_catalog_put = apply_then_signal
    with pytest.raises(CLICancelled) as cancelled, sigterm_cancellation():
        _create(source, backend, tmp_path)

    result = cancelled.value.result
    committed_id = backend.catalog["projects"]["new-room"]["latest"]
    assert committed_id == "snapshot-new"
    assert json.loads(marker.read_text())["snapshot_id"] == committed_id
    assert backend.events == ["object", "catalog"]
    assert result["publication_state"] == "committed"
    assert result["marker_state"] == "updated"
    assert result["snapshot_id"] == committed_id


def test_cli_sigterm_after_commit_keeps_committed_receipt_and_exits_cancelled(monkeypatch, tmp_path, capsys):
    from contextlib import nullcontext

    source, marker, backend = _snapshot(monkeypatch, tmp_path)

    def apply_then_signal(body, _etag):
        assert marker.read_bytes() == OLD_MARKER
        backend.events.append("catalog")
        backend.catalog = json.loads(body)
        signal.raise_signal(signal.SIGTERM)

    backend.conditional_catalog_put = apply_then_signal
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "_instance_root", lambda: tmp_path / "instance")
    monkeypatch.setattr(cli, "_uses_minio_encryption", lambda _args: False)
    monkeypatch.setattr(cli, "_identity_environment", nullcontext)
    monkeypatch.setattr(cli, "load_runtime_session", lambda: False)
    monkeypatch.setattr(cli, "_requires_oauth", lambda _args: False)
    monkeypatch.setattr(cli, "_requires_encryption", lambda _args: False)
    monkeypatch.setattr(cli, "_write_runtime_result", lambda _result: None)
    monkeypatch.setattr(cli, "dispatch", lambda *_args: _create(source, backend, tmp_path))

    assert cli.main(["projects", "list", "--json"]) == 130
    result = json.loads(capsys.readouterr().out)
    assert result["cancelled"] is True
    assert result["publication_state"] == "committed"
    assert result["marker_state"] == "updated"
    assert result["snapshot_id"] == "snapshot-new"
    assert json.loads(marker.read_text())["snapshot_id"] == "snapshot-new"
    assert backend.events == ["object", "catalog"]


def test_definite_catalog_rejection_keeps_old_marker_and_records_orphan(monkeypatch, tmp_path):
    source, marker, backend = _snapshot(monkeypatch, tmp_path)

    class Rejected(RuntimeError):
        published = False

    def reject(_body, _etag):
        backend.events.append("catalog")
        raise Rejected("conditional write rejected")

    backend.conditional_catalog_put = reject
    with pytest.raises(Rejected):
        _create(source, backend, tmp_path)

    assert marker.read_bytes() == OLD_MARKER
    assert backend.events == ["object", "catalog", "orphan"]


def test_ambiguous_catalog_transport_failure_keeps_old_marker_without_orphan(monkeypatch, tmp_path):
    source, marker, backend = _snapshot(monkeypatch, tmp_path)

    def ambiguous(_body, _etag):
        backend.events.append("catalog")
        raise OSError("connection lost after send")

    backend.conditional_catalog_put = ambiguous
    with pytest.raises(SavePublicationError) as failure:
        _create(source, backend, tmp_path)

    assert marker.read_bytes() == OLD_MARKER
    assert backend.events == ["object", "catalog"]
    assert failure.value.result["publication_state"] == "uncertain"
    assert failure.value.result["marker_state"] == "unchanged"
    assert failure.value.result["reconciliation_required"] is True


def test_marker_failure_after_commit_reports_committed_stale_marker(monkeypatch, tmp_path):
    source, marker, backend = _snapshot(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "josh_room.operations.write_workspace_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("marker write failed")),
    )

    with pytest.raises(SavePublicationError) as failure:
        _create(source, backend, tmp_path)

    assert marker.read_bytes() == OLD_MARKER
    assert backend.catalog["projects"]["new-room"]["latest"] == "snapshot-new"
    assert backend.events == ["object", "catalog"]
    assert failure.value.result["publication_state"] == "committed"
    assert failure.value.result["marker_state"] == "stale"
    assert failure.value.result["snapshot_id"] == "snapshot-new"


def test_local_catalog_atomic_replace_error_is_classified_from_readback(monkeypatch, tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    marker = source / ".josh-room.json"
    marker.write_bytes(OLD_MARKER)
    digest = _prepare_snapshot(monkeypatch, source)
    monkeypatch.setattr(
        operations.ImmutableLocalStore,
        "put_file",
        lambda _store, _path: ObjectRef(f"objects/sha256/{digest}", digest, len(b"ciphertext")),
    )
    visible_catalog = None
    read_count = 0

    def read(_catalog_file):
        nonlocal read_count
        read_count += 1
        return visible_catalog or Catalog.empty()

    def update(_catalog_file, _revision, catalog, _recipients):
        nonlocal visible_catalog
        visible_catalog = catalog
        raise OSError("local error after atomic replace")

    monkeypatch.setattr(operations.CatalogFile, "read", read)
    monkeypatch.setattr(operations.CatalogFile, "update_if_revision", update)

    with pytest.raises(SavePublicationError) as failure:
        create_snapshot(
            tmp_path / "instance",
            "new-room",
            source,
            tmp_path / "jat",
            ["age1daily", "age1recovery"],
        )

    assert read_count == 2
    assert visible_catalog.body["projects"]["new-room"]["latest"] == "snapshot-new"
    assert json.loads(marker.read_text())["project_id"] == "new-room"
    assert failure.value.result["publication_state"] == "committed"
    assert failure.value.result["marker_state"] == "updated"
