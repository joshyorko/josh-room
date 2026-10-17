"""Reject stale or modified disposable build inputs before invoking Cargo."""
import json
from pathlib import Path

import pytest

from scripts import build_rustic_candidate as build


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    for name in ('source', *build.CRATES):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'compiled.rs').write_text('synthetic source')
    monkeypatch.setattr(build.subprocess, 'check_output', lambda *a, **k: build.SOURCE + '\n')
    (tmp_path / 'prepared-source.json').write_text(json.dumps(build.prepared_sources(tmp_path)))
    return tmp_path


def test_prepared_sources_accept_exact_inputs(prepared):
    build.verify_prepared_sources(prepared)


@pytest.mark.parametrize('change', ['modified', 'added', 'removed', 'symlink', 'head'])
def test_prepared_sources_reject_changes(prepared, monkeypatch, change):
    path = prepared / 'source/compiled.rs'
    if change == 'modified': path.write_text('different compiled source')
    elif change == 'added': (prepared / 'rustic_core-0.13.0/unrecorded.rs').write_text('extra source')
    elif change == 'removed': path.unlink()
    elif change == 'symlink':
        original = Path.is_symlink
        monkeypatch.setattr(Path, 'is_symlink', lambda value: value == path or original(value))
    else: monkeypatch.setattr(build.subprocess, 'check_output', lambda *a, **k: '0' * 40 + '\n')
    with pytest.raises(RuntimeError): build.verify_prepared_sources(prepared)
