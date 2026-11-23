"""Archive closure checks using a synthetic OCI image and real zstd bytes."""
import hashlib
import io
import json
import tarfile

import pytest
import zstandard

from josh_room.composition_oci import verify_oci_archive


def export(path, fault=None):
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
    files['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [root]}).encode()
    files['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
    layer_path = 'blobs/sha256/' + layer['digest'][7:]
    if fault == 'digest':
        files[layer_path] = b'replacement'
    if fault == 'missing':
        del files[layer_path]
    if fault == 'unreachable':
        blob(b'extra bytes', 'application/octet-stream')
    if fault == 'path':
        files['credentials.json'] = b'{}'
    with path.open('wb') as raw, zstandard.ZstdCompressor().stream_writer(raw) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as archive:
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
