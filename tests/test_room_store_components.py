from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import room_store_components as components

REPOSITORY_ID = "a" * 64
SNAPSHOT_ID = "b" * 64
TREE_ID = "c" * 64


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "robot.yaml").write_text("condaConfigFile: conda.yaml\n", encoding="utf-8")
    (tmp_path / "conda.yaml").write_text("channels: [conda-forge]\ndependencies: [python=3.12]\n", encoding="utf-8")
    return tmp_path


class _Restic:
    def __init__(self):
        self.restored = []
        self.backups = []

    def restore(self, snapshot_id, destination):
        self.restored.append((snapshot_id, Path(destination)))

    def backup(self, source, *, parent=None, cancellation=None):
        source = Path(source)
        self.backups.append((source, parent, sorted(path.name for path in source.iterdir())))
        return SimpleNamespace(snapshot_id="d" * 64)

    def snapshot(self, snapshot_id):
        return SimpleNamespace(tree_id=TREE_ID)


def _prior(source_input_sha256: str) -> dict:
    return {
        "kind": "rcca",
        "snapshot": {
            "repository_id": REPOSITORY_ID,
            "repository_format": 2,
            "snapshot_id": SNAPSHOT_ID,
            "tree_id": TREE_ID,
        },
        "archive_sha256": "e" * 64,
        "archive_size": 4,
        "member_basename": "rcc-environment.rcca",
        "artifact_digest": "sha256:" + "1" * 64,
        "specification_digest": "sha256:" + "2" * 64,
        "platform": "linux-x64",
        "rcc_version": components.RCC_VERSION,
        "robot_relative_path": "robot.yaml",
        "source_input_sha256": source_input_sha256,
    }


def test_source_digest_tracks_rcc_file_closure_and_does_not_expose_paths(tmp_path):
    root = _workspace(tmp_path)
    original = components.source_input_sha256(root)
    (root / "conda.yaml").write_text("channels: [conda-forge]\ndependencies: [python=3.13]\n", encoding="utf-8")
    changed = components.source_input_sha256(root)
    assert original != changed
    assert str(root) not in changed


def test_source_digest_rejects_symlink_dependency(tmp_path):
    root = _workspace(tmp_path)
    (root / "conda.yaml").unlink()
    target = tmp_path.parent / "outside-conda.yaml"
    target.write_text("dependencies: []\n", encoding="utf-8")
    (root / "conda.yaml").symlink_to(target)
    with pytest.raises(components.RoomStoreComponentError, match="unsafe"):
        components.source_input_sha256(root)


