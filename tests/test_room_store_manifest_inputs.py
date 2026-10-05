from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
import yaml

from josh_room import room_store_hauler_runner as runner
from josh_room.room_store_manifest_inputs import prepare_manifest_inputs


def test_parent_relative_values_are_frozen_and_change_source_identity(tmp_path):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    chart = tmp_path / "chart"
    chart.mkdir()
    values = chart / "values.yaml"
    values.write_text("replicas: 2\n")
    manifest = manifests / "charts.yaml"
    manifest.write_text("""apiVersion: content.hauler.cattle.io/v1
kind: Charts
spec:
  charts:
    - name: synthetic
      repoURL: https://charts.example.invalid
      version: 1.0.0
      valuesFiles: [../chart/values.yaml]
""")
    original = prepare_manifest_inputs([manifest])
    frozen = prepare_manifest_inputs([manifest], tmp_path / "owned")
    assert frozen["sha256"] == original["sha256"]
    document = yaml.safe_load(Path(frozen["manifests"][0]).read_text())
    owned_values = Path(document["spec"]["charts"][0]["valuesFiles"][0])
    owned_values.relative_to(tmp_path / "owned")
    assert owned_values.read_bytes() == values.read_bytes()
    values.write_text("replicas: 3\n")
    assert owned_values.read_text() == "replicas: 2\n"
    assert prepare_manifest_inputs([manifest])["sha256"] != original["sha256"]


def test_local_repository_and_file_inputs_are_frozen(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "chart-repo"
    repo.mkdir()
    (repo / "index.yaml").write_text("synthetic index")
    source = tmp_path / "payload.txt"
    source.write_text("synthetic file")
    manifest = tmp_path / "inputs.yaml"
    manifest.write_text("""kind: Charts
spec:
  charts: [{name: synthetic, repoURL: chart-repo}]
---
kind: Files
spec:
  files: [{path: payload.txt, name: synthetic}]
""")
    result = prepare_manifest_inputs([manifest], tmp_path / "owned")
    chart, file = list(yaml.safe_load_all(Path(result["manifests"][0]).read_text()))
    assert (Path(chart["spec"]["charts"][0]["repoURL"]) / "index.yaml").read_text() == "synthetic index"
    assert Path(file["spec"]["files"][0]["path"]).read_text() == "synthetic file"


def test_manifest_dependency_symlink_fails_closed(tmp_path):
    source = tmp_path / "file"
    source.write_text("synthetic")
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    manifest = tmp_path / "inputs.yaml"
    manifest.write_text(json.dumps({"kind": "Files", "spec": {"files": [{"path": str(alias)}]}}))
    with pytest.raises(RuntimeError, match="unsafe"):
        prepare_manifest_inputs([manifest], tmp_path / "owned")


def test_native_file_uri_is_frozen_and_bound_to_identity(tmp_path):
    source = tmp_path / "input with spaces.txt"
    source.write_text("first")
    manifest = tmp_path / "input.yaml"
    manifest.write_text(json.dumps({"kind": "Files", "spec": {"files": [{"path": source.as_uri()}]}}))
    original = prepare_manifest_inputs([manifest])
    frozen = prepare_manifest_inputs([manifest], tmp_path / "owned")
    assert frozen["sha256"] == original["sha256"]
    document = yaml.safe_load(Path(frozen["manifests"][0]).read_text())
    owned = Path(url2pathname(urlsplit(document["spec"]["files"][0]["path"]).path))
    owned.relative_to(tmp_path / "owned")
    source.write_text("other")
    assert owned.read_text() == "first"
    assert prepare_manifest_inputs([manifest])["sha256"] != original["sha256"]


def test_local_config_verification_rejects_retag_and_restore(tmp_path):
    blobs = tmp_path / "blobs" / "sha256"
    blobs.mkdir(parents=True)
    observed = "sha256:" + "a" * 64
    config = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
    captured = "sha256:" + hashlib.sha256(config).hexdigest()
    (blobs / captured[7:]).write_bytes(config)
    raw = json.dumps({"schemaVersion": 2, "config": {"digest": captured}}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (blobs / digest).write_bytes(raw)
    (tmp_path / "index.json").write_text(json.dumps({"manifests": [{
        "digest": "sha256:" + digest,
        "annotations": {"org.opencontainers.image.ref.name": "localhost/synthetic:latest"},
    }]}))
    runner._verify_local_image_configs(tmp_path, [["localhost/synthetic:latest", captured]])
    with pytest.raises(runner.ManagedHaulerError, match="selected identity"):
        runner._verify_local_image_configs(tmp_path, [["localhost/synthetic:latest", observed]])


def test_output_budget_rejects_before_native_save(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_HAUL_OUTPUT_BYTES", 64 * 1024 * 1024)
    store = tmp_path / "store"
    store.mkdir()
    (store / "data").write_text("synthetic")

    class Adapter:
        def save(self, *args, **kwargs):
            pytest.fail("native Save must not start")

    with pytest.raises(runner.ManagedHaulerError, match="archive budget"):
        runner._bounded_hauler_save(Adapter(), store, tmp_path, tmp_path / "output.zst")
