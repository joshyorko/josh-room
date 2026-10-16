"""Verify official stage bytes and equality with an actually UI-tested package.

Run from the exact reviewed repository checkout. Uses only the standard library
and the repository's guarded staging verifier. Never publishes or installs.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def vsix_payload(stream):
    with zipfile.ZipFile(stream) as archive:
        names = [name for name in archive.namelist() if not name.endswith('/')]
        assert len(names) == len(set(names)) == 105
        return {name: archive.read(name) for name in names}


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True)
parser.add_argument('--stage-run', type=int, required=True)
parser.add_argument('--artifact-id', type=int, required=True)
parser.add_argument('--zip', type=Path, required=True)
parser.add_argument('--artifact-json', type=Path, required=True)
parser.add_argument('--run-json', type=Path, required=True)
parser.add_argument('--ui-vsix', type=Path, required=True)
parser.add_argument('--ui-sha256', required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == args.source
artifact = json.loads(args.artifact_json.read_text())
assert artifact['id'] == args.artifact_id and not artifact['expired']
assert artifact['workflow_run']['head_sha'] == args.source
assert artifact['workflow_run']['id'] == args.stage_run
zip_bytes = args.zip.read_bytes()
assert 'sha256:' + sha256(zip_bytes) == artifact['digest']
assert len(zip_bytes) == artifact['size_in_bytes']
ui_bytes = args.ui_vsix.read_bytes()
assert sha256(ui_bytes) == args.ui_sha256
with tempfile.TemporaryDirectory(prefix='room-guarded-stage-review-') as temporary:
    directory = Path(temporary)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)) == 7
        assert all('/' not in name and name not in ('.', '..') for name in names)
        receipt = json.loads(archive.read('release-candidate.json'))
        assert receipt['source_sha'] == args.source
        assert set(names) == set(receipt['assets']) | {'release-candidate.json'}
        for name, identity in receipt['assets'].items():
            content = archive.read(name)
            assert sha256(content) == identity['sha256'] and len(content) == identity['size']
        archive.extractall(directory)
    command = [sys.executable, '-m', 'scripts.release_staging', 'verify',
               '--directory', str(directory), '--source-sha', args.source,
               '--release-tag', 'v0.1.36-standalone-vsix', '--version', '0.1.36',
               '--checks', str(directory / 'ci-readiness.json'),
               '--runtime', 'vscode-extension/runtime/manifest.json',
               '--release-lock', 'release-lock.json',
               '--stage-run-json', str(args.run_json), '--stage-run-id', str(args.stage_run),
               '--stage-workflow-id', '341135067']
    checked = subprocess.run(command, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    stage_vsix = directory / 'josh-room-0.1.36.vsix'
    actual = vsix_payload(stage_vsix)
    assert actual == vsix_payload(io.BytesIO(ui_bytes))
    canonical = Path('src/josh_room/config.py').read_bytes()
    assert actual['extension/runtime/controller/josh_room/config.py'] == canonical
    manifest = json.loads(actual['extension/runtime/manifest.json'])
    assert manifest['jat']['git_sha'] == 'fe43065a654d1649ba9ebe5a6f202eac612e4385'
    assert manifest['jat']['release_tag'] == 'v0.1.14-jat-runtime'
    with zipfile.ZipFile(directory / 'josh_room-0.1.0.dev0-py3-none-any.whl') as wheel:
        assert wheel.read('josh_room/config.py') == canonical
    with tarfile.open(directory / 'josh_room-0.1.0.dev0.tar.gz') as sdist:
        entries = [name for name in sdist.getnames() if name.endswith('/src/josh_room/config.py')]
        assert len(entries) == 1 and sdist.extractfile(entries[0]).read() == canonical
report = {
    'status': 'PASS', 'source_sha': args.source, 'stage_run_id': args.stage_run,
    'artifact_id': args.artifact_id, 'zip_sha256': sha256(zip_bytes), 'zip_size': len(zip_bytes),
    'all_six_asset_hashes_and_sizes_verified': True,
    'canonical_release_staging_verifier': 'PASS',
    'ui_tested_vsix_sha256': args.ui_sha256,
    'stage_vsix': receipt['assets']['josh-room-0.1.36.vsix'],
    'all_105_uncompressed_vsix_files_equal_ui_tested_package': True,
    'wheel_and_sdist_config_equal_canonical': True,
    'jat14_source_and_release_pins_verified': True,
    'scope': 'Official guarded stage integrity and tested executable payload equality; not new GUI or live provider execution',
    'publication': 'NOT_PERFORMED',
}
args.report.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
