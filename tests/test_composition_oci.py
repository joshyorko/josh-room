"""Archive closure checks using a synthetic OCI image and real zstd bytes."""
import hashlib
import io
import json
import tarfile

import pytest
import zstandard

from josh_room.composition_oci import canonicalize_oci_archive, verify_oci_archive


def export(path, fault=None, bookkeeping=None):
    files = {}
    def blob(data, media_type):
        key = hashlib.sha256(data).hexdigest()
        files['blobs/sha256/' + key] = data
        return {'digest': 'sha256:' + key, 'size': len(data), 'mediaType': media_type}

    config = blob(b'{}', 'application/vnd.oci.image.config.v1+json')
    layer = blob(b'workspace artifact', 'application/vnd.oci.image.layer.v1.tar')
    manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'config': config, 'layers': [layer]}
    root = blob(json.dumps(manifest).encode(), manifest['mediaType'])
    files['index.json'] = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [root]}).encode()
    files['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
    if bookkeeping is not None:
        files['audit.log'] = bookkeeping
        files['store.json'] = bookkeeping
    layer_path = 'blobs/sha256/' + layer['digest'][7:]
    if fault == 'digest':
        files[layer_path] = b'replacement'
    if fault == 'missing':
        del files[layer_path]
    if fault == 'unreachable':
        blob(b'extra bytes', 'application/octet-stream')
    if fault == 'path':
        files['credentials.json'] = b'{}'
    with (path.open('wb') as raw, zstandard.ZstdCompressor().stream_writer(raw) as compressed,
          tarfile.open(fileobj=compressed, mode='w|') as archive):
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return [{'digest': root['digest'] if fault != 'wrong-root' else 'sha256:' + '0' * 64}]


@pytest.mark.parametrize('fault', ['digest', 'missing', 'unreachable', 'path', 'wrong-root', None])
def test_verified_exact_oci_graph(tmp_path, fault):
    archive = tmp_path / 'export'
    approved = export(archive, fault)
    if fault:
        with pytest.raises(ValueError):
            verify_oci_archive(archive, approved)
    else:
        verify_oci_archive(archive, approved)


def test_hauler_local_metadata_is_excluded_from_approved_transport(tmp_path):
    archives = [tmp_path / f'haul-{i}.tar.zst' for i in range(2)]
    for i, path in enumerate(archives):
        refs = export(path, bookkeeping=f'uncaptured trace {i}'.encode())
        with pytest.raises(ValueError, match='unexpected'):
            verify_oci_archive(path, refs)
        canonicalize_oci_archive(path, refs)
        verify_oci_archive(path, refs)
    assert archives[0].read_bytes() == archives[1].read_bytes()


def test_oci_digest_cannot_substitute_tag_or_platform(tmp_path):
    path = tmp_path / 'haul'
    refs = export(path)
    for field in ('reference_sha256', 'platform_sha256', 'media_type'):
        with pytest.raises(ValueError, match='tag, media type or platform'):
            verify_oci_archive(path, [{**refs[0], field: 'f' * 64}])
