from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading

import pytest

from josh_room import room_store_docker_identity as identity


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _member(archive: tarfile.TarFile, name: str, data: bytes, *, kind=tarfile.REGTYPE) -> None:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind in {tarfile.REGTYPE, tarfile.AREGTYPE} else 0
    archive.addfile(info, io.BytesIO(data) if info.size else None)


def _config(marker: str = "synthetic") -> bytes:
    return json.dumps({
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "1" * 64]},
        "history": [{"comment": marker}],
        "config": {},
    }, separators=(",", ":")).encode()


def _docker_save(configs: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        manifest = [{"Config": name, "RepoTags": [f"localhost/{name.removesuffix('.json')}:latest"], "Layers": []}
                    for name, _raw in configs]
        _member(archive, "manifest.json", json.dumps(manifest, separators=(",", ":")).encode())
        for name, raw in configs:
            _member(archive, name, raw)
    return stream.getvalue()


def _oci_save(configs: list[bytes]) -> tuple[bytes, list[str]]:
    stream = io.BytesIO()
    config_digests = [_sha256(raw) for raw in configs]
    layer_raw = b"\x1f\x8b" + b"synthetic-compressed-layer" * 16
    layer_digest = _sha256(layer_raw)
    manifest_raws = []
    for raw, config_digest in zip(configs, config_digests, strict=True):
        manifest_raws.append(json.dumps({
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(raw)},
            "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": layer_digest, "size": len(layer_raw)}],
        }, separators=(",", ":")).encode())
    manifest_digests = [_sha256(raw) for raw in manifest_raws]
    index_raw = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"mediaType": "application/vnd.oci.image.manifest.v1+json",
             "digest": digest, "size": len(raw),
             "platform": {"architecture": "amd64", "os": "linux"}}
            for digest, raw in zip(manifest_digests, manifest_raws, strict=True)
        ],
    }, separators=(",", ":")).encode()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _member(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        _member(archive, "manifest.json", json.dumps([
            {"Config": "blobs/sha256/" + digest[7:],
             "RepoTags": [f"localhost/synthetic-{index}:latest"], "Layers": ["layer.tar"]}
            for index, digest in enumerate(config_digests)
        ], separators=(",", ":")).encode())
        _member(archive, "index.json", index_raw)
        for raw, digest in zip(configs, config_digests, strict=True):
            _member(archive, "blobs/sha256/" + digest[7:], raw)
        _member(archive, "layer.tar", layer_raw)
        _member(archive, "blobs/sha256/" + layer_digest[7:], layer_raw)
        for raw, digest in zip(manifest_raws, manifest_digests, strict=True):
            _member(archive, "blobs/sha256/" + digest[7:], raw)
    return stream.getvalue(), manifest_digests


def test_legacy_docker_id_is_the_exact_saved_config_digest():
    raw = _config()
    digest = _sha256(raw)
    assert identity.saved_image_config_digest(io.BytesIO(_docker_save([(digest[7:] + ".json", raw)])), digest) == digest


@pytest.mark.parametrize("metadata_index", [False, True])
def test_modern_docker_id_resolves_from_saved_oci_metadata(metadata_index):
    raw = _config()
    archive, manifest_digests = _oci_save([raw])
    if metadata_index:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as saved:
            index_raw = saved.extractfile("index.json").read()
        image_id = _sha256(index_raw)
    else:
        image_id = manifest_digests[0]
    assert image_id != _sha256(raw)
    assert identity.saved_image_config_digest(io.BytesIO(archive), image_id) == _sha256(raw)


def test_legacy_config_id_resolves_through_oci_index():
    raw = _config()
    archive, _ = _oci_save([raw])
    assert identity.saved_image_config_digest(io.BytesIO(archive), _sha256(raw)) == _sha256(raw)


def test_compressed_blobs_skip_json_complexity_scan(monkeypatch):
    raw = _config()
    archive, _ = _oci_save([raw])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as saved:
        index_id = _sha256(saved.extractfile("index.json").read())
    scan = identity._json_complexity

    def complexity_probe(value):
        if value.startswith(b"\x1f\x8b"):
            pytest.fail("compressed layer was scanned as JSON")
        return scan(value)

    monkeypatch.setattr(identity, "_json_complexity", complexity_probe)
    assert identity.saved_image_config_digest(io.BytesIO(archive), index_id) == _sha256(raw)


def test_oci_index_may_omit_optional_media_type():
    raw = _config()
    archive, _ = _oci_save([raw])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as saved:
        for member in saved:
            files[member.name] = saved.extractfile(member).read() if member.isfile() else None
    index = json.loads(files["index.json"])
    del index["mediaType"]
    files["index.json"] = json.dumps(index, separators=(",", ":")).encode()
    rewritten = io.BytesIO()
    with tarfile.open(fileobj=rewritten, mode="w", format=tarfile.USTAR_FORMAT) as saved:
        for name, data in files.items():
            if data is not None:
                _member(saved, name, data)
    index_id = _sha256(files["index.json"])
    assert identity.saved_image_config_digest(io.BytesIO(rewritten.getvalue()), index_id) == _sha256(raw)


def test_saved_config_parser_accepts_a_nonseekable_stream():
    raw = _config()
    digest = _sha256(raw)
    archive = _docker_save([(digest[7:] + ".json", raw)])

    class NonSeekable:
        def __init__(self, body):
            self.body = io.BytesIO(body)

        def read(self, size):
            return self.body.read(size)

    assert identity.saved_image_config_digest(NonSeekable(archive), digest) == digest


def test_oci_descriptor_media_type_must_match_hashed_blob():
    raw = _config()
    config_digest = _sha256(raw)
    manifest = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": config_digest, "size": len(raw)},
        "layers": [],
    }, separators=(",", ":")).encode()
    manifest_digest = _sha256(manifest)
    index = json.dumps({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{"mediaType": "application/vnd.oci.image.index.v1+json",
                       "digest": manifest_digest, "size": len(manifest)}],
    }, separators=(",", ":")).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _member(archive, "index.json", index)
        _member(archive, "blobs/sha256/" + config_digest[7:], raw)
        _member(archive, "blobs/sha256/" + manifest_digest[7:], manifest)
    with pytest.raises(identity.DockerImageIdentityError, match="content does not match"):
        identity.saved_image_config_digest(io.BytesIO(stream.getvalue()), _sha256(index))