def test_unchanged_inputs_reuse_exact_prior_component_without_rcc_or_backup(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    digest = components.source_input_sha256(root)
    restic = _Restic()
    monkeypatch.setattr(components, "_run", lambda *args, **kwargs: pytest.fail("RCC must not run for unchanged inputs"))
    prior = _prior(digest)
    result = components.capture_rcc_component(
        workspace=root,
        prior_component=prior,
        repository_id=REPOSITORY_ID,
        repository_format=2,
        restic=restic,
        rcc_runtime="/synthetic/rcc",
    )
    assert result == prior
    assert restic.restored == []
    assert restic.backups == []


@pytest.mark.parametrize("publish_platform", ["linux-x64", None])
def test_changed_inputs_publish_export_and_backup_only_fixed_stage(tmp_path, monkeypatch, publish_platform):
    root = _workspace(tmp_path)
    digest = components.source_input_sha256(root)
    restic = _Restic()
    calls = []

    def run(argv, *, cwd, cancellation, timeout=1800):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return b"RCC version: v18.19.5\n"
        if argv[1:4] == ["env", "publish", "--robot"]:
            return json.dumps({
                "artifactDigest": "sha256:" + "1" * 64,
                "specificationDigest": "sha256:" + "2" * 64,
                **({"platform": publish_platform} if publish_platform else {}),
                "legacyBlueprintKey": "synthetic-blueprint",
            }).encode()
        if argv[1:3] == ["env", "acquire"]:
            return json.dumps({"artifactDigest": "sha256:" + "1" * 64,
                "verification": {"valid": True, "artifactDigest": "sha256:" + "1" * 64, "platform": "linux_amd64"}}).encode()
        if argv[1:3] == ["env", "export"]:
            Path(argv[-1]).write_bytes(b"rcca payload")
            return b""
        raise AssertionError(argv)

    monkeypatch.setattr(components, "_run", run)
    result = components.capture_rcc_component(
        workspace=root,
        prior_component=_prior("f" * 64),
        repository_id=REPOSITORY_ID,
        repository_format=2,
        restic=restic,
        rcc_runtime="/synthetic/rcc",
    )
    assert [call[1:3] for call in calls] == [["--version"], ["env", "publish"],
        *([["env", "acquire"]] if publish_platform is None else []), ["env", "export"]]
    assert calls[1] == [
        "/synthetic/rcc", "env", "publish", "--robot", str(root / "robot.yaml"), "--provider", "local", "--json"
    ]
    assert calls[-1][1:5] == ["env", "export", "--artifact", "sha256:" + "1" * 64]
    assert calls[-1][calls[-1].index("--provider") + 1] == "local"
    assert result["artifact_digest"] == "sha256:" + "1" * 64
    assert result["specification_digest"] == "sha256:" + "2" * 64
    assert result["snapshot"]["repository_id"] == REPOSITORY_ID
    assert restic.backups[0][1] == SNAPSHOT_ID
    assert restic.backups[0][2] == ["metadata.json", "rcc-environment.rcca"]
    assert result["source_input_sha256"] == digest
    assert digest


def test_component_rejects_prior_snapshot_from_another_repository(tmp_path):
    root = _workspace(tmp_path)
    prior = _prior("f" * 64)
    prior["snapshot"]["repository_id"] = "0" * 64
    with pytest.raises(components.RoomStoreComponentError, match="another Room Store"):
        components.capture_rcc_component(
            workspace=root,
            prior_component=prior,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=_Restic(),
            rcc_runtime="/synthetic/rcc",
        )


def test_native_metadata_mismatch_fails_closed(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    monkeypatch.setattr(
        components,
        "_run",
        lambda argv, **kwargs: b"RCC version: v18.19.5" if argv[1:] == ["--version"] else b'{"artifactDigest":"bad"}',
    )
    with pytest.raises(components.RoomStoreComponentError, match="environment metadata"):
        components.capture_rcc_component(
            workspace=root,
            prior_component=None,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=_Restic(),
            rcc_runtime="/synthetic/rcc",
        )


def test_wrong_native_platform_fails_before_export_or_backup(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    restic = _Restic()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return b"RCC version: v18.19.5"
        return json.dumps({
            "artifactDigest": "sha256:" + "1" * 64,
            "specificationDigest": "sha256:" + "2" * 64,
            "platform": "win32-x64" if components._host_platform() == "linux-x64" else "linux-x64",
            "legacyBlueprintKey": "synthetic-blueprint",
        }).encode()

    monkeypatch.setattr(components, "_run", run)
    with pytest.raises(components.RoomStoreComponentError, match="mismatched environment platform"):
        components.capture_rcc_component(
            workspace=root,
            prior_component=None,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=restic,
            rcc_runtime="/synthetic/rcc",
        )
    assert len(calls) == 2
    assert restic.backups == []


@pytest.mark.parametrize("field,wrong", [("platform", "win32-x64"), ("rcc_version", "v18.19.3")])
def test_reuse_rejects_changed_platform_or_runtime(tmp_path, monkeypatch, field, wrong):
    root = _workspace(tmp_path)
    digest = components.source_input_sha256(root)
    prior = _prior(digest)
    prior[field] = wrong
    restic = _Restic()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return b"RCC version: v18.19.5"
        if argv[1:3] == ["env", "publish"]:
            return json.dumps({
                "artifactDigest": "sha256:" + "1" * 64,
                "specificationDigest": "sha256:" + "2" * 64,
                "platform": components._host_platform(),
                "legacyBlueprintKey": "synthetic-blueprint",
            }).encode()
        Path(argv[-1]).write_bytes(b"rcca")
        return b""

    monkeypatch.setattr(components, "_run", run)
    result = components.capture_rcc_component(
        workspace=root,
        prior_component=prior,
        repository_id=REPOSITORY_ID,
        repository_format=2,
        restic=restic,
        rcc_runtime="/synthetic/rcc",
    )
    assert len(calls) == 3
    assert result["platform"] == components._host_platform()
    assert result["rcc_version"] == components.RCC_VERSION


def test_wrong_rcc_executable_version_fails_before_publish(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    calls = []
    monkeypatch.setattr(components, "_run", lambda argv, **kwargs: calls.append(argv) or b"RCC version: v18.19.50")
    with pytest.raises(components.RoomStoreComponentError, match="version is unsupported"):
        components.capture_rcc_component(
            workspace=root,
            prior_component=None,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=_Restic(),
            rcc_runtime="/synthetic/rcc",
        )
    assert calls == [["/synthetic/rcc", "--version"]]


def test_missing_rcc_fails_closed_without_path_diagnostic(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    monkeypatch.setattr(components.shutil, "which", lambda _name: None)
    with pytest.raises(components.RoomStoreComponentError) as error:
        components.capture_rcc_component(
            workspace=root,
            prior_component=None,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=_Restic(),
        )
    assert str(root) not in str(error.value)


def test_source_change_during_export_prevents_component_backup(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    restic = _Restic()
    digests = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(components, "source_input_sha256", lambda *_args, **_kwargs: next(digests))

    def run(argv, **kwargs):
        if argv[1:] == ["--version"]:
            return b"RCC version: v18.19.5"
        if argv[1:3] == ["env", "publish"]:
            return json.dumps({
                "artifactDigest": "sha256:" + "1" * 64,
                "specificationDigest": "sha256:" + "2" * 64,
                "platform": "linux-x64",
                "legacyBlueprintKey": "synthetic-blueprint",
            }).encode()
        Path(argv[-1]).write_bytes(b"rcca")
        return b""

    monkeypatch.setattr(components, "_run", run)
    with pytest.raises(components.RoomStoreComponentError, match="changed during capture"):
        components.capture_rcc_component(
            workspace=root,
            prior_component=None,
            repository_id=REPOSITORY_ID,
            repository_format=2,
            restic=restic,
            rcc_runtime="/synthetic/rcc",
        )
    assert restic.backups == []


def test_no_root_robot_means_no_component(tmp_path):
    assert components.capture_rcc_component(
        workspace=tmp_path,
        prior_component=None,
        repository_id=REPOSITORY_ID,
        repository_format=2,
        restic=_Restic(),
        rcc_runtime="/synthetic/rcc",
    ) is None