def test_unrelated_id_and_tampered_config_blob_fail_closed():
    raw = _config()
    archive, _ = _oci_save([raw])
    with pytest.raises(identity.DockerImageIdentityError):
        identity.saved_image_config_digest(io.BytesIO(archive), "sha256:" + "f" * 64)

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as saved:
        digest = _sha256(raw)
        bad = raw + b" "
        manifest = json.dumps({"schemaVersion": 2, "config": {"digest": digest, "size": len(raw)}, "layers": []}).encode()
        _member(saved, "index.json", json.dumps({"schemaVersion": 2, "manifests": [{"digest": _sha256(manifest), "size": len(manifest)}]}).encode())
        _member(saved, "blobs/sha256/" + _sha256(manifest)[7:], manifest)
        _member(saved, "blobs/sha256/" + digest[7:], bad)
    with pytest.raises(identity.DockerImageIdentityError):
        identity.saved_image_config_digest(io.BytesIO(stream.getvalue()), _sha256(manifest))


def test_multi_config_index_is_ambiguous():
    archive, _ = _oci_save([_config("first"), _config("second")])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as saved:
        index_id = _sha256(saved.extractfile("index.json").read())
    with pytest.raises(identity.DockerImageIdentityError, match="ambiguous"):
        identity.saved_image_config_digest(io.BytesIO(archive), index_id)


@pytest.mark.parametrize("unsafe_name,kind", [
    ("../outside.json", tarfile.REGTYPE),
    ("/absolute.json", tarfile.REGTYPE),
    ("link", tarfile.SYMTYPE),
])
def test_unsafe_tar_members_fail_closed(unsafe_name, kind):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _member(archive, unsafe_name, b"{}", kind=kind)
    with pytest.raises(identity.DockerImageIdentityError):
        identity.saved_image_config_digest(io.BytesIO(stream.getvalue()), "sha256:" + "0" * 64)


def test_duplicate_manifest_json_keys_fail_closed():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _member(archive, "manifest.json", b'[{"Config":"one.json","Config":"two.json","Layers":[]}]')
    with pytest.raises(identity.DockerImageIdentityError):
        identity.saved_image_config_digest(io.BytesIO(stream.getvalue()), "sha256:" + "0" * 64)


def test_json_metadata_token_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(identity, "MAX_JSON_TOKENS", 4)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        _member(archive, "manifest.json", b"[0,0,0,0,0]")
    with pytest.raises(identity.DockerImageIdentityError, match="metadata is invalid"):
        identity.saved_image_config_digest(io.BytesIO(stream.getvalue()), "sha256:" + "0" * 64)


@pytest.mark.parametrize("limit_name", [
    "MAX_ARCHIVE_BYTES", "MAX_MEMBER_COUNT", "MAX_CONFIG_BYTES", "MAX_METADATA_BYTES",
])
def test_archive_limits_are_enforced(monkeypatch, limit_name):
    raw = _config()
    digest = _sha256(raw)
    archive = _docker_save([(digest[7:] + ".json", raw)])
    monkeypatch.setattr(identity, limit_name, 1)
    with pytest.raises(identity.DockerImageIdentityError):
        identity.saved_image_config_digest(io.BytesIO(archive), digest)


def test_docker_export_deduplicates_immutable_ids_and_preserves_output_names(monkeypatch):
    raw = _config()
    config_id = _sha256(raw)
    saved = _docker_save([(config_id[7:] + ".json", raw)])
    calls = []

    class Process:
        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.stdout = io.BytesIO(saved)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr(identity.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(identity.subprocess, "Popen", Process)
    result = identity.docker_image_config_digests([
        ("localhost/a:one", config_id),
        ("localhost/a:two", config_id),
    ], timeout=1)
    assert result == [("localhost/a:one", config_id), ("localhost/a:two", config_id)]
    assert len(calls) == 1
    assert calls[0][0][-1] == config_id
    assert "localhost/a:one" not in calls[0][0]
    assert "localhost/a:two" not in calls[0][0]
    assert calls[0][1]["stderr"] == identity.subprocess.DEVNULL


def test_docker_export_timeout_stops_and_reaps_child(monkeypatch):
    stopped = threading.Event()
    processes = []

    class BlockingStream:
        def read(self, _size):
            stopped.wait(2)
            return b""

        def close(self):
            stopped.set()

    class Process:
        def __init__(self, _command, **_kwargs):
            self.stdout = BlockingStream()
            self.returncode = None
            self.killed = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            stopped.set()

        def wait(self, timeout=None):
            if self.returncode is None:
                raise identity.subprocess.TimeoutExpired("docker image save", timeout)
            return self.returncode

    monkeypatch.setattr(identity.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(identity.subprocess, "Popen", Process)
    monkeypatch.setattr("josh_room.cancellation.terminate_owned_process", lambda process, **_kwargs: (process.kill(), process.wait()))
    with pytest.raises(identity.DockerImageIdentityError, match="timed out"):
        identity.docker_image_config_digests([("localhost/synthetic:latest", "sha256:" + "a" * 64)], timeout=0.01)
    assert processes[0].killed is True
    assert processes[0].returncode == -9


def test_docker_export_archive_budget_is_cumulative(monkeypatch):
    raw_a, raw_b = _config("first"), _config("second")
    id_a, id_b = _sha256(raw_a), _sha256(raw_b)
    archive_a = _docker_save([(id_a[7:] + ".json", raw_a)])
    archive_b = _docker_save([(id_b[7:] + ".json", raw_b)])
    assert len(archive_a) < 15_000 and len(archive_b) < 15_000
    monkeypatch.setattr(identity, "MAX_ARCHIVE_BYTES", 15_000)

    class Process:
        def __init__(self, command, **_kwargs):
            self.stdout = io.BytesIO({id_a: archive_a, id_b: archive_b}[command[-1]])
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def poll(self):
            return self.returncode

    monkeypatch.setattr(identity.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(identity.subprocess, "Popen", Process)
    with pytest.raises(identity.DockerImageIdentityError, match="size limit"):
        identity.docker_image_config_digests([
            ("localhost/a:latest", id_a),
            ("localhost/b:latest", id_b),
        ], timeout=1)
